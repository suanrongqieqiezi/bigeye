#!/usr/bin/env python3
"""Task management tools — rename tasks, manage workspace."""
import os
import sys
import threading
from .registry import register_tool

# ── 翻阅游标：让 AI 用 more=True 续翻，不必自己算时间戳（2026-10-21 上下文丢失复盘）──
_READ_CURSORS = {}
_READ_CURSOR_LOCK = threading.Lock()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else BASE_DIR


@register_tool(
    name="current_topic",
    description="查看当前活跃任务。返回任务ID、标题、消息数。改名后用它验证是否生效，或不确定当前在哪个任务时调用。",
    parameters={"type": "object", "properties": {}, "required": []}
)
def current_topic():
    try:
        from db import get_db
        from .task_context import get_current_topic
        db = get_db()
        tid = get_current_topic()
        if tid:
            t = db.get_topic(tid)
            title = t["title"] if t else "?"
            msgs = db.message_count(tid)
            return f"当前任务：{tid[:8]} 「{title}」 {msgs}条消息"
        return "没有活跃任务。"
    except Exception as e:
        return f"查询失败：{e}"


@register_tool(
    name="name_task",
    description="给当前活跃任务改名。改名立即生效——调用成功即表示数据库已更新，用 current_topic 可验证。自动识别当前任务，不需传 topic_id。了解了用户需求后、任务方向明确时调用。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "新任务名，简短描述（不超过20字），如'修复文件浏览'"}
        },
        "required": ["name"]
    }
)
def name_task(name: str):
    try:
        from db import get_db
        from .task_context import get_current_topic
        db = get_db()
        tid = get_current_topic()
        if tid:
            db.rename_topic(tid, name)
            return f"当前任务已改名 →「{name}」"
        return "找不到当前活跃任务。用 current_topic 查看。"
    except Exception as e:
        return f"改名失败：{e}"


@register_tool(
    name="list_topics",
    description="列出所有任务（含ID/标题/是否当前）。可按标题关键词筛选。",
    parameters={
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "按标题模糊筛选，如'修复'"},
            "limit": {"type": "integer", "description": "最多返回条数，默认50", "default": 50}
        },
        "required": []
    }
)
def list_topics(filter: str = "", limit: int = 50):
    try:
        from db import get_db
        db = get_db()
        topics = db.list_topics()
        if filter:
            topics = [t for t in topics if filter in (t.get("title") or "")]
        topics = topics[:limit]
        lines = []
        for t in topics:
            tid = t["id"][:8]
            title = t.get("title", "新任务")
            msgs = db.message_count(t["id"])
            lines.append(f"{tid} 「{title}」 {msgs}条消息")
        return f"共 {len(lines)} 个任务：\n" + "\n".join(lines) if lines else "没有匹配的任务。"
    except Exception as e:
        return f"列任务失败：{e}"


@register_tool(
    name="rename_topic",
    description="给指定任务改名。需 topic_id（从 list_topics 获取）和新标题。改名立即写入数据库，返回成功即完成。当前任务改名优先用 name_task（不需传ID）。用 list_topics 或 current_topic 验证结果。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {"type": "string", "description": "任务ID前缀，list_topics 返回的短ID（如'e845'）或完整ID均可"},
            "title": {"type": "string", "description": "新名字，简短描述，如'修文件浏览'"}
        },
        "required": ["topic_id", "title"]
    }
)
@register_tool(
    name="rename_topics_batch",
    description="批量改多个任务名。一次传入多个 {topic_id, title}，比逐个调 rename_topic 高效。返回每个任务的改名结果。",
    parameters={
        "type": "object",
        "properties": {
            "renames": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic_id": {"type": "string", "description": "任务ID前缀，list_topics 返回的短ID"},
                        "title": {"type": "string", "description": "新名字"}
                    },
                    "required": ["topic_id", "title"]
                },
                "description": "改名列表，如 [{\"topic_id\":\"e845\",\"title\":\"修bug\"}]"
            }
        },
        "required": ["renames"]
    }
)
def rename_topic(topic_id: str, title: str):
    try:
        from db import get_db
        db = get_db()
        all_topics = db.list_topics()
        matched = [t for t in all_topics if t["id"].startswith(topic_id)]
        if not matched:
            return f"找不到 id 以 '{topic_id}' 开头的任务。"
        for t in matched:
            db.rename_topic(t["id"], title)
        return f"已改名 {len(matched)} 个任务 → 「{title}」"
    except Exception as e:
        return f"改名失败：{e}"


@register_tool(
    name="read_history_before",
    description="查看被截断的对话历史。传入压缩边界的时间戳（边界标记中提供），返回截断点之前的消息。通常需要先 remember() 记录当前状态再调用。",
    parameters={
        "type": "object",
        "properties": {
            "anchor": {"type": "number", "description": "边界标记中的时间戳，如 1234567890.123"},
            "topic_id": {"type": "string", "description": "任务ID前缀或完整ID。不传=当前任务。从 list_topics 获取"}
        },
        "required": ["anchor"]
    }
)
def read_history_before(anchor: float, topic_id: str = None):
    try:
        from db import get_db
        db = get_db()
        # Resolve topic_id: use provided, else current topic
        if topic_id:
            all_topics = db.list_topics()
            matched = [t for t in all_topics if t["id"].startswith(topic_id)]
            if not matched:
                return f"找不到 id 以 '{topic_id}' 开头的任务。用 list_topics 查看。"
            tid = matched[0]["id"]
        else:
            tid = db.get_meta("active_topic")
            if not tid:
                return "没有活跃任务。"
        rows = db.conn.execute(
            "SELECT role, text, ts FROM messages WHERE topic_id=? AND ts < ? "
            "ORDER BY ts DESC LIMIT 30",
            (tid, anchor)
        ).fetchall()
        if not rows:
            return f"边界 #{str(int(anchor))[-6:]} 之前没有更多消息。"
        lines = [f"[边界 #{str(int(anchor))[-6:]} 之前的对话 — 共 {len(rows)} 条]"]
        for r in reversed(rows):
            role_label = "用户" if r["role"] == "user" else ("AI" if r["role"] == "ai" else r["role"])
            text = r["text"][:200] + ("..." if len(r["text"]) > 200 else "")
            lines.append(f"{role_label}: {text}")
        return "\n".join(lines)
    except Exception as e:
        return f"读取历史失败：{e}"



@register_tool(
    name="get_workspace",
    description="查看当前工作目录的路径和内容。",
    parameters={"type": "object", "properties": {}, "required": []}
)
def get_workspace():
    """列出当前工作区全部文件/目录。不做过滤——隐藏文件、系统目录全显示。"""
    from .file_tools import _safe_path
    base = _safe_path(".")
    items = os.listdir(base)
    # 显示全部，不做硬编码过滤
    dirs = [f"  📁 {d}/" for d in sorted(items) if os.path.isdir(os.path.join(base, d))]
    files = [f"  📄 {f}" for f in sorted(items) if os.path.isfile(os.path.join(base, f))]
    result = f"当前工作区：{base}\n\n"
    if dirs:
        result += "\n".join(dirs) + "\n"
    if files:
        result += "\n".join(files) + "\n"
    result += f"\n共 {len(items)} 个项目"
    return result


@register_tool(
    name="read_topic_messages",
    description="翻阅某个任务的原始对话。默认只返回「人话」（用户消息 + AI 最终回复），自动跳过工具返回、"
               "过程叙述和空消息 —— 一页就是真正的一轮轮对话。"
               "【找某件事】传 query='要找回的那件事'：语义命中相关几轮，不用翻页、不用填任何编号。"
               "一次找不到就把怀疑的几个关键词一起给 search（空格分隔＝任一命中即返回，按命中词数排序），"
               "不要一个词一个词地连着调 —— 每次调用都要重发全部上下文。"
               "【继续翻】传 more=True：服务器记住上次翻到哪，不必自己算时间戳。"
               "【分层】默认每条只给 300 字的「菜单」（够判断这条讲什么），被截断处会标注原文长度；"
               "要看某条全文就把它行首的 #编号原样填进 message_id —— 只为你真正需要的那条付全费。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "任务ID前缀或完整ID，从 list_topics 获取"
            },
            "query": {
                "type": "string",
                "description": "语义检索：一句话描述要找什么，直接返回本任务里最相关的几轮（不翻页、不用编号）"
            },
            "search": {
                "type": "string",
                "description": "关键字过滤。多个词用空格分开＝任一命中（OR），结果按命中词数排序，一次查完不要逐个试"
            },
            "more": {
                "type": "boolean",
                "description": "true = 接着上一页继续翻（服务器记游标，不用填数字）"
            },
            "order": {
                "type": "string",
                "description": "'desc'(默认)=从最新往前翻；'asc'=从最早往后翻"
            },
            "limit": {
                "type": "integer",
                "description": "一页条数，默认 10（整页一次看完，不会被外层截断）",
                "default": 10
            },
            "max_chars": {
                "type": "integer",
                "description": "单条正文上限，默认 300（菜单粒度）。想看长正文用 message_id 取单条，不要靠把这个值调大。",
                "default": 300
            },
            "include_process": {
                "type": "boolean",
                "description": "true = 连 AI 的动作叙述一起看（默认只看人话）"
            },
            "include_tools": {
                "type": "boolean",
                "description": "true = 连工具返回也翻（排查\"那条命令到底打印了什么\"时用，默认不含）"
            },
            "message_id": {
                "type": "string",
                "description": "取某一条全文：把输出里的 #12345 原样复制过来"
            },
            "before_ts": {
                "type": "number",
                "description": "兼容旧调用：翻到此时刻之前"
            }
        },
        "required": ["topic_id"]
    }
)
def read_topic_messages(topic_id: str, query: str = "", search: str = "", more: bool = False,
                        order: str = "desc", limit: int = 10, max_chars: int = 300,
                        include_process: bool = False, include_tools: bool = False,
                        message_id: str = "", before_ts: float = None):
    try:
        import time as _time
        from db import get_db
        db = get_db()
        all_topics = db.list_topics()
        matched = [t for t in all_topics if t["id"].startswith(topic_id)]
        if not matched:
            return f"找不到 id 以 '{topic_id}' 开头的任务。用 list_topics 查看所有任务。"
        tid = matched[0]["id"]
        title = matched[0].get("title", "?")
        short_tid = tid[:8]
        conn = db.conn
        # 页体积纪律（见 server.py _TOOL_INLINE_CAP / _TOOL_INLINE_DEFAULT=4000）：
        #   默认 10 条 × 300 字 ≈ 3000 字 ≈ 3600 tok —— 落在 4000 内框内，永远不会被
        #   head+tail 压成残页（旧版一页 9000 字被压成 1500 字残页，AI 看到的既不是精华也不是全文）。
        #   要看长正文走 message_id 单条取（按需付费），不要靠调大 max_chars —— 那是「全篇读」的老路。
        limit = max(1, min(int(limit or 10), 40))
        max_chars = max(200, min(int(max_chars or 300), 2000))

        # 只认「人话」：用户消息 + AI 最终回复。
        # 旧实现 SQL 只有 WHERE topic_id=?，一页 30 条里 16 条工具 JSON + 12 条空消息，
        # 翻到话题开头要 232 次调用 —— 这是「AI 照做也读不回来」的主因。
        human = (('1=1' if include_tools else "role != 'tool'")
                 + " AND text IS NOT NULL AND trim(text) != ''"
                 + ("" if include_process else
                    " AND (args IS NULL OR args NOT LIKE '%\"process\"%')"))

        def _fmt_ts(ts):
            try:
                return _time.strftime("%m-%d %H:%M", _time.localtime(float(ts)))
            except Exception:
                return ""

        def _clip(txt, mid):
            t = (txt or "").strip()
            if len(t) <= max_chars:
                return t
            # 标注要短：10 条各带 75 字标注就是 750 字，正好把一页顶过 4000 内框。
            # message_id 的用法在工具 schema 里已写明，行内只需给出编号。
            return t[:max_chars] + f"…〔原文{len(t)}字，要全文填 message_id='{mid}'〕"

        # ── 分支 1：取单条全文 ──
        if message_id:
            key = str(message_id).strip().lstrip("#")
            if not key.isdigit():
                return f"message_id 要用输出里的 #编号（纯数字），收到的是 {message_id!r}。"
            row = conn.execute(
                "SELECT id, role, text, ts FROM messages WHERE id=? AND topic_id=?",
                (int(key), tid)).fetchone()
            if not row:
                return f"「{title}」里没有 #{key} 这条消息。"
            lab = "用户" if row["role"] == "user" else ("AI" if row["role"] == "ai" else row["role"])
            body = (row["text"] or "").strip()
            full_len = len(body)
            # 外层内联上限 14000（server.py _TOOL_INLINE_CAP）：超了会被压成 head1000+tail500，
            # 中间整段消失且 AI 不知情。这里主动截到 11000 并明说，比「假装给了全文」诚实。
            _cap = 11000
            note = ""
            if full_len > _cap:
                body = body[:_cap]
                note = (f"\n…（本条原文 {full_len} 字，超出单次回读上限已截到 {_cap} 字；"
                        f"要看中间某段用 search='关键词' 直接定位）…")
            return (f"——「{title}」#{row['id']} [{lab}] {_fmt_ts(row['ts'])} 全文"
                    f"（{full_len} 字）——\n" + body + note)

        total_human = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE topic_id=? AND {human}", (tid,)).fetchone()[0]
        total_all = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE topic_id=?", (tid,)).fetchone()[0]

        # ── 分支 2：语义检索（不翻页、不用编号）──
        if query and query.strip():
            try:
                from memory.msg_vectors import recall_topic_dialogs
                hits = recall_topic_dialogs(tid, query.strip(), top_k=min(limit, 6))
            except Exception as _e:
                hits = []
                print(f"[read_topic_messages] 语义检索不可用: {_e}")
            if not hits:
                # 语义没命中 ≠ 话题里没有：可能是向量索引还没建（新话题/embedder 未加载）。
                # 这里就地用实词做 LIKE 兜底，而不是把 AI 打发回去逐个关键字试 ——
                # 历史上同一话题一分钟连调 7 次 search，就是这么来的。
                import re as _re2
                _kw = [x for x in _re2.findall(r"[A-Za-z][A-Za-z0-9_\-\.]{1,}|[0-9][0-9\-\.]{2,}", query)]
                for _seg in _re2.split(r"[\s，。、,.!?！？:：;；\-—()（）\"'“”‘’/]+", query):
                    _seg = _seg.strip()
                    if 2 <= len(_seg) <= 12:
                        _kw.append(_seg)
                _kw = _kw[:4]
                _fb = []
                if _kw:
                    _ph = " OR ".join(["text LIKE ?"] * len(_kw))
                    _fb = conn.execute(
                        f"SELECT id, role, text, ts FROM messages WHERE topic_id=? AND {human} "
                        f"AND ({_ph}) ORDER BY ts DESC LIMIT ?",
                        tuple([tid] + [f"%{w}%" for w in _kw] + [min(limit, 6)])).fetchall()
                if _fb:
                    lines = [f"——「{title}」语义没命中，但按关键词「{' '.join(_kw)}」找到 {len(_fb)} 条 ——"]
                    for r in _fb:
                        lab = "用户" if r["role"] == "user" else ("AI" if r["role"] == "ai" else r["role"])
                        lines.append(f"[{lab} #{r['id']} {_fmt_ts(r['ts'])}] " + _clip(r["text"], r["id"]))
                    lines.append("")
                    lines.append(f"💭 换一种说法再试 query=，或缩小到某个词用 search=。")
                    return "\n".join(lines)
                return (f"「{title}」里没有和「{query}」相关的内容（语义 + 关键词都没命中）。"
                        f"换一种说法，或保持默认 order 用 more=True 看最近的对话。")
            import re as _re

            def _snippet(txt, mid, probe, span=200):
                """语义召回只给「命中片段」：定位 probe 里最长的可用词，取前后 span 字窗口。
                定位不到就退回按页首截断 —— 绝不整条发出（那是「全篇读」的老路）。"""
                t = (txt or "").strip()
                if len(t) <= span * 2:
                    return t
                pos, word = -1, ""
                for w in sorted([x for x in _re.split(
                        r"[\s，。、,.!?！？:：;；\-—()（）\"'“”‘’]+", probe or "") if len(x) >= 2],
                        key=len, reverse=True):
                    pos = t.find(w)
                    if pos >= 0:
                        word = w
                        break
                if pos < 0:
                    return _clip(t, mid)
                a = max(0, pos - span)
                b = min(len(t), pos + len(word) + span)
                return (("…" if a > 0 else "") + t[a:b] + ("…" if b < len(t) else "")
                        + f"（命中处；本条原文 {len(t)} 字，全文: "
                          f"read_topic_messages(topic_id='{short_tid}', message_id='{mid}')）")

            # 语义命中的总预算：4500 字（≈5400 tok）。
            # 比默认页（≈3000 tok）贵一档是刻意的：它替代的是「同一话题一分钟内连调 7 次
            # 关键字试错 × 每次重发全部上下文」—— 一次给够远比来回翻便宜。
            # 但必须有上界，否则 6 轮 × 3 段又滑回「全篇读」。
            _budget = 4500
            _all_weak = all(float(h.get("lex") or 0) == 0 for h in hits)
            _lines0 = [f"——「{title}」中与「{query}」最相关的 {len(hits)} 轮"
                       f"（按相关度排序，不连续；每段只截命中处前后 200 字）——"]
            if _all_weak:
                _lines0.append("⚠ 这几轮只是语义相近，原文里一个关键词都没对上 —— 未必是你要找的。"
                               "换一种说法，或把怀疑的词写进 search= 更准。")
            _used, _shown = len(_lines0[0]), 0
            for h in hits:
                _blk = [f"[用户 #{h['message_id']} {_fmt_ts(h.get('ts'))}] "
                        + _snippet(h.get("text"), h["message_id"], query)]
                for rep in (h.get("replies") or []):
                    _blk.append(f"[AI #{rep['id']} {_fmt_ts(rep.get('ts'))}] "
                                + _snippet(rep.get("text"), rep["id"], query))
                _sz = sum(len(x) + 1 for x in _blk)
                if _shown and _used + _sz > _budget:
                    break
                _lines0.extend(_blk)
                _used += _sz
                _shown += 1
            lines = _lines0
            if _shown < len(hits):
                lines.append(f"（按体积上限只列了最相关的 {_shown} 轮；"
                             f"缩小 query 或加 search='关键词' 可更精准）")
            lines.append("")
            lines.append(f"💭 本任务共 {total_human} 条人话（另有 {total_all - total_human} 条工具/过程记录）。"
                         f"要看某条原文：message_id='#编号'。")
            return "\n".join(lines)

        # ── 分支 3/4：顺序翻阅。more=True 走服务器游标，AI 不必自己算时间戳 ──
        asc = str(order or "desc").lower() == "asc"
        cur_key = (tid, "asc" if asc else "desc", search or "")
        cur = None
        if before_ts:
            cur = (float(before_ts), -1 if not asc else (1 << 62))
        elif more:
            with _READ_CURSOR_LOCK:
                cur = _READ_CURSORS.get(cur_key)

        # 多词 search：空格/逗号分隔视为 OR，并按「命中几个词」排序。
        # 这是把「一分钟连调 7 次、每次换一个关键字」压成 1 次的关键 ——
        # 每次工具往返都要重发 system + 工具定义（实测 ≈3 万 token）。
        import re as _re3
        _terms = [t for t in _re3.split(r"[\s|,，、]+", (search or "").strip()) if t][:6]
        _hit_expr, _hit_args = "", []
        if _terms:
            _hit_expr = ", (" + " + ".join(["CASE WHEN text LIKE ? THEN 1 ELSE 0 END"] * len(_terms)) + ") AS _hits"
            _hit_args = [f"%{t}%" for t in _terms]
        sql = f"SELECT id, role, text, ts{_hit_expr} FROM messages WHERE topic_id=? AND {human}"
        args = list(_hit_args) + [tid]
        if _terms:
            sql += " AND (" + " OR ".join(["text LIKE ?"] * len(_terms)) + ")"
            args += list(_hit_args)
        if cur:
            op = ">" if asc else "<"
            sql += f" AND (ts {op} ? OR (ts = ? AND id {op} ?))"
            args += [cur[0], cur[0], cur[1]]
        _dir = "ASC" if asc else "DESC"
        _hit_order = "_hits DESC, " if _terms else ""
        sql += f" ORDER BY {_hit_order}ts {_dir}, id {_dir} LIMIT ?"
        args.append(limit + 1)
        rows = conn.execute(sql, tuple(args)).fetchall()

        if not rows:
            with _READ_CURSOR_LOCK:
                _READ_CURSORS.pop(cur_key, None)
            return f"「{title}」已翻到头（本任务共 {total_human} 条人话）。"

        has_more = len(rows) > limit
        rows = rows[:limit]
        kw = (f" · 含关键字「{search}」" + (f"（{len(_terms)} 个词任一命中，按命中数排序）" if len(_terms) > 1 else "")) \
            if search else ""
        # 自限页体积：外层对工具结果有 14000 字符的内联上限（server.py _TOOL_INLINE_CAP），
        # 这里按 13000 装。**必须按游标方向装**（desc 从新到旧、asc 从旧到新）：
        # 超预算时丢掉的是"离游标最远"的几条，游标才停在这一页真正给出的最后一条上。
        picked, picked_lines = [], []
        used = 0
        for r in rows:
            lab = "用户" if r["role"] == "user" else ("AI" if r["role"] == "ai" else r["role"])
            line = f"[{lab} #{r['id']} {_fmt_ts(r['ts'])}] {_clip(r['text'], r['id'])}"
            if picked and used + len(line) + 2 > 13000:
                break
            picked.append(r)
            picked_lines.append(line)
            used += len(line) + 2
        if not picked:
            return (f"「{title}」这条消息太长，单条就超过一页预算；"
                    f"用 message_id='{rows[0]['id']}' 单独取。")
        if len(picked) < len(rows):
            has_more = True
        boundary = picked[-1]
        with _READ_CURSOR_LOCK:
            _READ_CURSORS[cur_key] = (boundary["ts"], boundary["id"])
        body_lines = picked_lines if asc else list(reversed(picked_lines))
        head_line = (f"——「{title}」(tid={short_tid}) 本页 {len(picked)} 条人话{kw}，"
                     f"本任务共 {total_human} 条人话 / {total_all} 条原始记录 ——")
        lines = [head_line] + body_lines
        lines.append("")
        if has_more:
            lines.append(f"📖 还有更多 —— 继续翻: read_topic_messages(topic_id='{short_tid}', more=True"
                         + (", order='asc'" if asc else "") + ")")
            lines.append(f"🎯 更快的找法：read_topic_messages(topic_id='{short_tid}', query='要找回的那件事')"
                         f" —— 直接命中相关几轮，不用逐页翻。")
        else:
            lines.append("（已经翻到头了）")
        return "\n".join(lines)
    except Exception as e:
        return f"读消息失败：{e}"

@register_tool(
    name="get_first_message",
    description="获取当前话题的第一条用户消息。无需参数，直接返回原文。用于回答\"第一句话是什么\"类问题。",
    parameters={"type": "object", "properties": {}, "required": []}
)
def get_first_message():
    try:
        from db import get_db
        from .task_context import get_current_topic
        db = get_db()
        # 归属优先走任务上下文通道；全局活跃任务仅作无绑定兜底
        tid = get_current_topic() or db.get_meta("active_topic")
        if not tid:
            return "没有活跃任务。"
        rows = db.conn.execute(
            "SELECT text FROM messages WHERE topic_id=? AND role='user' AND text != '' "
            "ORDER BY ts ASC LIMIT 1",
            (tid,)
        ).fetchall()
        if rows:
            return f"第一条用户消息：\"{rows[0]['text']}\""
        return "未找到用户消息。"
    except Exception as e:
        return f"查询失败：{e}"

@register_tool(
    name="get_topic_tree",
    description="查看一个话题的碎片树。传入 topic_id 或留空查看当前话题。显示碎片之间的父子关系，像文件夹一样展开。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "话题ID前8位。留空则查看当前活跃话题。"
            }
        },
        "required": []
    }
)
def get_topic_tree(topic_id: str = ""):
    import os as _os
    from memory.fragment_store import FragmentStore
    store = FragmentStore()

    if not topic_id:
        topic_id = _os.environ.get("DAEYE_TOPIC_ID", "")
    if not topic_id:
        try:
            from .task_context import get_current_topic
            tid = get_current_topic()
            if tid:
                topic_id = tid
        except Exception:
            pass
    if not topic_id:
        return "不知道当前话题。传 topic_id 或用 current_topic 查看。"

    fragments = store.recall_by_topic(topic_id, limit=50)
    if not fragments:
        return "这个话题下没有碎片。"

    by_parent = {}
    roots = []
    for f in fragments:
        pid = f.get("parent_id")
        if pid:
            by_parent.setdefault(pid, []).append(f)
        else:
            roots.append(f)

    def render_node(f, indent=0):
        prefix = "  " * indent + ("\u251c\u2500" if indent > 0 else "")
        ts = f.get("ts", "")[:12]
        source_tag = {"task_root": "\U0001f331", "context_wall": "\U0001f9f1", "reflection": "\U0001f914", "impression": "\u26a1"}.get(f.get("source", ""), "\U0001f4dd")
        lines = [f"{prefix}{source_tag} [{ts}] {f['text'][:80]}"]
        children = by_parent.get(f["id"], [])
        children.sort(key=lambda c: c.get("ts", ""))
        for child in children:
            lines.extend(render_node(child, indent + 1))
        return lines

    roots.sort(key=lambda r: r.get("ts", ""))
    result = []
    for root in roots:
        result.extend(render_node(root))

    hint = "\n\u2500\u2500\n\u7528 trace_memory(id) \u8ffd\u6eaf\u56e0\u679c\u94fe\uff0c\u7528 discover_tools('task_management') \u83b7\u53d6 read_history_before \u7b49\u5386\u53f2\u7ffb\u9605\u5de5\u5177\u3002"
    return "\n".join(result) + hint

@register_tool(
    name="continue_task",
    description="告诉系统\"我还没干完，继续给我轮次\"。任务未完成但暂时不需要其他工具时调用，系统不会停掉你。",
    parameters={"type": "object", "properties": {}, "required": []}
)
def continue_task():
    return {"continued": True, "msg": "继续执行。"}


@register_tool(
    name="find_empty_tasks",
    description=(
        "扫描所有任务，找出工作区没有文件的任务。"
        "判定标准：工作区目录不存在，或目录存在但里面没有任何文件（含子目录里的文件）。"
        "用于清理僵尸任务、整理任务列表。返回空任务列表含 tid/标题/原因，可用 delete_task 删除。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_no_workspace": {
                "type": "boolean",
                "description": "是否包含从未设置工作区的任务（默认true）",
                "default": True
            }
        },
        "required": []
    }
)
def find_empty_tasks(include_no_workspace: bool = True):
    try:
        from db import get_db
        import shutil
        db = get_db()
        topics = db.list_topics()
        if not topics:
            return "没有任务。"

        empty = []
        non_empty = 0
        for t in topics:
            tid = t["id"]
            title = t.get("title", "新任务")
            ws = db.get_topic_meta(tid, "workspace")

            # 解析工作区路径
            ws_path = None
            if ws:
                ws_path = ws
            else:
                # 默认工作区：data/missions/{tid}/workspace
                default_ws = os.path.join(ROOT_DIR, "data", "missions", tid, "workspace")
                if os.path.isdir(default_ws):
                    ws_path = default_ws

            reason = ""
            if not ws_path and not ws:
                if include_no_workspace:
                    reason = "未设置工作区，且默认目录不存在"
                else:
                    non_empty += 1
                    continue
            elif not ws_path:
                if include_no_workspace:
                    reason = "工作区路径为空"
                else:
                    non_empty += 1
                    continue
            elif not os.path.isdir(ws_path):
                reason = f"工作区目录不存在: {ws_path}"
            else:
                # 统计文件数（含子目录）
                file_count = 0
                for root, dirs, files in os.walk(ws_path):
                    file_count += len(files)
                    if file_count > 0:
                        break
                if file_count == 0:
                    reason = f"工作区空（无文件）: {ws_path}"
                else:
                    non_empty += 1
                    continue

            if reason:
                empty.append({
                    "tid": tid[:8],
                    "full_tid": tid,
                    "title": title,
                    "reason": reason,
                    "workspace": ws_path or "(无)",
                    "msgs": db.message_count(tid),
                })

        if not empty:
            return f"扫描了 {len(topics)} 个任务，没有空工作区任务（{non_empty} 个有文件）。"

        lines = [f"扫描了 {len(topics)} 个任务，发现 {len(empty)} 个空工作区任务（{non_empty} 个有文件）："]
        for e in empty:
            lines.append(f"  • [{e['tid']}] 「{e['title']}」 {e['msgs']}条消息")
            lines.append(f"      原因: {e['reason']}")
        lines.append("")
        lines.append("💡 用 delete_task(topic_id) 删除。记忆碎片会保留（AI 经验不丢）。")
        return "\n".join(lines)
    except Exception as e:
        return f"扫描失败：{e}"


@register_tool(
    name="delete_task",
    description=(
        "删除一个任务。会清理：对话历史、DAG、工作区目录、任务元数据。"
        "记忆碎片保留（AI 经验不随任务删除而丢失）。"
        "先 find_empty_tasks 扫描，再用此工具删除空任务。"
        "删当前活跃任务会自动切到下一个任务。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "任务ID前缀（如'e845'）或完整ID。从 find_empty_tasks / list_topics 获取。"
            },
            "confirm": {
                "type": "boolean",
                "description": "确认删除。默认false（只预览要删什么）。设true才真删。",
                "default": False
            }
        },
        "required": ["topic_id"]
    }
)
def delete_task(topic_id: str, confirm: bool = False):
    try:
        from db import get_db
        import shutil
        db = get_db()

        # 解析短ID → 完整ID
        all_topics = db.list_topics()
        matched = [t for t in all_topics if t["id"].startswith(topic_id)]
        if not matched:
            return f"找不到 id 以 '{topic_id}' 开头的任务。用 list_topics 查看所有任务。"
        if len(matched) > 1:
            return f"有多个任务以 '{topic_id}' 开头，请传更长的前缀。"
        tid = matched[0]["id"]
        title = matched[0].get("title", "?")

        # 收集要删的东西
        ws = db.get_topic_meta(tid, "workspace")
        default_ws = os.path.join(ROOT_DIR, "data", "missions", tid, "workspace")
        mission_dir = os.path.join(ROOT_DIR, "data", "missions", tid)
        ws_to_delete = ws if ws else default_ws
        msgs = db.message_count(tid)

        # 检查 task_instances
        has_task_instance = False
        try:
            row = db._fetchone("SELECT id, status FROM task_instances WHERE id=? OR topic_id=?", (tid, tid))
            if row:
                has_task_instance = True
        except Exception:
            pass

        # 检查记忆碎片（不删，只报告）
        memory_count = 0
        try:
            row = db._fetchone(
                "SELECT COUNT(*) as c FROM memory_fragments WHERE topic_id=? AND dirty=1",
                (tid,)
            )
            if row:
                memory_count = row["c"]
        except Exception:
            pass

        # 预览模式
        if not confirm:
            lines = [
                f"📋 删除预览 ——「{title}」(tid={tid[:8]})",
                f"  对话消息: {msgs} 条 → 删除",
                f"  工作区目录: {ws_to_delete} → {'删除' if os.path.isdir(ws_to_delete) else '不存在'}",
                f"  任务目录: {mission_dir} → {'删除' if os.path.isdir(mission_dir) else '不存在'}",
                f"  DAG 任务: {'有' if has_task_instance else '无'} → {'删除' if has_task_instance else '-'}",
                f"  记忆碎片: {memory_count} 条 → 保留（AI 经验不丢）",
                f"  meta 键: workspace/allow_outside/working → 删除",
                "",
                "⚠️ 这是不可恢复操作。确认删除请调 delete_task(topic_id, confirm=true)",
            ]
            return "\n".join(lines)

        # 执行删除
        deleted = []
        # 1. 删工作区目录 + 任务目录
        if os.path.isdir(mission_dir):
            try:
                shutil.rmtree(mission_dir)
                deleted.append(f"任务目录 {mission_dir}")
            except Exception as e:
                deleted.append(f"任务目录删除失败: {e}")
        elif os.path.isdir(ws_to_delete):
            try:
                shutil.rmtree(ws_to_delete)
                deleted.append(f"工作区目录 {ws_to_delete}")
            except Exception as e:
                deleted.append(f"工作区目录删除失败: {e}")

        # 2. 删 task_instances / task_nodes
        if has_task_instance:
            try:
                db._execute("DELETE FROM task_nodes WHERE task_id=?", (tid,))
                db._execute("DELETE FROM task_instances WHERE id=? OR topic_id=?", (tid, tid))
                db._commit()
                deleted.append("DAG 任务数据")
            except Exception as e:
                deleted.append(f"DAG 数据删除失败: {e}")

        # 3. 删 meta 键
        for key in ("workspace", "allow_outside", "working"):
            try:
                db._execute("DELETE FROM meta WHERE key=?", (f"{key}_{tid}",))
                db._commit()
            except Exception:
                pass
        deleted.append("meta 键")

        # 4. 删 messages + topic（db.delete_topic）
        db.delete_topic(tid)
        deleted.append(f"对话消息 {msgs} 条 + 任务记录")

        # 5. 如果删的是当前活跃任务，清空 active_topic
        active = db.get_active_topic_id()
        if active == tid:
            remaining = db.list_topics()
            if remaining:
                db.set_active_topic_id(remaining[0]["id"])
                new_active = f"，已切换到「{remaining[0].get('title','?')}」"
            else:
                db._execute("DELETE FROM meta WHERE key='active_topic'")
                db._commit()
                new_active = "（无剩余任务）"
        else:
            new_active = ""

        lines = [
            f"✅ 已删除「{title}」(tid={tid[:8]})",
        ] + [f"  • {d}" for d in deleted]
        lines.append(f"  • 记忆碎片 {memory_count} 条已保留{new_active}")
        return "\n".join(lines)
    except Exception as e:
        return f"删除失败：{e}"
