# -*- coding: utf-8 -*-
"""行事备忘（Self Memo）——全局自我反省的**常驻收敛层**。

起点：memory/reflection.py 的 MemoryAI 已经在写全局反思碎片（source=deep/causal），
真库 279 条、31,234 字。但它们走记忆网络**召回**，而召回是"带着当前这句话去查相关记忆"——
犯错的当下恰恰没想起来要查。实测：「动手前先核实实际状态」这条教训被写了 39 遍还在犯。
所以需要一个**常驻**的收敛层：把反复出现的教训压成短条目，每次都在眼里。

与 miaow55 的对照（反面教材）：
  reflections.json 126 条 / 188KB，随轮次自我膨胀（179 字 → 2,609 字，术语堆叠 1,520 次），
  注入时只取最后一条，越写越没用。同一项目的 miaow_personality.txt 只有 829 字、全是可执行禁则，
  却每轮生效。
  ⇒ 结论：**常驻区能放的是"可执行禁则"，不是"自我感想"**。所以本模块的核心不是"让它自由反省"，
    而是给反省装三副夹板：
      ① 条数上限（纪律 16 / 用户 6）—— 多了必须淘汰，不许只增不减
      ② 字数上限（budget_chars=1000）—— 超了按 hits 强制截断
      ③ 每条 hits 必须可追溯 —— 数字来自真实碎片，不是模型自己觉得重要

用户画像的护栏（2026-10-26 拍板「允许性格层面总结」，所以不是禁止，是加约束）：
  - trait（性格判断）必须带 evidence：哪次对话/哪句话得出的
  - **反向证据通道**：观察到与标签不符的行为 → contradictions+1；满 2 次自动 disputed，
    从注入里摘掉、转待复核。这是防"标签自我加固"的唯一有效手段 ——
    标签一旦只收同向证据，就会越贴越牢，用户改了它仍按旧的对待。
  - 60 天没被再次验证 → 自动降级（trait→fact 或移除），并标注推断日期
  - 注入时 trait 带「（据<日期>）」，让模型知道这是可能过期的判断，不是事实

版本：每次写入都留一版到 self_memo_history（保留 20 版），支持回滚。
"""
import json
import os
import sys
import time
import uuid
import re

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from db import get_db

MEMO_KEY = "self_memo"
HIST_KEY = "self_memo_history"
CONVERGE_KEY = "self_memo_converge_state"

BUDGET_CHARS = 1000      # 常驻总字数硬上限
MAX_RULES = 16           # 行事纪律条数上限
MAX_NOTES = 6            # 关于用户条数上限
MAX_HISTORY = 20         # 保留版本数
RULE_MAX_CHARS = 52      # 单条纪律字数上限
NOTE_MAX_CHARS = 46      # 单条用户备注字数上限

# 收敛节流：新增碎片够多才收敛，且两次之间至少隔这么久（避免每轮重写冲掉前缀缓存）
CONVERGE_MIN_NEW = 8
CONVERGE_MIN_INTERVAL = 12 * 3600
TRAIT_EXPIRE_DAYS = 60

# 自动收敛的独占锁（见 maybe_converge 的注释：并发会让基线回退成旧值）
_CONVERGE_LOCK = None


def _converge_lock():
    global _CONVERGE_LOCK
    if _CONVERGE_LOCK is None:
        import threading
        _CONVERGE_LOCK = threading.Lock()
    return _CONVERGE_LOCK


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _now_ts():
    return int(time.time())


def _norm(t):
    if not t:
        return ""
    return re.sub(r"\s+", " ", str(t)).strip()


def _clip(t, n):
    t = _norm(t)
    return t if len(t) <= n else t[: max(0, n - 1)] + "…"


def _empty():
    return {
        "version": 0,
        "updated_at": "",
        "budget_chars": BUDGET_CHARS,
        "rules": [],
        "user_notes": [],
    }


# ── 存储 ────────────────────────────────────────────

def load():
    """读取当前备忘。任何异常都返回空结构，绝不抛给调用方（注入路径不能因为这里挂了而崩）。"""
    try:
        raw = get_db().get_meta(MEMO_KEY)
        if not raw:
            return _empty()
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            return _empty()
        doc.setdefault("rules", [])
        doc.setdefault("user_notes", [])
        doc.setdefault("budget_chars", BUDGET_CHARS)
        doc.setdefault("version", 0)
        return doc
    except Exception:
        return _empty()


def _load_history():
    try:
        raw = get_db().get_meta(HIST_KEY)
        if not raw:
            return []
        h = json.loads(raw)
        return h if isinstance(h, list) else []
    except Exception:
        return []


def save(doc, note=""):
    """写入并留痕。note 说明这次为什么变（回滚时给人看）。返回 True/False。"""
    try:
        db = get_db()
        prev = load()
        doc = dict(doc)
        doc["version"] = int(prev.get("version") or 0) + 1
        doc["updated_at"] = _now()
        doc["budget_chars"] = BUDGET_CHARS

        # 留痕：存**变更前**的那一版，回滚才有意义
        hist = _load_history()
        hist.append({
            "version": prev.get("version") or 0,
            "saved_at": prev.get("updated_at") or "",
            "note": _clip(note, 120),
            "rules": prev.get("rules") or [],
            "user_notes": prev.get("user_notes") or [],
        })
        hist = hist[-MAX_HISTORY:]

        db.set_meta(MEMO_KEY, json.dumps(doc, ensure_ascii=False))
        db.set_meta(HIST_KEY, json.dumps(hist, ensure_ascii=False))
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def history(limit=10):
    h = _load_history()
    return list(reversed(h))[:limit]


def rollback(version=None):
    """回滚到指定版本；version=None 表示退回上一版。返回 (ok, msg)。"""
    h = _load_history()
    if not h:
        return False, "没有可回滚的历史版本"
    if version is None:
        target = h[-1]
    else:
        target = None
        for item in reversed(h):
            if int(item.get("version") or 0) == int(version):
                target = item
                break
        if target is None:
            return False, f"找不到版本 {version}"
    doc = _empty()
    doc["rules"] = target.get("rules") or []
    doc["user_notes"] = target.get("user_notes") or []
    if not save(doc, note=f"回滚到 v{target.get('version')}"):
        return False, "写入失败"
    return True, f"已回滚到 v{target.get('version')}（{target.get('saved_at') or '-'}）"


# ── 额度控制：这是防膨胀的核心，不靠自律靠代码 ────────

def _trim(doc):
    """按 (hits, 最近命中) 排序裁剪，返回被淘汰的条目说明。

    🔴 裁剪后必须重新编号。曾经只排序不重编，结果注入里出现
       "17. … 2. … 1. … 3. …" 这种乱序 —— 排序生效了，编号还是旧的，
       模型看到一个从 17 开始的列表会直接忽略序号规则（用户按序号改条目也没法对齐）。
    """
    dropped = []
    rules = doc.get("rules") or []
    rules_sorted = sorted(rules, key=lambda r: (-int(r.get("hits") or 0), -(int(r.get("last_hit_ts") or 0))))
    keep = rules_sorted[:MAX_RULES]
    dropped += [f"纪律「{_clip(r.get('text'), 18)}」(hits={r.get('hits')})" for r in rules_sorted[MAX_RULES:]]
    doc["rules"] = _renumber(keep)

    notes = doc.get("user_notes") or []
    live = [n for n in notes if n.get("state") != "disputed"]
    disputed = [n for n in notes if n.get("state") == "disputed"]
    live_sorted = sorted(live, key=lambda n: (-int(n.get("hits") or 0), -(int(n.get("last_hit_ts") or 0))))
    keep_notes = live_sorted[:MAX_NOTES]
    dropped += [f"备注「{_clip(n.get('text'), 18)}」" for n in live_sorted[MAX_NOTES:]]
    doc["user_notes"] = _renumber_notes(keep_notes + disputed)
    return dropped


def _expire_traits(doc):
    """性格判断过期检查：60 天没被再次验证的 trait 降级为 fact 的风格，或标待复核。"""
    changed = []
    now = _now_ts()
    for n in doc.get("user_notes") or []:
        if n.get("kind") != "trait":
            continue
        last = int(n.get("last_hit_ts") or 0)
        if last and (now - last) > TRAIT_EXPIRE_DAYS * 86400 and n.get("state") != "disputed":
            n["state"] = "stale"
            changed.append(f"「{_clip(n.get('text'), 16)}」超过 {TRAIT_EXPIRE_DAYS} 天未验证")
    return changed


# ── 渲染（注入用）────────────────────────────────────

def render():
    """生成常驻块正文。返回 (text, stats)。text 为空表示还没收敛过，不该注入空壳。"""
    doc = load()
    rules = doc.get("rules") or []
    notes = [n for n in (doc.get("user_notes") or [])
             if n.get("state") not in ("disputed",)]
    if not rules and not notes:
        return "", {"rules": 0, "notes": 0, "chars": 0}

    lines = ["【我的行事备忘】"]
    lines.append("这是我从自己的失误里收敛出的纪律，不是知识——动手前过一眼。括号里是栽过的次数。")
    lines.append("")

    if rules:
        for r in rules:
            hits = int(r.get("hits") or 0)
            mark = f"（栽 {hits} 次）" if hits >= 2 else ""
            lines.append(f"{r.get('n')}. {_norm(r.get('text'))}{mark}")
        lines.append("")

    if notes:
        lines.append("关于你（我的观察，可能是错的，可以随时推翻我）：")
        for n in notes:
            since = (n.get("since") or "")[:7]
            tail = f"（据 {since}）" if (n.get("kind") == "trait" and since) else ""
            stale = "（较久未验证）" if n.get("state") == "stale" else ""
            lines.append(f"- {_norm(n.get('text'))}{tail}{stale}")
        lines.append("")

    # 额度兜底：真超了就从头截（理论上前面的裁剪已保证不超，这里是最后一道）
    body = "\n".join(lines)
    if len(body) > BUDGET_CHARS:
        keep = []
        for ln in lines:
            if len("\n".join(keep + [ln])) > BUDGET_CHARS - 1:
                break
            keep.append(ln)
        body = "\n".join(keep)

    return body, {
        "rules": len(rules),
        "notes": len(notes),
        "chars": len(body),
        "version": doc.get("version") or 0,
        "updated_at": doc.get("updated_at") or "",
    }


def stats():
    doc = load()
    text, st = render()
    return {
        "version": doc.get("version") or 0,
        "updated_at": doc.get("updated_at") or "",
        "budget": BUDGET_CHARS,
        "used": len(text),
        "rules": [{"n": r.get("n"), "text": r.get("text"), "hits": r.get("hits"),
                   "id": r.get("id")} for r in (doc.get("rules") or [])],
        "user_notes": [{"n": n.get("n"), "text": n.get("text"), "kind": n.get("kind"),
                        "since": n.get("since"), "state": n.get("state"),
                        "contradictions": n.get("contradictions", 0), "id": n.get("id")}
                       for n in (doc.get("user_notes") or [])],
        "history_count": len(_load_history()),
    }


# ── 手工编辑（用户说"第3条改成XX"走这里）───────────────

def set_rule(text, n=None, hits=None, rule_id=None):
    """新增或修改一条纪律。n = 序号（1 起），rule_id 优先。"""
    doc = load()
    rules = doc.get("rules") or []
    text = _clip(text, RULE_MAX_CHARS)
    if not text:
        return {"error": "内容不能为空"}
    target = None
    if rule_id:
        target = next((r for r in rules if r.get("id") == rule_id), None)
    elif n:
        target = next((r for r in rules if int(r.get("n") or 0) == int(n)), None)
    if target:
        target["text"] = text
        if hits is not None:
            target["hits"] = int(hits)
        doc["rules"] = rules
        ok = save(doc, note=f"手工修改纪律：{_clip(text, 20)}")
        return {"success": True, "id": target.get("id")} if ok else {"error": "写入失败"}
    if len(rules) >= MAX_RULES:
        return {"error": f"纪律已达 {MAX_RULES} 条上限，请先删一条或降低某条的优先级"}
    rules.append({
        "id": "r_" + uuid.uuid4().hex[:8],
        "n": 0, "text": text, "hits": int(hits or 0),
        "evidence": [], "added_at": _now(), "last_hit_ts": _now_ts(),
    })
    doc["rules"] = _renumber(rules)
    ok = save(doc, note=f"手工新增纪律：{_clip(text, 20)}")
    return {"success": True} if ok else {"error": "写入失败"}


def drop_rule(n=None, rule_id=None):
    doc = load()
    rules = doc.get("rules") or []
    before = len(rules)
    if rule_id:
        rules = [r for r in rules if r.get("id") != rule_id]
    elif n:
        rules = [r for r in rules if int(r.get("n") or 0) != int(n)]
    else:
        return {"error": "需要 n 或 rule_id"}
    if len(rules) == before:
        return {"error": "找不到该条纪律"}
    doc["rules"] = _renumber(rules)
    ok = save(doc, note="手工删除纪律")
    return {"success": True} if ok else {"error": "写入失败"}


def set_user_note(text, kind="fact", n=None, note_id=None, evidence=""):
    doc = load()
    notes = doc.get("user_notes") or []
    text = _clip(text, NOTE_MAX_CHARS)
    if not text:
        return {"error": "内容不能为空"}
    kind = "trait" if kind == "trait" else "fact"
    target = None
    if note_id:
        target = next((x for x in notes if x.get("id") == note_id), None)
    elif n:
        target = next((x for x in notes if int(x.get("n") or 0) == int(n)), None)
    if target:
        target["text"] = text
        target["kind"] = kind
        if evidence:
            target["evidence"] = _clip(evidence, 120)
        target["state"] = "active"
        target["contradictions"] = 0
        doc["user_notes"] = notes
        ok = save(doc, note=f"手工修改用户备注：{_clip(text, 18)}")
        return {"success": True, "id": target.get("id")} if ok else {"error": "写入失败"}
    if len([x for x in notes if x.get("state") != "disputed"]) >= MAX_NOTES:
        return {"error": f"用户备注已达 {MAX_NOTES} 条上限，请先删一条"}
    notes.append({
        "id": "u_" + uuid.uuid4().hex[:8], "n": 0, "text": text, "kind": kind,
        "evidence": _clip(evidence, 120), "since": time.strftime("%Y-%m"),
        "state": "active", "contradictions": 0,
        "added_at": _now(), "last_hit_ts": _now_ts(),
    })
    doc["user_notes"] = _renumber_notes(notes)
    ok = save(doc, note=f"手工新增用户备注：{_clip(text, 18)}")
    return {"success": True} if ok else {"error": "写入失败"}


def drop_user_note(n=None, note_id=None):
    doc = load()
    notes = doc.get("user_notes") or []
    before = len(notes)
    if note_id:
        notes = [x for x in notes if x.get("id") != note_id]
    elif n:
        notes = [x for x in notes if int(x.get("n") or 0) != int(n)]
    else:
        return {"error": "需要 n 或 note_id"}
    if len(notes) == before:
        return {"error": "找不到该条备注"}
    doc["user_notes"] = _renumber_notes(notes)
    ok = save(doc, note="手工删除用户备注")
    return {"success": True} if ok else {"error": "写入失败"}


def note_contradiction(text=None, n=None, note_id=None):
    """反向证据通道：观察到跟某条备注不符的行为。

    这是防"标签自我加固"的唯一有效手段 —— 满 2 次自动 disputed 并停止注入。
    """
    doc = load()
    notes = doc.get("user_notes") or []
    target = None
    if note_id:
        target = next((x for x in notes if x.get("id") == note_id), None)
    elif n:
        target = next((x for x in notes if int(x.get("n") or 0) == int(n)), None)
    if not target:
        return {"error": "找不到该条备注"}
    target["contradictions"] = int(target.get("contradictions") or 0) + 1
    if text:
        target.setdefault("counter_evidence", []).append(_clip(text, 80))
    if target["contradictions"] >= 2:
        target["state"] = "disputed"
    doc["user_notes"] = notes
    ok = save(doc, note=f"记录反向证据：{_clip(target.get('text'), 16)}（第 {target['contradictions']} 次）")
    if not ok:
        return {"error": "写入失败"}
    return {
        "success": True,
        "contradictions": target["contradictions"],
        "state": target["state"],
        "note": ("这条已标记为存疑并停止注入，等你裁决" if target["state"] == "disputed"
                 else "已记录。再观察到一次不符就会自动停用这条"),
    }


def _renumber(rules):
    for i, r in enumerate(rules, 1):
        r["n"] = i
    return rules


def _renumber_notes(notes):
    for i, n in enumerate(notes, 1):
        n["n"] = i
    return notes


# ── 自动收敛（从记忆网络的反思碎片 → 短纪律）────────────

def _load_converge_state():
    try:
        raw = get_db().get_meta(CONVERGE_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_converge_state(st):
    try:
        get_db().set_meta(CONVERGE_KEY, json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


_CONVERGE_PROMPT = """你负责维护「我的行事备忘」——从我自己过去的失误记录里，收敛出**可执行的纪律**。

【这是干什么用的】
每条纪律都会被放在我每次对话的最前面常驻。所以它的价值 = 下次真能拦住我犯错。
写成"我学到了要仔细"这种话等于没写——因为下次我依然会觉得"我已经很仔细了"。

【铁律】
1. 只写**触发场景 + 具体动作**。"删改前重新读一遍真实内容"是纪律；"要谨慎"不是纪律。
2. 同义归并。已有的条目如果表达了同一个意思，**不要新增**，把 hits 累加即可。
3. 每条 ≤ 40 字，去掉所有形容词修饰，不许堆叠术语。
4. 只保留**反复出现**的（hits ≥ 2）。只犯过一次的不进常驻区，留在记忆网络里等召回。
5. 纪律总数上限 16 条，用户备注上限 6 条。超了必须淘汰 hits 最低的。

【关于用户的部分】
- fact：可验证的行为事实（"他要求规则由他自己定义"）
- trait：性格/偏好判断（"他偏好先看结论"）。trait 必须能说出依据。
- 两者都写成一句话，≤ 40 字。trait 前面加 [性格]，fact 前加 [事实]。
- 不知道就留空，不许编。

【输出格式】严格如下，没有任何额外文字。没有要改的就输出 UNCHANGED：
<<<纪律>>>39|删改任何东西前先重读真实内容，不凭印象
<<<纪律>>>11|没有工具返回就算没做，说"已改"前先看返回值
<<<用户>>>[事实]9|规则由他定义，反对 AI 自行决定什么算重要
<<<用户>>>[性格]3|偏好先动眼看现状再听方案，不喜欢空谈设计

【现有纪律】（格式：次数|内容，命中次数是累计值，归并同类时把新增的加进去）
{rules}

【现有用户备注】
{notes}

【新增的失误记录】
{fragments}
"""


def _parse_converge(text):
    """解析 LLM 输出。返回 (rules, notes, unchanged)。"""
    text = text or ""
    if "UNCHANGED" in text and "<<" not in text:
        return [], [], True
    rules, notes = [], []
    for m in re.finditer(r"<<<纪律>>>\s*(\d+)?\s*[|｜]\s*(.+)", text):
        hits = int(m.group(1) or 1)
        body = _norm(m.group(2))
        if body and len(body) <= RULE_MAX_CHARS + 8:
            rules.append({"text": body, "hits": hits})
    for m in re.finditer(r"<<<用户>>>\s*(\[\S+?\])?\s*(\d+)?\s*[|｜]\s*(.+)", text):
        kind_raw = (m.group(1) or "").strip("[]")
        hits = int(m.group(2) or 1)
        body = _norm(m.group(3))
        kind = "trait" if ("性格" in kind_raw or "trait" in kind_raw.lower()) else "fact"
        if body and len(body) <= NOTE_MAX_CHARS + 8:
            notes.append({"text": body, "hits": hits, "kind": kind})
    return rules, notes, False


def _semantic_dup(a, b):
    """轻量同义判断：归一化后互相包含，或公共子串过长。

    ⚠️ 只能做**粗筛**，不能指望它合并干净。中文短句里「说"做了"前先查工具调用记录」
    和「说"做了/完成"前先看工具返回值」是同一个意思，但去掉标点后的相似度只有
    ~0.6，阈值放到 0.72 就漏（真库实测漏成 3 条并排，各带 42/30/10 次）。
    阈值再降就会把「先查工具」和「先查数据源」误合。
    ⇒ 真正的语义合并交给 consolidate() 走 LLM，这里只挡"一字之差"的重复。
    """
    import difflib
    a2 = re.sub(r"[，。、；：！？,.;:!?（）()\s“”\"'‘’]", "", a or "")
    b2 = re.sub(r"[，。、；：！？,.;:!?（）()\s“”\"'‘’]", "", b or "")
    if not a2 or not b2:
        return False
    if a2 in b2 or b2 in a2:
        return True
    return difflib.SequenceMatcher(None, a2, b2).ratio() > 0.72


# ── 收敛三步（组 prompt / 调模型 / 归并）────────────────
# 这三步原来全都内联在 maybe_converge() 里。2026-10-26 加入「自我思绪整理」
# （tools/mind_organize.py 的可见化整理会话）后必须抽出来共用 ——
# 否则同一个收敛语义会有两份实现，改一处漏一处是本项目最高频的根因。

def build_converge_prompt(doc, frags):
    """组收敛 prompt。doc = 当前备忘，frags = 本批碎片文本列表。"""
    rules_txt = "\n".join(f"{r.get('hits')}|{r.get('text')}" for r in (doc.get("rules") or [])) or "（空）"
    notes_txt = "\n".join(
        f"[{'性格' if n.get('kind') == 'trait' else '事实'}]{n.get('hits')}|{n.get('text')}"
        for n in (doc.get("user_notes") or []) if n.get("state") != "disputed"
    ) or "（空）"
    frag_txt = "\n".join("- " + f for f in frags)
    return (_CONVERGE_PROMPT
            .replace("{rules}", rules_txt)
            .replace("{notes}", notes_txt)
            .replace("{fragments}", frag_txt))


def converge_call(llm_config, prompt, on_progress=None, cancel_event=None, kind="收敛"):
    """调一次模型做收敛，返回 {"text","thinking","error","usage"}。

    用流式（chat_stream）而不是 chat_once_cached，因为「自我思绪整理」要把
    模型正在写什么实时显示给用户；非流式只能等整轮结束才有东西可看。
    on_progress(text_len, thinking_len, tail) 每 ~0.35s 回调一次。

    kind：记进「非对话用量台账」的分类（整理 / 收敛）。记账在这里做，
    因为它是所有后台收敛调用的**唯一出口** —— 在调用方各记一次，
    早晚会漏一处或者重复记一次。
    """
    try:
        from llm import chat_stream, LLMConfig
        from llm import (EVENT_TEXT_DELTA, EVENT_THINKING_DELTA,
                         EVENT_DONE, EVENT_ERROR)
    except Exception as e:
        return {"text": "", "thinking": "", "error": f"llm 导入失败: {e}", "usage": {}}

    if llm_config is None:
        try:
            cfg_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else _root
            llm_config = LLMConfig.from_config(os.path.join(cfg_dir, "model_config.json"))
        except Exception as e:
            return {"text": "", "thinking": "", "error": f"读取模型配置失败: {e}", "usage": {}}

    # 🔴 max_tokens 必须单独抬高：推理模型的 thinking 和正文共用额度，
    #    默认 8192 会被思考吃光（实测 thinking 占到 2 万字符、正文 0 字）。
    try:
        import copy as _copy
        cfg = _copy.copy(llm_config)
        cfg.max_tokens = max(int(getattr(cfg, "max_tokens", 8192) or 8192), 16000)
    except Exception:
        cfg = llm_config

    msgs = [
        {"role": "system", "content": "你是一个只输出结构化条目的收敛器，不写解释、不写分析过程。"},
        {"role": "user", "content": prompt},
    ]
    text, thinking, usage, err = "", "", {}, ""
    last_emit = 0.0
    try:
        for ev in chat_stream(cfg, msgs, cancel_event=cancel_event):
            et = ev.get("type")
            if et == EVENT_TEXT_DELTA:
                text += ev.get("delta", "") or ""
            elif et == EVENT_THINKING_DELTA:
                thinking += ev.get("delta", "") or ""
            elif et == EVENT_DONE:
                text = ev.get("full_text", text) or text
                thinking = ev.get("full_thinking", thinking) or thinking
                usage = ev.get("usage", {}) or {}
            elif et == EVENT_ERROR:
                err = ev.get("error", "") or "unknown"
                break
            if on_progress:
                now = time.time()
                if now - last_emit > 0.35:
                    last_emit = now
                    try:
                        on_progress(len(text), len(thinking), text[-120:])
                    except Exception:
                        pass
    except Exception as e:
        err = str(e)
    # 记一笔「非任务对话用量」：后台整理/收敛没有 topic，不进任何一场对话的账。
    # 记账本身不许影响主流程（record 内部全 try/except）。
    if usage:
        try:
            from tools import mind_spend
            mind_spend.record(kind, getattr(cfg, "model", "") or "", usage)
        except Exception:
            pass
    return {"text": text, "thinking": thinking, "error": err, "usage": usage}


def apply_converge(doc, new_rules, new_notes, note="自动收敛"):
    """把解析出的条目归并进 doc 并存盘。返回 report。

    防御：新条目超过现有条目数 → 视为模型扩写，整批拒绝。
    （增量收敛时这是允许的，只有收口整理才禁止变多，所以放在调用方判断。）
    """
    rules = doc.get("rules") or []
    added_r, merged_r = [], []
    for nr in new_rules:
        hit = next((r for r in rules if _semantic_dup(r.get("text"), nr["text"])), None)
        if hit:
            hit["hits"] = max(int(hit.get("hits") or 0), int(nr.get("hits") or 1))
            hit["last_hit_ts"] = _now_ts()
            merged_r.append(nr["text"])
        else:
            rules.append({
                "id": "r_" + uuid.uuid4().hex[:8], "n": 0,
                "text": _clip(nr["text"], RULE_MAX_CHARS),
                "hits": int(nr.get("hits") or 1), "evidence": [],
                "added_at": _now(), "last_hit_ts": _now_ts(),
            })
            added_r.append(nr["text"])

    notes = doc.get("user_notes") or []
    added_n, merged_n = [], []
    for nn in new_notes:
        hit = next((x for x in notes if _semantic_dup(x.get("text"), nn["text"])), None)
        if hit:
            hit["hits"] = max(int(hit.get("hits") or 0), int(nn.get("hits") or 1))
            hit["last_hit_ts"] = _now_ts()
            merged_n.append(nn["text"])
        else:
            notes.append({
                "id": "u_" + uuid.uuid4().hex[:8], "n": 0,
                "text": _clip(nn["text"], NOTE_MAX_CHARS), "kind": nn["kind"],
                "evidence": "自动收敛（来源：反思碎片）",
                "since": time.strftime("%Y-%m"), "state": "active",
                "contradictions": 0, "hits": int(nn.get("hits") or 1),
                "added_at": _now(), "last_hit_ts": _now_ts(),
            })
            added_n.append(nn["text"])

    doc["rules"] = _renumber(rules)
    doc["user_notes"] = _renumber_notes(notes)
    expired = _expire_traits(doc)
    dropped = _trim(doc)

    ok = save(doc, note=f"{note}：新增纪律 {len(added_r)}、归并 {len(merged_r)}、"
                        f"用户备注 +{len(added_n)}")
    if not ok:
        return {"error": "写入失败"}
    return {
        "success": True,
        "rules_added": added_r, "rules_merged": merged_r,
        "notes_added": added_n, "notes_merged": merged_n,
        "dropped": dropped, "expired": expired,
    }


_CONSOLIDATE_PROMPT = """你在整理「我的行事备忘」。这份清单会常驻在我每次对话最前面，所以每一条都必须真正能拦住我犯错。

【任务】把下面这份清单**合并整理**，不是重写也不是扩写。

【只准做三件事】
1. **同义合并**：表达同一件事的条目合成一条，次数相加。
   例：「说"做了/完成"前先看工具返回值」和「说"做了"前先查工具调用记录，没调用就不算做」
   是同一条，合并成一条，次数 42+30=72。
2. **删过时的**：描述已废弃机制的直接删。已知废弃项（2026-10-26）：
   - 重要事项不再是全局清单，已降级为「该任务重要的信息」，不再写入 important_matters
   - 任何"写进 important_matters / 全局重要信息"的纪律都已失效
3. **缩短**：每条压到 35 字以内，去掉所有解释和修饰，只留"触发场景 + 具体动作"。

【绝不允许】
- 不许新增原来没有的条目（哪怕你觉得该有）
- 不许把两条不相干的合成一条模糊表述
- 不许写成"要谨慎""要认真"这类没有动作的空话

【关于用户的部分】同样只许合并、删过时的、缩短。区分 [事实]（可验证行为）和 [性格]（推断）。

【输出格式】严格如下，没有任何额外文字。合并后按次数从高到低排：
<<<纪律>>>72|说"做了"前先看工具返回值，没返回就不算做
<<<用户>>>[事实]9|规则由他定义，反对 AI 自行决定什么算重要

【待整理的纪律】
{rules}

【待整理的用户备注】
{notes}
"""


def consolidate(llm_config=None):
    """把现有清单整体收口：合并同义、删过时、压长度。

    为什么必须有这一步：逐批增量收敛时，同一语义会被拆成多条并排
    （真库实测「看工具返回值」拆成 3 条，各带 42/30/10 次），
    额度被重复条目吃光，真正独立的教训反而被 _trim 挤掉。
    字符串相似度在中文短句上判不准，只能交给 LLM 做语义合并。
    """
    doc = load()
    rules = doc.get("rules") or []
    notes = [n for n in (doc.get("user_notes") or []) if n.get("state") != "disputed"]
    if len(rules) < 4 and len(notes) < 3:
        return {"skipped": "条目太少，无需收口"}

    rules_txt = "\n".join(f"{r.get('hits')}|{r.get('text')}" for r in rules) or "（空）"
    notes_txt = "\n".join(
        f"[{'性格' if n.get('kind') == 'trait' else '事实'}]{n.get('hits')}|{n.get('text')}"
        for n in notes) or "（空）"
    prompt = (_CONSOLIDATE_PROMPT.replace("{rules}", rules_txt).replace("{notes}", notes_txt))

    try:
        from llm import chat_once_cached, LLMConfig
        import copy as _copy
        if llm_config is None:
            cfg_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else _root
            llm_config = LLMConfig.from_config(os.path.join(cfg_dir, "model_config.json"))
        cfg = _copy.copy(llm_config)
        cfg.max_tokens = max(int(getattr(cfg, "max_tokens", 8192) or 8192), 16000)
        resp = chat_once_cached(cfg, [
            {"role": "system", "content": "你是一个条目整理器，只输出结构化条目，不写解释、不写分析过程。"},
            {"role": "user", "content": prompt},
        ])
        raw = resp.get("text", "")
        # 这条走的是非流式通道（chat_once_cached），跟 converge_call 是两个出口，
        # 所以这里得单独记一笔 —— 合并到一处就得改 llm.py，那是全链路的事。
        try:
            from tools import mind_spend
            mind_spend.record("整理", getattr(cfg, "model", "") or "", resp.get("usage"))
        except Exception:
            pass
    except Exception as e:
        return {"error": f"LLM 调用失败: {e}"}

    new_rules, new_notes, unchanged = _parse_converge(raw)
    if unchanged:
        return {"unchanged": True}
    if not new_rules:
        return {"error": "整理输出无可解析纪律条目，已放弃（现有清单未改动）",
                "raw_head": _clip(raw, 160)}
    # 安全检查：整理只准变少或持平，突然变多说明模型在扩写，拒绝
    if len(new_rules) > len(rules):
        return {"error": f"整理后条目变多（{len(rules)}→{len(new_rules)}），疑似扩写，已拒绝"}

    old_by_text = {_norm(r.get("text")): r for r in rules}
    out_rules = []
    for nr in new_rules:
        src = old_by_text.get(_norm(nr["text"]))
        out_rules.append({
            "id": (src or {}).get("id") or ("r_" + uuid.uuid4().hex[:8]),
            "n": 0, "text": _clip(nr["text"], RULE_MAX_CHARS),
            "hits": int(nr["hits"] or 1),
            "evidence": (src or {}).get("evidence") or [],
            "added_at": (src or {}).get("added_at") or _now(),
            "last_hit_ts": _now_ts(),
        })

    old_notes = {_norm(n.get("text")): n for n in notes}
    out_notes = []
    for nn in new_notes:
        src = old_notes.get(_norm(nn["text"]))
        out_notes.append({
            "id": (src or {}).get("id") or ("u_" + uuid.uuid4().hex[:8]),
            "n": 0, "text": _clip(nn["text"], NOTE_MAX_CHARS), "kind": nn["kind"],
            "evidence": (src or {}).get("evidence") or "收口整理",
            "since": (src or {}).get("since") or time.strftime("%Y-%m"),
            "state": "active", "contradictions": (src or {}).get("contradictions", 0),
            "hits": int(nn["hits"] or 1),
            "added_at": (src or {}).get("added_at") or _now(),
            "last_hit_ts": _now_ts(),
        })

    doc["rules"] = _renumber(out_rules)
    doc["user_notes"] = _renumber_notes(out_notes + [n for n in (doc.get("user_notes") or [])
                                                   if n.get("state") == "disputed"])
    dropped = _trim(doc)
    ok = save(doc, note=f"收口整理：{len(rules)}→{len(doc['rules'])} 条纪律，"
                        f"{len(notes)}→{len([n for n in doc['user_notes'] if n.get('state') != 'disputed'])} 条备注")
    if not ok:
        return {"error": "写入失败"}
    return {"success": True, "rules": f"{len(rules)}→{len(doc['rules'])}",
            "notes": f"{len(notes)}→{len([n for n in doc['user_notes'] if n.get('state') != 'disputed'])}",
            "dropped": dropped}


def maybe_converge(llm_config=None, force=False):
    """节流式自动收敛。返回 report dict。

    节流两个条件（都为真才跑）：新增碎片 ≥ 8 条；距上次 ≥ 12h。
    理由：这块在恒定区，一变整段前缀缓存失效。不能每轮重写。

    🔴 必须独占：reflect 流程可能被并发触发两次（正常一轮对话 + 手动深度整合）。
       两个线程各自 `_load_converge_state()` 得到同一份 `last_fragment_id`，
       各读一批、各推进、各保存 —— 后保存的那个把前一个的推进**覆盖回旧值**。
       真库实测到这个：`last_run_ts` 是刚刚，`last_fragment_id` 却停在几个小时前，
       而它后面明明还有 100 多条没嚼。表现就是「一直在跑，但一直在嚼同一批」。
       抢不到锁直接跳过 —— 漏一次自动收敛无害，跑重了才要命。
    """
    lock = _converge_lock()
    if not lock.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        return _maybe_converge_locked(llm_config, force)
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _maybe_converge_locked(llm_config=None, force=False):
    state = _load_converge_state()
    if not force:
        last = int(state.get("last_run_ts") or 0)
        if last and (_now_ts() - last) < CONVERGE_MIN_INTERVAL:
            return {"skipped": "interval", "next_in_h": round((CONVERGE_MIN_INTERVAL - (_now_ts() - last)) / 3600, 1)}
    try:
        new_frags = _collect_new_fragments(state)
    except Exception as e:
        return {"error": f"读取碎片失败: {e}"}
    if len(new_frags) < CONVERGE_MIN_NEW and not force:
        return {"skipped": "not_enough_new", "new": len(new_frags)}
    if not new_frags:
        return {"skipped": "no_new"}

    doc = load()
    prompt = build_converge_prompt(doc, new_frags)
    resp = converge_call(llm_config, prompt)
    if resp.get("error"):
        return {"error": f"LLM 调用失败: {resp['error']}"}
    raw = resp.get("text", "")

    new_rules, new_notes, unchanged = _parse_converge(raw)
    if unchanged:
        state["last_run_ts"] = _now_ts()
        state["last_count"] = len(new_frags)
        state["fail_streak"] = 0
        _save_converge_state(state)
        return {"unchanged": True, "new_fragments": len(new_frags)}
    if not new_rules and not new_notes:
        # 解析不出东西 → 不动现有备忘（宁可不变，也不许把已有纪律清空）。
        #
        # 🔴 但失败时**不能无限重试同一批**：如果这批碎片本身就让模型吐不出正文
        #    （例如 thinking 吃光额度），last_fragment_id 不推进就会永久卡死在这个
        #    点位上，后面新碎片永远轮不到。连续失败 3 次就跳过这批并推进基线，
        #    把"卡死"换成"漏一批"，后者至少不阻塞。
        streak = int(state.get("fail_streak") or 0) + 1
        state["fail_streak"] = streak
        if streak >= 3:
            state["last_run_ts"] = _now_ts()
            state["fail_streak"] = 0
            state["skipped_batches"] = int(state.get("skipped_batches") or 0) + 1
            _save_converge_state(state)
            return {"error": f"连续 3 次解析失败，已跳过这批 {len(new_frags)} 条碎片"
                             f"（thinking 占满额度？）", "raw_head": _clip(raw, 160)}
        return {"error": "收敛输出无可解析条目，已放弃本次（现有备忘未改动）",
                "fail_streak": streak, "raw_head": _clip(raw, 160)}

    report = apply_converge(doc, new_rules, new_notes, note="自动收敛")
    if report.get("error"):
        return report
    state["last_run_ts"] = _now_ts()
    state["last_count"] = len(new_frags)
    _save_converge_state(state)
    out = {"new_fragments": len(new_frags)}
    out.update(report)
    return out


def _collect_new_fragments(state):
    """取上次收敛之后新增的 deep/causal 碎片（只取短碎片，长叙事不进收敛）。

    🔴 每批只取 25 条上限 —— 不是因为怕慢，是因为 glm-5.3-flash 这类推理模型
       的 thinking 和正文共用 max_tokens。实测：一次喂 200 条（22k 字）时，
       模型把 20,424 字全写进 thinking，正文返回 0 字，收敛直接失败
       （表现为"收敛输出无可解析条目"，容易被误判成解析正则写错了）。
       40 条首轮能过，但等纪律攒到十几条后归并判断变难，thinking 又顶满；
       25 条是实测能稳定出正文的量级。
    """
    limit_id = int(state.get("last_fragment_id") or 0)
    try:
        rows = get_db()._fetchall(
            "SELECT id, text FROM memory_fragments "
            "WHERE dirty=1 AND source IN ('deep','causal') AND id > ? "
            "AND length(text) BETWEEN 12 AND 200 "
            "ORDER BY id ASC LIMIT 25",
            (limit_id,),
        )
    except Exception:
        rows = []
    out = []
    max_id = limit_id
    for r in rows or []:
        try:
            max_id = max(max_id, int(r["id"]))
            out.append(_norm(r["text"]))
        except Exception:
            continue
    state["last_fragment_id"] = max_id
    return out


def auto_status():
    """后台自动收敛的「情况怎么样」——给设置页可视化用。

    要回答的是三个具体问题，不是"跑过没有"：
      ① 上次什么时候跑的，读了几条，结果怎样（改了 / 没变 / 解析失败）
      ② 它的**素材源**还剩多少料。这条最关键：_collect_new_fragments 只认
         source IN ('deep','causal')，而真库里这批碎片的最后写入停在 2026-08-26，
         之后的新碎片全是 compressed_original / ai_tool 等别的类型。
         只看 last_run_ts 会觉得"一直在正常跑"，实际是在反复嚼两个月前的旧料。
      ③ 还得等多久才会再跑（节流：新增 ≥8 条 且 距上次 ≥12h）。
    """
    state = _load_converge_state()
    last_ts = int(state.get("last_run_ts") or 0)
    base_id = int(state.get("last_fragment_id") or 0)
    now = _now_ts()

    out = {
        "last_run_ts": last_ts,
        "base_id": base_id,
        "last_count": int(state.get("last_count") or 0),
        "fail_streak": int(state.get("fail_streak") or 0),
        "skipped_batches": int(state.get("skipped_batches") or 0),
        "min_new": CONVERGE_MIN_NEW,
        "interval_h": round(CONVERGE_MIN_INTERVAL / 3600, 1),
        "next_at": (last_ts + CONVERGE_MIN_INTERVAL) if last_ts else 0,
        "wait_h": (0 if (not last_ts or now - last_ts >= CONVERGE_MIN_INTERVAL)
                   else round((CONVERGE_MIN_INTERVAL - (now - last_ts)) / 3600, 1)),
        # 素材源：只剩这些还能被收敛读到
        "new_fragments": 0,
        "src_max_id": 0,
        "src_last_at": "",
        "src_total": 0,
        "source_stale_days": 0,
    }
    try:
        rows = get_db()._fetchall(
            "SELECT COUNT(*) AS n, MAX(id) AS m FROM memory_fragments "
            "WHERE dirty=1 AND source IN ('deep','causal') AND id > ?", (base_id,))
        if rows:
            out["new_fragments"] = int(rows[0]["n"] or 0)
            out["src_max_id"] = int(rows[0]["m"] or 0)
        # 素材源最后一次写入 + 总量：用来判断「源头是不是断了」。
        # 这几个数字单独查一次、只算一次，别在下面再查一遍同一个 MAX。
        rows = get_db()._fetchall(
            "SELECT COUNT(*) AS n, MAX(created_at) AS t FROM memory_fragments "
            "WHERE source IN ('deep','causal')")
        if rows:
            out["src_total"] = int(rows[0]["n"] or 0)
            ts = float(rows[0]["t"] or 0)
            if ts:
                out["src_last_at"] = time.strftime("%Y-%m-%d", time.localtime(ts))
                out["source_stale_days"] = int(max(0, (now - ts) // 86400))
    except Exception:
        pass

    # 「这些看不见的活儿一共花了多少」——台账由 mind_spend 记，这里只把合计摊出去。
    # 拿不到（还没记过 / 模块异常）就给 None，前端显示"还没开始记"，
    # 不能用 0 冒充"花了 0 元"。
    try:
        from tools import mind_spend
        out["spend"] = mind_spend.stats()
    except Exception:
        out["spend"] = None

    return out


def reset_baseline():
    """把收敛基线推到当前最新碎片（避免把陈年 279 条一次性全灌进来）。"""
    try:
        rows = get_db()._fetchall(
            "SELECT MAX(id) AS m FROM memory_fragments WHERE dirty=1 AND source IN ('deep','causal')"
        )
        st = _load_converge_state()
        st["last_fragment_id"] = int((rows[0]["m"] if rows else 0) or 0)
        _save_converge_state(st)
        return {"success": True, "baseline": st["last_fragment_id"]}
    except Exception as e:
        return {"error": str(e)}


# ── AI 工具层：用户可以直接在对话里改这份备忘 ────────────

try:
    from tools.registry import register_tool as _register_tool
except Exception:
    _register_tool = None


if _register_tool:

    @_register_tool(
        name="self_memo_review",
        description="查看我的「行事备忘」——那份常驻在每个对话最前面的 ≤1000 字纪律清单。"
                    "返回条目序号、内容、栽过几次、占了多少额度，以及可回滚的历史版本。",
        parameters={"type": "object", "properties": {}},
    )
    def self_memo_review():
        st = stats()
        return {
            "版本": st["version"],
            "更新时间": st["updated_at"],
            "额度": f"{st['used']}/{st['budget']} 字",
            "纪律": st["rules"],
            "关于用户": st["user_notes"],
            "可回滚版本数": st["history_count"],
        }

    @_register_tool(
        name="self_memo_edit",
        description="修改行事备忘。kind=rule 改纪律，kind=user 改关于用户的备注。"
                    "传 n（序号）或 id 表示修改已有条目，都不传表示新增。用户让你"
                    "「把某条改掉/删掉/加上」时用这个，不要自己悄悄改。",
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "rule=行事纪律；user=关于用户的备注"},
                "text": {"type": "string", "description": "条目内容（纪律 ≤52 字，备注 ≤46 字）"},
                "n": {"type": "integer", "description": "序号（1 起），改已有条目时用"},
                "id": {"type": "string", "description": "条目 id，比序号可靠"},
                "hits": {"type": "integer", "description": "栽过的次数（可以不改）"},
                "note_kind": {"type": "string", "description": "kind=user 时用：fact=可验证事实 / trait=性格判断"},
                "action": {"type": "string", "description": "set=新增或修改（默认）；drop=删除"},
            },
            "required": ["kind"],
        },
    )
    def self_memo_edit(kind="rule", text=None, n=None, id=None, hits=None,
                       note_kind="fact", action="set"):
        if kind == "user":
            if action == "drop":
                return drop_user_note(n=n, note_id=id)
            if not text:
                return {"error": "需要 text"}
            return set_user_note(text, kind=note_kind, n=n, note_id=id)
        if action == "drop":
            return drop_rule(n=n, rule_id=id)
        if not text:
            return {"error": "需要 text"}
        return set_rule(text, n=n, hits=hits, rule_id=id)

    @_register_tool(
        name="self_memo_contradict",
        description="用户说「我不是这样」「我早就不这样了」，或我观察到跟某条备注不符的行为时调用。"
                    "记满 2 次该条会自动标为存疑并停止注入，等用户裁决。这是防止我给用户贴错标签的通道。",
        parameters={
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "备注序号（1 起）"},
                "id": {"type": "string", "description": "备注 id"},
                "evidence": {"type": "string", "description": "这次观察到的矛盾事实（一句话）"},
            },
            "required": [],
        },
    )
    def self_memo_contradict(n=None, id=None, evidence=None):
        return note_contradiction(text=evidence, n=n, note_id=id)

    @_register_tool(
        name="self_memo_rollback",
        description="把行事备忘回滚到某个历史版本。不传 version 就退回上一版。每次改动都留了痕。",
        parameters={
            "type": "object",
            "properties": {"version": {"type": "integer", "description": "目标版本号，缺省=返回上一版"}},
            "required": [],
        },
    )
    def self_memo_rollback_tool(version=None):
        ok, msg = rollback(version)
        return {"success": ok, "message": msg}

