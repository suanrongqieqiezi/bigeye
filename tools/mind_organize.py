# -*- coding: utf-8 -*-
"""自我思绪整理 —— 把「收敛反思碎片」变成一个可见、可控、可独立运行的整理会话。

为什么要有这个模块（而不是继续让 self_memo 在背后偷偷收敛）：
  self_memo.maybe_converge() 是被反思流程顺带触发的，安静、节流、不可观测。
  用户看不到它在干什么，也不知道它把哪条纪律合并了、为什么淘汰某条。
  而「整理」这件事的价值恰好全在过程里 —— 用户要能看到哪些碎片进来、
  模型怎么归并、备忘从几条变成几条。

引擎与注入的分工：
  本模块只管「跑一次整理」，产出仍然写进 self_memo 的 meta（那一份才是注入源）。
  ⇒ 注入路径一行没动，这里挂了也不影响对话。

三条硬约束（都是用户明确要求的）：
  1. **独立线程**：daemon 线程，不碰 topics / messages / task DAG，
     不占 /api/chat 的任何锁；只做「只读碎片 + 写自己 meta」。
  2. **不干预其它任务**：同一时刻只允许一个整理会话；取消由 cancel_event 中断流式。
  3. **触发时间由用户定**：手动 或 每天 HH:MM（server 侧守护线程负责到点触发）。

观测方式：
  - 进程内 `live()` 返回当前会话快照（**不读 DB**，前端 1.5s 轮询零成本）
  - 结束后精简后落 `mind_sessions`（保留最近 30 次，供回看）
  - 每轮记录：进来的碎片原文、模型流式写了多少字、归并结果、额度前后变化
  ⇒ 拼起来就是一篇「整理过程长文」，前端按时间线渲染。
"""
import json
import os
import sys
import threading
import time
import uuid

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from db import get_db
from tools import self_memo as sm

CFG_KEY = "mind_cfg"
SESS_KEY = "mind_sessions"
MAX_SESSIONS = 30          # 历史保留次数
MAX_ROUNDS = 12            # 单次整理最多跑几轮（每轮 25 条碎片）
KEEP_FRAG_SAMPLES = 3      # 历史里每条轮次保留几例碎片原文（其余只记数量）
KEEP_RAW_CHARS = 900       # 历史里保留多少字符的模型原文

DEFAULT_CFG = {
    "auto": False,          # 是否每天定时整理
    "at": "03:00",          # 定时时刻 HH:MM
    "provider": "",         # 空 = 跟随主模型
    "model": "",
    "max_rounds": MAX_ROUNDS,
}


# ── 配置 ────────────────────────────────────────────

def load_cfg():
    try:
        raw = get_db().get_meta(CFG_KEY)
        cfg = json.loads(raw) if raw else {}
    except Exception:
        cfg = {}
    out = dict(DEFAULT_CFG)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULT_CFG})
    out["auto"] = str(out.get("auto", False)).lower() in ("1", "true")
    at = str(out.get("at") or "03:00")
    # 只接受 HH:MM，坏值回落默认 —— 守护线程按字符串比较，格式错会永远不触发
    try:
        hh, mm = at.split(":")[0], at.split(":")[1]
        if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
            out["at"] = "%02d:%02d" % (int(hh), int(mm))
        else:
            out["at"] = DEFAULT_CFG["at"]
    except Exception:
        out["at"] = DEFAULT_CFG["at"]
    try:
        out["max_rounds"] = max(1, min(30, int(out.get("max_rounds") or MAX_ROUNDS)))
    except Exception:
        out["max_rounds"] = MAX_ROUNDS
    return out


def save_cfg(**kw):
    cfg = load_cfg()
    for k in ("auto", "at", "provider", "model", "max_rounds"):
        if k in kw and kw[k] is not None:
            cfg[k] = kw[k]
    # 规范化后存，避免脏值进库
    try:
        hh, mm = str(cfg["at"]).split(":")[0], str(cfg["at"]).split(":")[1]
        cfg["at"] = "%02d:%02d" % (int(hh), int(mm))
    except Exception:
        cfg["at"] = DEFAULT_CFG["at"]
    cfg["auto"] = bool(cfg["auto"])
    get_db().set_meta(CFG_KEY, json.dumps(cfg, ensure_ascii=False))
    return load_cfg()


# ── 进程内活动会话 ───────────────────────────────────

_LOCK = threading.RLock()
_LIVE = None                 # 当前/最近一次会话（内存）
_CANCEL = threading.Event()
_THREAD = None


def live():
    """当前会话快照。返回 None 表示没跑过。深拷贝，避免前端读到一半被改。"""
    with _LOCK:
        if _LIVE is None:
            return None
        try:
            return json.loads(json.dumps(_LIVE, ensure_ascii=False))
        except Exception:
            return None


def is_running():
    with _LOCK:
        return bool(_LIVE and _LIVE.get("status") == "running")


def _push_event(sess, text, kind="info"):
    """往会话长文里追加一行。这是「实时情况」的唯一载体。"""
    with _LOCK:
        sess.setdefault("events", []).append({
            "ts": int(time.time()), "kind": kind, "text": str(text)[:400],
        })
        sess["updated_at"] = int(time.time())


# ── 历史 ────────────────────────────────────────────

def sessions(limit=10):
    try:
        raw = get_db().get_meta(SESS_KEY)
        rows = json.loads(raw) if raw else []
        if not isinstance(rows, list):
            return []
    except Exception:
        return []
    out = []
    for s in reversed(rows[-limit:]):
        if not isinstance(s, dict):
            continue
        # 进程重启后残留的 running 状态：标记为中断。
        # 判据不能只看 status —— 重启后 _LIVE 为空，running 一定是假的。
        if s.get("status") == "running" and not is_running():
            s = dict(s)
            s["status"] = "interrupted"
        out.append(s)
    return out


def _persist(sess):
    """会话落库（精简版）。异常不影响主流程。"""
    try:
        slim = dict(sess)
        slim["rounds"] = []
        for r in (sess.get("rounds") or []):
            rr = dict(r)
            frags = rr.pop("frags", None) or []
            rr["frags_n"] = rr.get("frags_n") or len(frags)
            rr["frag_samples"] = [f for f in frags[:KEEP_FRAG_SAMPLES]]
            rr["raw"] = (rr.get("raw") or "")[:KEEP_RAW_CHARS]
            rr.pop("prompt", None)
            slim["rounds"].append(rr)
        slim["events"] = (sess.get("events") or [])[-80:]
        db = get_db()
        try:
            rows = json.loads(db.get_meta(SESS_KEY) or "[]")
            rows = rows if isinstance(rows, list) else []
        except Exception:
            rows = []
        rows.append(slim)
        db.set_meta(SESS_KEY, json.dumps(rows[-MAX_SESSIONS:], ensure_ascii=False))
    except Exception:
        import traceback
        traceback.print_exc()


def clear_sessions():
    try:
        get_db().set_meta(SESS_KEY, "[]")
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


# 定时触发「当天是否已跑过」的标记。放 meta 不放内存 —— 服务半夜重启后
# 内存标记会丢，同一天就会再跑一次（用户会发现备忘莫名多了一版）。
AUTO_KEY = "mind_last_auto"


def auto_fired_day():
    try:
        return str(get_db().get_meta(AUTO_KEY) or "")
    except Exception:
        return ""


def mark_auto_fired(day):
    try:
        get_db().set_meta(AUTO_KEY, str(day))
    except Exception:
        pass



# ── 执行体 ──────────────────────────────────────────

def _brief(sess):
    """给前端/日志看的一行额度摘要。"""
    doc = sm.load()
    txt, st = sm.render()
    return {"rules": len(doc.get("rules") or []), "notes": st.get("notes", 0),
            "chars": st.get("chars", 0), "budget": sm.BUDGET_CHARS,
            "version": doc.get("version") or 0}


def _run(sess, resolver):
    """线程体：多轮收敛 + 收口。任何异常都必须落到 sess.status，不能静默。"""
    try:
        cfg = load_cfg()
        llm_config = None
        try:
            if resolver:
                llm_config = resolver(cfg.get("provider") or "", cfg.get("model") or "")
        except Exception as e:
            _push_event(sess, f"模型配置解析失败，回退默认模型：{e}", "warn")
            llm_config = None
        sess["model"] = getattr(llm_config, "model", "") or (cfg.get("model") or "跟随主模型")
        sess["provider"] = getattr(llm_config, "provider", "") or (cfg.get("provider") or "默认")
        _push_event(sess, f"开始整理｜模型：{sess['model']}", "start")

        before_all = _brief(sess)
        sess["before"] = before_all
        _push_event(sess, f"整理前：{before_all['rules']} 条纪律 / {before_all['notes']} 条关于你 / "
                          f"{before_all['chars']}/{before_all['budget']} 字")

        state = sm._load_converge_state()
        rounds_cap = int(cfg.get("max_rounds") or MAX_ROUNDS)
        total_in = 0
        stopped_reason = "轮次上限"
        did_apply = 0

        for i in range(rounds_cap):
            if _CANCEL.is_set():
                stopped_reason = "手动取消"
                _push_event(sess, "收到取消指令，停在当前轮", "warn")
                break

            frags = []
            try:
                frags = sm._collect_new_fragments(state)
            except Exception as e:
                _push_event(sess, f"读取反思碎片失败：{e}", "error")
                stopped_reason = "读取碎片失败"
                break
            if not frags:
                stopped_reason = "没有新的反思记录"
                _push_event(sess, "没有新的反思记录了，本次整理到此为止")
                break

            total_in += len(frags)
            rnd = {
                "n": i + 1, "ts": int(time.time()), "status": "running",
                "frags_n": len(frags), "frags": [{"text": f} for f in frags],
                "text_len": 0, "thinking_len": 0, "tail": "",
                "added": [], "merged": [], "added_notes": [], "merged_notes": [],
                "dropped": [], "raw": "", "error": "", "before": None, "after": None,
            }
            with _LOCK:
                sess.setdefault("rounds", []).append(rnd)
            _push_event(sess, f"第 {rnd['n']} 轮｜读到 {len(frags)} 条新的失误记录", "round")

            doc = sm.load()
            rnd["before"] = _brief(sess)
            try:
                prompt = sm.build_converge_prompt(doc, frags)
            except Exception as e:
                rnd["status"] = "error"
                rnd["error"] = f"组 prompt 失败：{e}"
                _push_event(sess, rnd["error"], "error")
                stopped_reason = "组 prompt 失败"
                break

            t0 = time.time()

            def _on_prog(tlen, thlen, tail):
                # 流式进度：这是「实时看到它正在写什么」的唯一来源。
                with _LOCK:
                    rnd["text_len"] = tlen
                    rnd["thinking_len"] = thlen
                    rnd["tail"] = (tail or "")[-160:]
                    rnd["status"] = "streaming"

            _push_event(sess, f"第 {rnd['n']} 轮｜调用模型思考中…", "call")
            resp = sm.converge_call(llm_config, prompt, on_progress=_on_prog,
                                    cancel_event=_CANCEL, kind="整理")
            rnd["elapsed"] = round(time.time() - t0, 1)

            if resp.get("error"):
                rnd["status"] = "error"
                rnd["error"] = str(resp.get("error"))[:300]
                rnd["raw"] = (resp.get("text") or "")[:KEEP_RAW_CHARS]
                _push_event(sess, f"第 {rnd['n']} 轮｜模型调用失败：{rnd['error']}", "error")
                stopped_reason = "模型调用失败"
                break

            raw = resp.get("text", "") or ""
            rnd["raw"] = raw
            rnd["thinking_len"] = len(resp.get("thinking") or "")
            rnd["text_len"] = len(raw)
            usage = resp.get("usage") or {}
            _push_event(sess, f"第 {rnd['n']} 轮｜模型返回 {len(raw)} 字正文"
                              f"（思考 {rnd['thinking_len']} 字，{rnd['elapsed']}s）", "call")

            try:
                new_rules, new_notes, unchanged = sm._parse_converge(raw)
            except Exception as e:
                rnd["status"] = "error"
                rnd["error"] = f"解析失败：{e}"
                _push_event(sess, rnd["error"], "error")
                stopped_reason = "解析失败"
                break

            if unchanged:
                rnd["status"] = "unchanged"
                _push_event(sess, f"第 {rnd['n']} 轮｜模型判断这批没有新东西（UNCHANGED）")
                stopped_reason = "模型判断无新增"
                state["last_run_ts"] = int(time.time())
                state["last_count"] = len(frags)
                state["fail_streak"] = 0
                sm._save_converge_state(state)
                break

            if not new_rules and not new_notes:
                # 解析不出 → 不动现有备忘。连续 3 次失败才推进基线（同 maybe_converge）。
                streak = int(state.get("fail_streak") or 0) + 1
                state["fail_streak"] = streak
                rnd["status"] = "parse_fail"
                rnd["error"] = "模型输出里没有可解析条目"
                if streak >= 3:
                    state["last_run_ts"] = int(time.time())
                    state["fail_streak"] = 0
                    state["skipped_batches"] = int(state.get("skipped_batches") or 0) + 1
                    _push_event(sess, f"第 {rnd['n']} 轮｜连续 3 次解析失败，跳过这批", "warn")
                    sm._save_converge_state(state)
                    stopped_reason = "连续解析失败"
                else:
                    _push_event(sess, f"第 {rnd['n']} 轮｜输出无可用条目（第 {streak} 次），"
                                      f"现有备忘未改动", "warn")
                    stopped_reason = "输出无可用条目"
                break

            try:
                report = sm.apply_converge(doc, new_rules, new_notes,
                                           note=f"思绪整理[{sess['trigger']}]")
            except Exception as e:
                rnd["status"] = "error"
                rnd["error"] = f"写入失败：{e}"
                _push_event(sess, rnd["error"], "error")
                stopped_reason = "写入失败"
                break
            if report.get("error"):
                rnd["status"] = "error"
                rnd["error"] = str(report["error"])
                _push_event(sess, rnd["error"], "error")
                stopped_reason = "写入失败"
                break

            did_apply += 1
            rnd["status"] = "ok"
            rnd["added"] = report.get("rules_added") or []
            rnd["merged"] = report.get("rules_merged") or []
            rnd["added_notes"] = report.get("notes_added") or []
            rnd["merged_notes"] = report.get("notes_merged") or []
            rnd["dropped"] = report.get("dropped") or []
            rnd["after"] = _brief(sess)

            for t in rnd["added"]:
                _push_event(sess, f"第 {rnd['n']} 轮｜新增纪律：{t}", "rule")
            for t in rnd["merged"]:
                _push_event(sess, f"第 {rnd['n']} 轮｜归并到已有纪律：{t}", "merge")
            for t in rnd["added_notes"]:
                _push_event(sess, f"第 {rnd['n']} 轮｜新增关于你的观察：{t}", "note")
            for t in rnd["dropped"]:
                _push_event(sess, f"第 {rnd['n']} 轮｜淘汰：{t}", "drop")
            _push_event(sess, f"第 {rnd['n']} 轮｜完毕：{rnd['before']['rules']}→"
                              f"{rnd['after']['rules']} 条纪律 / {rnd['before']['chars']}→"
                              f"{rnd['after']['chars']} 字", "round")

            state["last_run_ts"] = int(time.time())
            state["last_count"] = len(frags)
            state["fail_streak"] = 0
            sm._save_converge_state(state)
        else:
            stopped_reason = "达到轮次上限"

        # 收口：多轮增量必然产生同义条目并排（真库实测 3 条「看返回值」各带次数）
        if did_apply >= 2 and not _CANCEL.is_set():
            _push_event(sess, "多轮增量后条目会有同义并排，开始收口整理…", "consolidate")
            try:
                cres = sm.consolidate(llm_config)
            except Exception as e:
                cres = {"error": str(e)}
            sess["consolidate"] = cres
            if cres.get("success"):
                _push_event(sess, f"收口完成：纪律 {cres.get('rules')}，备注 {cres.get('notes')}",
                            "consolidate")
                for d in (cres.get("dropped") or []):
                    _push_event(sess, f"收口淘汰：{d}", "drop")
            elif cres.get("unchanged"):
                _push_event(sess, "收口：清单已经很整齐，没有可合并的", "consolidate")
            elif cres.get("skipped"):
                _push_event(sess, f"收口跳过：{cres.get('skipped')}", "consolidate")
            else:
                _push_event(sess, f"收口失败：{cres.get('error')}", "warn")

        after_all = _brief(sess)
        sess["after"] = after_all
        sess["total_fragments"] = total_in
        sess["status"] = "cancelled" if _CANCEL.is_set() else "done"
        sess["stop_reason"] = stopped_reason
        _push_event(sess, f"整理结束（{stopped_reason}）｜共读 {total_in} 条记录，"
                          f"内容 {before_all['rules']}→{after_all['rules']} 条纪律 / "
                          f"{before_all['chars']}→{after_all['chars']} 字", "done")
    except Exception as e:
        import traceback
        traceback.print_exc()
        with _LOCK:
            sess["status"] = "error"
            sess["error"] = str(e)[:300]
        try:
            _push_event(sess, f"整理线程异常：{e}", "error")
        except Exception:
            pass
    finally:
        with _LOCK:
            sess["ended_at"] = int(time.time())
            sess["elapsed"] = sess["ended_at"] - int(sess.get("started_at") or sess["ended_at"])
        _persist(sess)


def start(trigger="manual", resolver=None):
    """起一个整理会话（独立线程）。返回 {session_id} 或 {error}。"""
    global _LIVE, _THREAD
    with _LOCK:
        if _LIVE and _LIVE.get("status") == "running":
            return {"error": "已经有一次整理在跑", "running": _LIVE.get("id")}
        _CANCEL.clear()
        sess = {
            "id": "m_" + uuid.uuid4().hex[:8],
            "trigger": trigger,
            "status": "running",
            "started_at": int(time.time()),
            "ended_at": 0,
            "model": "", "provider": "",
            "rounds": [], "events": [],
            "before": None, "after": None,
            "total_fragments": 0,
            "stop_reason": "",
        }
        _LIVE = sess
    t = threading.Thread(target=_run, args=(sess, resolver), daemon=True,
                         name="mind-organize")
    _THREAD = t
    t.start()
    return {"success": True, "session_id": sess["id"]}


def cancel():
    if not is_running():
        return {"error": "当前没有在跑的整理"}
    _CANCEL.set()
    return {"success": True, "message": "已发出停止指令，等当前这轮返回后停下"}


def state():
    """给前端的一揽子状态：配置 + 活会话 + 历史 + 备忘现状。"""
    return {
        "cfg": load_cfg(),
        "live": live(),
        "running": is_running(),
        "sessions": sessions(12),
        "memo": sm.stats(),
        "budget": sm.BUDGET_CHARS,
        "max_rules": sm.MAX_RULES,
        "max_notes": sm.MAX_NOTES,
    }
