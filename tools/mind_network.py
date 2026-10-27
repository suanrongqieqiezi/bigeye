# -*- coding: utf-8 -*-
"""整理记忆网络 —— 把 ReflectionLoop 的十几项职责变成一个可见、可控、可独立运行的会话。

为什么单独一个模块（而不是塞进 mind_organize）：
  1. 数据完全不同。mind_organize 只碰 meta.self_memo（一份 1000 字的清单）；
     这里碰的是 memory_fragments / memory_relations（10640 条碎片、2125 条边）。
     两件事的耗时、失败模式、节流策略没有一处相同，混在一个会话里只会互相拖累。
  2. 两者可以同时跑。备忘整理读 meta、写 meta；网络整理读碎片、写碎片与边，
     没有共享写目标，也没共享锁。串行化它们等于凭空多等几分钟。
  3. 网络整理的每一步都是既有能力（memory/reflection_loop.py 里的 11 个方法），
     以前只能通过 AI 工具或裸 API 触发，全程黑箱。本模块不重写它们，
     只负责「一项一项跑 + 每一项都说清楚跑了什么、产出多少、花多久」。

三条硬约束（与 mind_organize 一致，都是用户要求的）：
  1. **独立线程**：daemon，不碰 topics / messages / task DAG。
  2. **不干预其它任务**：同类同时只允许一个；取消在「项与项之间」生效
     （单步内部是纯 DB 循环或一次 LLM 调用，强行打断会留下半截数据）。
  3. **触发时间由用户定**：手动 或 每天 HH:MM。

观测：
  - `live()` 读进程内存，前端 1.5s 轮询零 DB 成本
  - 结束后精简落 `mind_net_sessions`（最近 20 次）
  - 每步记录：起始快照 → 耗时 → 产出 → 结束快照
"""
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from urllib.parse import quote

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from db import get_db

CFG_KEY = "mind_net_cfg"
SESS_KEY = "mind_net_sessions"
AUTO_KEY = "mind_net_last_auto"
MAX_SESSIONS = 20
KEEP_EVENTS = 100
GAP_SAMPLES = 5            # 盲区只列前几条，全列会把长文冲垮

DEFAULT_CFG = {
    "auto": False,
    "at": "04:00",         # 比备忘整理晚一小时，错开夜里两个 LLM 高峰
    "max_minutes": 30,     # 整场硬上限，超了在下一项之前停（防止某项卡到天亮）
}

# 11 项职责。顺序沿用 memory/reflection_loop.run() —— 那里有依赖关系：
# backfill_entities 必须最先（detect_gaps 依赖 entities 表）。
# kind 决定产出怎么读：int=计数 / gaps=列表 / obs=四动作 dict。
STEPS = [
    ("entities",  "回填实体",     "backfill_entities",       "int",  "条记忆"),
    ("fileth",    "提取文件思考", "extract_file_thoughts",   "int",  "条"),
    ("weak",      "建弱关联",     "build_weak_associations", "int",  "条边"),
    ("meta",      "涌现元晶",     "emerge_meta_crystals",    "int",  "个元晶"),
    ("prune",     "反熵修剪",     "prune_entropy",           "int",  "条边"),
    ("gaps",      "自检盲区",     "detect_gaps",             "gaps", ""),
    ("authority", "提拔权威",     "promote_authority",       "int",  "个"),
    ("knowlink",  "连接知识层",   "link_knowledge_cores",    "int",  "条"),
    ("story",     "叙事沉淀",     "narrative_consolidation", "int",  "个故事"),
    ("storyassoc", "故事互链",    "build_story_weak_assocs", "int",  "条边"),
    ("obs",       "观察整合",     "consolidate_observations", "obs", ""),
]
STEP_LABEL = {s[0]: s[1] for s in STEPS}


# ── 网络现状（只读，别用写连接：WAL 下 close 要读 -wal 做恢复，实测 450ms/次）──

def _ro():
    """只读连接。打不开就返回 None，调用方自己判 —— 不能因此让整个面板 500。"""
    try:
        path = get_db().path
        uri = "file:%s?mode=ro" % quote(os.path.abspath(path).replace("\\", "/"),
                                        safe="/:()+,._-")
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


SED_STORY_N, SED_AUTH_N, SED_KNOW_N = 30, 40, 40


def _clip(s, n):
    """压成一行 + 截断。列表里只用来"认得出是哪一条"，全文在记忆里，不在这儿。"""
    s = " ".join((s or "").replace("\r", " ").split())
    return s if len(s) <= n else s[:n] + "…"


def _day(ts):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except Exception:
        return ""


def sediment(net=None):
    """三类"沉淀物"的清单：故事链 / 权威记忆 / 知识库。

    都只读（_ro 走只读 URI，一次 7ms 量级），任何一项查不到就留空，不抛 ——
    面板上少一张卡，比整个 /api/mind 500 好。
    长文一律截断：这里要的是"能认出是哪一条 + 有多少条"，不是把记忆再复制一份。

    🔴 `net`（net_stats 的结果）要传进来：它的 `layers` 已经是一次全表 GROUP BY 的产物，
       这里再写 `COUNT(*) WHERE layer='story'` 就是**同一次全扫再来一遍** ——
       实测冷缓存 1003ms，比热了之后慢三个数量级（表 10k 行、每行带大段文本）。
    """
    out = {"stories": {"total": 0, "items": []},
           "authority": {"total": 0, "items": [], "files": 0},
           "knowledge": {"pages": 0, "versions": 0, "frags": 0, "items": []}}
    layers = ((net or {}).get("layers") or {})
    conn = _ro()
    if conn is None:
        return out
    try:
        # ── 故事链：最近 N 段，按事件时间倒序 ────────────────────
        # 🔴 别 `ORDER BY COALESCE(event_time,...)` —— 那要先把全部 story 行捞出来再排序
        #    （走不到任何索引，234ms↑）。story 是追加写入的，id 单调 ⇒ 先用主键倒序取到
        #    窗口，再在 Python 里按 event_time 排 —— 30 条而已。
        out["stories"]["total"] = layers.get("story", 0)
        rows = conn.execute(
            "SELECT id, text, event_time, tags, created_at "
            "FROM memory_fragments WHERE layer='story' AND dirty=1 "
            "ORDER BY id DESC LIMIT ?", (SED_STORY_N,)).fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "d": (r["event_time"] or _day(r["created_at"]))[:23],   # 可能是 "起 ~ 止"
                "t": _clip(r["text"], 300),
                "tags": _clip(_taglist(r["tags"]), 48),
            })
        items.sort(key=lambda x: (x["d"] or "", x["id"]), reverse=True)
        out["stories"]["items"] = items

        # ── 权威记忆：authority_level>=1 ────────────────────────
        out["authority"]["total"] = conn.execute(
            "SELECT COUNT(*) n FROM memory_fragments WHERE authority_level>=1"
        ).fetchone()["n"]
        for r in conn.execute(
                "SELECT id, text, layer, source, tags, weight, authority_level "
                "FROM memory_fragments WHERE authority_level>=1 "
                "ORDER BY COALESCE(weight,0) DESC, id DESC LIMIT ?", (SED_AUTH_N,)):
            out["authority"]["items"].append({
                "id": r["id"],
                "t": _clip(r["text"], 150),
                "layer": r["layer"] or "",
                "src": _clip(r["source"], 20),
            })
        try:
            out["authority"]["files"] = conn.execute(
                "SELECT COUNT(*) n FROM file_crystals WHERE authority_level>=1"
            ).fetchone()["n"]
        except Exception:
            pass

        # ── 知识库：带版本的知识页 ──────────────────────────────
        try:
            out["knowledge"]["pages"] = conn.execute(
                "SELECT COUNT(*) n FROM knowledge_pages").fetchone()["n"]
            out["knowledge"]["versions"] = conn.execute(
                "SELECT COUNT(*) n FROM knowledge_page_versions").fetchone()["n"]
            for r in conn.execute(
                    "SELECT id, title, page_key, current_version, updated_at "
                    "FROM knowledge_pages ORDER BY COALESCE(updated_at,0) DESC LIMIT ?",
                    (SED_KNOW_N,)):
                out["knowledge"]["items"].append({
                    "id": r["id"],
                    "t": _clip(r["title"] or r["page_key"], 40),
                    "v": r["current_version"] or 0,
                    "d": _day(r["updated_at"]),
                })
        except Exception:
            pass
        out["knowledge"]["frags"] = layers.get("knowledge", 0)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# ── 单条详情：点标题进去看全文 ────────────────────────────────────────────
# 🔴 列表接口一律是**截断过**的（故事 300 字 / 权威 150 字 / 知识页只有标题），
#    它只负责"认出是哪一条"。所以"点开看内容"必须回服务端取全文 ——
#    前端把列表里那段再展开一次，最多显示到省略号为止（上一轮就是这么错的）。
#    全文不能塞进列表：468 页正文 + 656 条权威一次搬过来是好几 MB，而且
#    绝大多数条目用户根本不会点开。
# 取不到一律返回 ok=False + 人话，不抛：一条坏数据不该让整个详情层白屏。

_LAYER_CN = {"core": "核心", "knowledge": "知识", "story": "故事",
             "archival": "档案", "experience": "经验", "emotion": "情绪"}
# event_span 是模型写的英文粒度，界面上不要直接印 hours
_SPAN_CN = {"hours": "小时级", "hour": "小时级", "days": "按天", "day": "按天",
            "weeks": "按周", "week": "按周", "months": "按月", "month": "按月",
            "years": "按年", "year": "按年"}


def _taglist(raw):
    """tags 里混着压缩标记（compressed:hash:112），那是内部账，不给人看。"""
    return ", ".join([t for t in (raw or "").split(",")
                      if t and not t.startswith("compressed:")])


def detail(kind, ident, ver=None):
    """取一条沉淀物的全文。kind = story / auth / know。

    返回统一结构，前端只按它画：
      ok / kind / id / kicker（这是哪一类）/ title（可空）/ date /
      text（全文，原样不加工换行）/ tags / meta（[[中文标签, 值], …]）/ error
    """
    k = (kind or "").strip()
    out = {"ok": False, "kind": k, "id": None, "kicker": "", "title": "",
           "date": "", "text": "", "tags": "", "meta": [], "error": ""}
    try:
        rid = int(ident)
    except Exception:
        out["error"] = "参数不对"
        return out
    if k not in ("story", "auth", "know"):
        out["error"] = "不认识的类型"
        return out

    conn = _ro()
    if conn is None:
        out["error"] = "记忆库打不开"
        return out
    try:
        # ── 知识页：正文在 knowledge_page_versions 里，按版本取 ──────────
        if k == "know":
            page = conn.execute(
                "SELECT id, page_key, title, current_version, obs_ids, updated_at "
                "FROM knowledge_pages WHERE id=?", (rid,)).fetchone()
            if page is None:
                out["error"] = "这一页已经不在了"
                return out
            want = None
            try:
                want = int(ver) if ver not in (None, "", 0, "0") else None
            except Exception:
                want = None
            row = None
            if want:
                row = conn.execute(
                    "SELECT version, content FROM knowledge_page_versions "
                    "WHERE page_id=? AND version=?", (rid, want)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT version, content FROM knowledge_page_versions "
                    "WHERE page_id=? ORDER BY version DESC LIMIT 1", (rid,)).fetchone()
            try:
                nfrag = len(json.loads(page["obs_ids"] or "[]"))
            except Exception:
                nfrag = 0
            vnow = (row["version"] if row is not None else 0) or 0
            vtot = page["current_version"] or 0
            out.update({
                "ok": True, "id": page["id"], "kicker": "知识库",
                "title": page["title"] or page["page_key"] or ("知识页 #%d" % rid),
                "date": _day(page["updated_at"]),
                "text": (row["content"] if row is not None else "") or "",
                "meta": [["版本", "v%d / 共 %d 版" % (vnow, vtot)],
                         ["更新", _day(page["updated_at"])],
                         ["知识碎片", "%d 条" % nfrag],
                         ["目录键", (page["page_key"] or "—")]],
            })
            return out

        # ── 故事 / 权威：都在 memory_fragments，同一条 SQL ───────────────
        r = conn.execute(
            "SELECT id, text, event_time, event_span, tags, source, layer, weight, "
            "importance, recall_count, authority_level, created_at "
            "FROM memory_fragments WHERE id=?", (rid,)).fetchone()
        if r is None:
            out["error"] = "这条记忆已经不在了（整理时可能被并进别的碎片）"
            return out
        layer = r["layer"] or ""
        meta = [["层", _LAYER_CN.get(layer, layer or "—")],
                ["来源", r["source"] or "—"],
                ["权重", "%.2f" % (r["weight"] or 0)],
                ["重要度", "%.1f" % (r["importance"] or 0)],
                ["被召回", "%d 次" % (r["recall_count"] or 0)]]
        if r["authority_level"]:
            meta.append(["权威等级", str(r["authority_level"])])
        if r["event_span"]:
            # 库里存的是 LLM 写的英文粒度（hours / days / months…），
            # 界面上直接印 hours 很怪；认不出来的原样给，别乱猜。
            _sp = str(r["event_span"]).strip()
            meta.append(["事件跨度", _SPAN_CN.get(_sp.lower(), _sp[:40])])
        meta.append(["写入", _day(r["created_at"])])
        out.update({
            "ok": True, "id": r["id"],
            "kicker": "故事链" if k == "story" else "权威记忆",
            "date": (r["event_time"] or _day(r["created_at"]))[:23],
            "text": r["text"] or "",
            "tags": _taglist(r["tags"]),
            "meta": meta,
        })
        return out
    except Exception as e:
        out["error"] = "取内容失败：%s" % e
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def net_stats():
    """记忆网络的一张体检表。任何一项查不到就给 None，不抛。"""
    out = {"total": 0, "chars": 0, "layers": {}, "pending_cores": 0, "stories": 0,
           "relations": 0, "authority": 0, "crystals": 0, "entities": 0}
    conn = _ro()
    if conn is None:
        return out
    try:
        row = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(LENGTH(text)),0) c "
                           "FROM memory_fragments").fetchone()
        out["total"], out["chars"] = row["n"], row["c"]
        for r in conn.execute("SELECT layer, COUNT(*) n FROM memory_fragments GROUP BY layer"):
            out["layers"][r["layer"]] = r["n"]
        out["stories"] = out["layers"].get("story", 0)
        row = conn.execute(
            "SELECT COUNT(*) n FROM memory_fragments WHERE layer='core' AND dirty=1 "
            "AND crystal_parent_id IS NULL AND embedding IS NOT NULL AND embedding != '[]'"
        ).fetchone()
        out["pending_cores"] = row["n"]
        row = conn.execute("SELECT COUNT(*) n FROM memory_fragments "
                           "WHERE crystal_parent_id IS NOT NULL").fetchone()
        out["crystals"] = row["n"]
        row = conn.execute("SELECT COUNT(*) n FROM memory_fragments "
                           "WHERE authority_level >= 1").fetchone()
        out["authority"] = row["n"]
        row = conn.execute("SELECT COUNT(*) n FROM memory_relations WHERE status='active'").fetchone()
        out["relations"] = row["n"]
        row = conn.execute("SELECT COUNT(*) n FROM memory_entities").fetchone()
        out["entities"] = row["n"]
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


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
    at = str(out.get("at") or DEFAULT_CFG["at"])
    try:
        hh, mm = at.split(":")[0], at.split(":")[1]
        out["at"] = ("%02d:%02d" % (int(hh), int(mm))
                     if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 else DEFAULT_CFG["at"])
    except Exception:
        out["at"] = DEFAULT_CFG["at"]
    try:
        out["max_minutes"] = max(3, min(180, int(out.get("max_minutes") or 30)))
    except Exception:
        out["max_minutes"] = 30
    return out


def save_cfg(**kw):
    cfg = load_cfg()
    for k in ("auto", "at", "max_minutes"):
        if k in kw and kw[k] is not None:
            cfg[k] = kw[k]
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
_LIVE = None
_CANCEL = threading.Event()
_THREAD = None
# 与后台「轻量叙事触发」互斥：两处都调 narrative_consolidation 会重复造故事。
# 用同一把锁把「有人正在动记忆网络」这件事说清楚，谁在跑另一边就退让。
NET_BUSY = threading.Lock()


def live():
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


def _push(sess, text, kind="info"):
    with _LOCK:
        sess.setdefault("events", []).append({
            "ts": int(time.time()), "kind": kind, "text": str(text)[:400],
        })
        sess["updated_at"] = int(time.time())


# ── 历史 ────────────────────────────────────────────

def _persist(sess):
    try:
        slim = dict(sess)
        slim["events"] = (sess.get("events") or [])[-KEEP_EVENTS:]
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
        if s.get("status") == "running" and not is_running():
            s = dict(s)
            s["status"] = "interrupted"
        out.append(s)
    return out


def clear_sessions():
    try:
        get_db().set_meta(SESS_KEY, "[]")
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


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


# ── 单步产出怎么读成人话 ──────────────────────────────

def _format_result(step_key, kind, unit, raw):
    """把各方法的返回值翻译成一句话。返回 (简短结果文本, 明细列表)。

    🔴 三样都可能有：int（计数）、list（盲区）、dict（四动作）。不能一律 str() ——
       dict 直接 str 出来的是一坨 {'create': 2, ...}，用户看不出发生了什么。
    """
    if kind == "int":
        try:
            n = int(raw or 0)
        except Exception:
            n = 0
        return ("%d %s" % (n, unit)).strip(), []
    if kind == "gaps":
        rows = raw if isinstance(raw, list) else []
        detail = []
        for g in rows[:GAP_SAMPLES]:
            try:
                name = g.get("entity_name") or g.get("suggestion") or ""
                cnt = g.get("mention_count")
                detail.append("「%s」被提到 %s 次，但还没有记忆" % (name, cnt) if cnt
                              else str(name))
            except Exception:
                continue
        return "%d 个盲区" % len(rows), detail
    if kind == "obs":
        d = raw if isinstance(raw, dict) else {}
        parts = []
        for k, label in (("create", "新建"), ("update", "更新"), ("merge", "合并"),
                         ("contradict", "发现矛盾"), ("skipped", "跳过")):
            if int(d.get(k) or 0):
                parts.append("%s %d" % (label, int(d.get(k) or 0)))
        return ("观察整合：" + (" · ".join(parts) if parts else "没有需要动的")), []
    return str(raw)


def _diff(before, after):
    """整理前后关键数字的变化，只报变了的。"""
    out = []
    for key, label in (("total", "碎片"), ("stories", "故事"), ("relations", "关系边"),
                       ("crystals", "晶体"), ("authority", "权威"), ("pending_cores", "待整理")):
        b = int((before or {}).get(key) or 0)
        a = int((after or {}).get(key) or 0)
        if a != b:
            out.append("%s %d→%d" % (label, b, a))
    return " · ".join(out)


# ── 执行体 ──────────────────────────────────────────

def _run(sess):
    acquired = NET_BUSY.acquire(blocking=False)
    try:
        if not acquired:
            msg = "记忆网络正在被别处整理（后台轻量叙事触发），本次跳过"
            with _LOCK:
                sess["status"] = "skipped"
                sess["stop_reason"] = msg
            _push(sess, msg, "warn")
            return
        _run_steps(sess)
    except Exception as e:
        import traceback
        traceback.print_exc()
        with _LOCK:
            sess["status"] = "error"
            sess["error"] = str(e)[:300]
        try:
            _push(sess, "整理线程异常：%s" % e, "error")
        except Exception:
            pass
    finally:
        if acquired:
            try:
                NET_BUSY.release()
            except Exception:
                pass
        with _LOCK:
            sess["ended_at"] = int(time.time())
            sess["elapsed"] = sess["ended_at"] - int(sess.get("started_at") or sess["ended_at"])
        _persist(sess)


def _run_steps(sess):
    cfg = load_cfg()
    from memory.reflection_loop import get_loop
    loop = get_loop()

    before = net_stats()
    sess["before"] = before
    sess["model"] = "本地（无 LLM 的步骤跳过模型）"
    _push(sess, "开始整理记忆网络｜当前 %d 条碎片 / %d 条关系边 / %d 条待整理"
                % (before["total"], before["relations"], before["pending_cores"]), "start")

    deadline = time.time() + int(cfg.get("max_minutes") or 30) * 60
    total = len(STEPS)
    stopped = "全部 %d 项跑完" % total
    done_n = 0
    idx = 0

    for idx, (key, label, meth, kind, unit) in enumerate(STEPS):
        if _CANCEL.is_set():
            stopped = "手动取消"
            _push(sess, "收到取消指令，停在「%s」之前" % label, "warn")
            break
        if time.time() > deadline:
            stopped = "超过 %d 分钟上限" % int(cfg.get("max_minutes") or 30)
            _push(sess, "已经跑了 %d 分钟，还剩后面几项，先停在这里"
                        "（下次接着跑，各项都是增量的）" % int(cfg.get("max_minutes") or 30), "warn")
            break

        step = {
            "n": idx + 1, "key": key, "label": label, "status": "running",
            "ts": int(time.time()), "elapsed": 0, "result": "", "detail": [], "error": "",
        }
        with _LOCK:
            sess.setdefault("steps", []).append(step)
            sess["done"] = idx
        _push(sess, "第 %d/%d 项 · %s · 开始" % (idx + 1, total, label), "step")

        t0 = time.time()
        try:
            raw = getattr(loop, meth)()
        except Exception as e:
            step["status"] = "error"
            step["error"] = str(e)[:200]
            step["elapsed"] = round(time.time() - t0, 1)
            _push(sess, "第 %d 项 · %s · 失败：%s" % (idx + 1, label, step["error"]), "error")
            continue

        step["elapsed"] = round(time.time() - t0, 1)
        step["status"] = "ok"
        text, detail = _format_result(key, kind, unit, raw)
        step["result"] = text
        step["detail"] = detail
        done_n += 1
        _push(sess, "第 %d 项 · %s · %s（%.1fs）" % (idx + 1, label, text, step["elapsed"]), "ok")
        for d in detail:
            _push(sess, "　　%s" % d, "detail")

        # 每项之间递增进度，前端能看出「跑到第几项了」
        with _LOCK:
            sess["done"] = idx + 1
    else:
        idx = total

    after = net_stats()
    sess["after"] = after
    sess["status"] = "cancelled" if _CANCEL.is_set() else "done"
    sess["stop_reason"] = stopped
    delta = _diff(before, after)
    _push(sess, "整理结束（%s）｜完成 %d/%d 项%s"
                % (stopped, done_n, total, ("｜" + delta) if delta else "｜数字没有变化"), "done")


def start(trigger="manual"):
    """起一个网络整理会话。返回 {session_id} 或 {error}。"""
    global _LIVE, _THREAD
    with _LOCK:
        if _LIVE and _LIVE.get("status") == "running":
            return {"error": "已经有一次记忆网络整理在跑", "running": _LIVE.get("id")}
        _CANCEL.clear()
        sess = {
            "id": "n_" + uuid.uuid4().hex[:8],
            "kind": "network",
            "trigger": trigger,
            "status": "running",
            "started_at": int(time.time()),
            "ended_at": 0,
            "model": "", "steps": [], "events": [],
            "done": 0, "total": len(STEPS),
            "before": None, "after": None,
            "stop_reason": "",
        }
        _LIVE = sess
    t = threading.Thread(target=_run, args=(sess,), daemon=True, name="mind-network")
    _THREAD = t
    t.start()
    return {"success": True, "session_id": sess["id"]}


def cancel():
    if not is_running():
        return {"error": "当前没有在跑的网络整理"}
    _CANCEL.set()
    return {"success": True, "message": "已发出停止指令，等当前这一项跑完就停"}


# ── 后台「轻量叙事触发」的可观测面 ─────────────────────
# server.py 里 _lightweight_narrative 是既有的自动整理路径：积压 ≥30 且距上次 ≥2h，
# 只跑 narrative_consolidation。它是静的 —— 用户看不到上次什么时候跑、跑了几次。

LIGHT_KEY = "mind_net_light_stat"


def light_stat():
    """后台轻量整理的历史记录（由 server 侧写入）。"""
    try:
        raw = get_db().get_meta(LIGHT_KEY)
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def note_light(kind, n=0, backlog=0, err=""):
    """记一笔后台轻量整理。保留最近 30 条，异常不影响主流程。"""
    try:
        d = light_stat()
        runs = d.get("runs") or []
        runs.append({"ts": int(time.time()), "kind": kind, "n": int(n or 0),
                     "backlog": int(backlog or 0), "err": str(err)[:200]})
        d["runs"] = runs[-30:]
        d["last_ts"] = int(time.time())
        get_db().set_meta(LIGHT_KEY, json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def state():
    """给前端的一揽子状态。"""
    _net = net_stats()          # 全表 GROUP BY 只做一次，sediment 复用它的 layers
    return {
        "cfg": load_cfg(),
        "live": live(),
        "running": is_running(),
        "sessions": sessions(12),
        "net": _net,
        "light": light_stat(),
        "steps": [{"key": s[0], "label": s[1]} for s in STEPS],
        # 三类沉淀物的清单（故事链 / 权威记忆 / 知识库）。只读 + 截断，
        # 跟着这一个接口回来 —— 不另开路由，省掉一次重启。
        "sediment": sediment(_net),
    }
