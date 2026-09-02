#!/usr/bin/env python3
"""Memory tools for AI — wraps the memory system as AI-callable tools."""
from .registry import register_tool
import sys
import os
import time
import json

# Lazy import to avoid circular dependency
_memory_store = None
_memory_enabled = False

def _ensure_memory():
    global _memory_store, _memory_enabled
    if _memory_store is None:
        try:
            from memory.fragment_store import get_store
            _memory_store = get_store()
            _memory_enabled = True
        except Exception:
            _memory_enabled = False


@register_tool(
    name="remember",
    description="写入记忆，第一人称，一句话。支持 epistemic 类型：experience/opinion/world。",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "用第一人称写，不超过一句话。要具体。"
            },
            "tags": {
                "type": "string",
                "description": "逗号分隔的标签，如 '错误,爬虫,教训'"
            },
            "epistemic": {
                "type": "string",
                "enum": ["experience", "opinion", "world"],
                "description": "认识论类型：experience=经验教训, opinion=判断观点, world=外部事实。默认 experience。"
            },
            "crystal_parent": {
                "type": "string",
                "description": "派生此记忆的上层晶体 id（思维血统），可选。"
            },
            "raw_source": {
                "type": "string",
                "description": "原始素材 id（如触发这次记忆的任务上下文片段），可选。"
            }
        },
        "required": ["text"]
    }
)
def remember(text: str, tags: str = "", topic_id: str = None, parent_id: int = None, link_type: str = None,
             epistemic: str = "experience", crystal_parent: str = None, raw_source: str = None):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        import time
        ts = time.strftime("%Y%m%d%H%M%S")
        # CMN P3: 传入 CMN 字段
        # 注意：authority_level 不接受 AI 自填，只能由反思回路提拔（P4）

        # 透明桥接：如果 AI 之前读过文件，自动把 raw_source 指向对应切片
        # AI 完全无感——它只是 remember 了一条经验，系统在背后把桥建好
        if raw_source is None:
            try:
                from tools.crystal_session import get_current_slice_id
                auto_slice_id = get_current_slice_id()
                if auto_slice_id:
                    raw_source = auto_slice_id
            except Exception:
                pass

        kwargs = dict(
            ts=ts, source="ai_tool", tags=tags,
            topic_id=topic_id, parent_id=parent_id, link_type=link_type,
            epistemic=epistemic,
            crystal_parent_id=crystal_parent,
            raw_source_id=raw_source,
        )
        fid = _memory_store.add(text, **kwargs)
        bridge_msg = ""
        if raw_source:
            bridge_msg = f" 桥接切片: {raw_source}"
        return "记下了。" + (f" 标签: {tags}" if tags else "") + f" 类型: {epistemic}{bridge_msg}"
    except Exception as e:
        return f"记的时候出了问题：{e}"


@register_tool(
    name="remember_knowledge",
    description="存一段你对某个主题的成体系理解。和 remember（记一句话碎片）不同——"
                "当你觉得对这个领域已经想明白了、有完整认知时用这个。"
                "存的是结构化知识，不是零散经验。对话时系统会自然想到它。",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "你对这个主题的完整理解。可以写长一点，把结论、原因、要点都讲清楚。"
            },
            "topic": {
                "type": "string",
                "description": "主题标识，1-3个词，如 '蓝牙HID开发' 'SQLite锁库排查'。用于关联相关碎片。"
            },
            "epistemic": {
                "type": "string",
                "enum": ["experience", "opinion", "world"],
                "description": "认识论类型：experience=经验总结, opinion=判断观点, world=外部事实。默认 experience。",
                "default": "experience"
            }
        },
        "required": ["text", "topic"]
    }
)
def remember_knowledge(text: str, topic: str, epistemic: str = "experience", **_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        import time
        ts = time.strftime("%Y%m%d%H%M%S")

        # 透明桥接：如果 AI 之前读过文件，自动把 raw_source 指向对应切片
        raw_source = None
        try:
            from tools.crystal_session import get_current_slice_id
            auto_slice_id = get_current_slice_id()
            if auto_slice_id:
                raw_source = auto_slice_id
        except Exception:
            pass

        fid = _memory_store.add(
            text,
            ts=ts,
            source="ai_knowledge",
            tags=topic,
            epistemic=epistemic,
            importance=7.0,
            layer="knowledge",
            raw_source_id=raw_source,
        )
        if not fid:
            return "内容太短或存的时候出了问题。"

        # 即时关联同主题的 core 碎片（不等反思回路）
        linked = 0
        try:
            linked = _memory_store.link_cores_to_knowledge(fid, topic_tag=topic)
        except Exception:
            pass

        msg = f"知识已存入。主题: {topic}，id: {fid}"
        if linked:
            msg += f"，关联了 {linked} 条相关碎片"
        if raw_source:
            msg += f"，桥接切片: {raw_source}"
        return msg
    except Exception as e:
        return f"存知识的时候出了问题：{e}"


@register_tool(
    name="edit_memory",
    description="修改一条记忆的内容或标签。先 crystal_recall 查 id。",
    parameters={
        "type": "object",
        "properties": {
            "fragment_id": {
                "type": "integer",
                "description": "记忆碎片 ID"
            },
            "text": {
                "type": "string",
                "description": "新的记忆内容（不改就留空）"
            },
            "tags": {
                "type": "string",
                "description": "新的标签，逗号分隔（不改就留空）"
            }
        },
        "required": ["fragment_id"]
    }
)
def edit_memory(fragment_id: int, text: str = "", tags: str = "", **_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        kwargs = {}
        if text:
            kwargs["text"] = text
        if tags:
            kwargs["tags"] = tags
        if not kwargs:
            return "没有要修改的内容。"
        ok = _memory_store.edit(fragment_id, **kwargs)
        return "改好了。" if ok else "没改成，id 可能不对。"
    except Exception as e:
        return f"改的时候出了问题：{e}"


@register_tool(
    name="forget",
    description="忘了它。有记忆是错的、过时了——删掉。先用 crystal_recall 查 id。",
    parameters={
        "type": "object",
        "properties": {
            "fragment_id": {
                "type": "integer",
                "description": "要忘掉的记忆 ID。先 crystal_recall 查一下。"
            }
        },
        "required": ["fragment_id"]
    }
)
def forget(fragment_id: int, **_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        _memory_store.forget(fragment_id)
        return "忘了。"
    except Exception as e:
        return f"忘不掉：{e}"


@register_tool(
    name="reinforce",
    description="加深某个人、某件事的印象，以后更容易想起来。",
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "什么值得加深——人名、事件、话题。"
            }
        },
        "required": ["keyword"]
    }
)
def reinforce(keyword: str, **_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        n = _memory_store.reinforce(keyword)
        return f"加深了 {n} 条关于 '{keyword}' 的记忆。"
    except Exception as e:
        return f"没加深成：{e}"


@register_tool(
    name="trace_memory",
    description="追溯记忆因果链。给 fragment_id 精准追，或给 keyword 自动找最相关的再追。",
    parameters={
        "type": "object",
        "properties": {
            "fragment_id": {
                "type": "integer",
                "description": "记忆碎片 ID。从 crystal_recall 返回的 id= 数字。与 keyword 二选一。"
            },
            "keyword": {
                "type": "string",
                "description": "关键词（如'蓝牙重连'）。自动找最相关的一条记忆再追因果链。与 fragment_id 二选一。"
            }
        },
        "required": []
    }
)
def trace_memory(fragment_id: int = None, keyword: str = "", **_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        # 二选一逻辑：fragment_id 优先，否则用 keyword 找
        if fragment_id is None and not keyword.strip():
            return "给个 fragment_id 或 keyword 吧，二选一。"
        if fragment_id is None:
            # 用关键词找最相关的一条——优先文本搜索（能捞到已归档的素材碎片）
            # recall() 走 dirty=1 过滤，归档碎片追不到；这里要追溯完整因果链，必须带归档
            text_results = _memory_store.search_by_text(keyword, limit=5, include_archived=True)
            if not text_results:
                # 降级到向量召回（只看活着的记忆）
                fragments = _memory_store.recall(keyword, top_k=1, threshold=0.3)
                if not fragments:
                    return f"想不起来 '{keyword}' 相关的事，没法追。"
                text_results = fragments
            fragment_id = text_results[0]["id"]
        # 追溯因果链时带归档碎片——story 整理后素材碎片 dirty=0，但仍是记忆的一部分
        chain = _memory_store.get_chain(fragment_id, include_archived=True)
        if not chain:
            return f"找不到 id={fragment_id} 的记忆链。"
        lines = [f"追溯 id={fragment_id} 的记忆链（共 {len(chain)} 条）："]
        for f in chain:
            ts = f.get("ts", "")
            lt = f.get("link_type") or ""
            link_mark = ""
            if lt == "cause":
                link_mark = " →导致→"
            elif lt == "similar":
                link_mark = " ~类似~"
            elif lt == "reverse":
                link_mark = " ←被引←"
            tid_f = f.get("topic_id") or ""
            topic_tag = f" [topic={tid_f[:8]}]" if tid_f else ""
            archived = " 📦归档" if f.get("dirty") == 0 else ""
            layer = f.get("layer", "core")
            layer_mark = ""
            if layer == "story":
                layer_mark = " 📖故事"
            elif layer == "knowledge":
                layer_mark = " 📚知识"
            lines.append(f"[{ts[:8]} {ts[8:12]} id={f['id']}{link_mark}{topic_tag}{layer_mark}{archived}] {f['text']}")
        return "\n".join(lines)
    except Exception as e:
        return f"追溯的时候卡住了：{e}"


@register_tool(
    name="build_self_narrative",
    description="翻看所有记忆，从头到尾回顾自己经历过什么、是个什么样的人。有感触就记下来。",
    parameters={
        "type": "object",
        "properties": {},
        "required": []
    }
)
def build_self_narrative(**_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        fragments = _memory_store.get_all(limit=200)
        if not fragments:
            return "脑子里一片空白，什么都想不起来。"
        lines = [f"翻阅了 {len(fragments)} 条记忆："]
        for f in fragments:
            ts = f.get("ts", "")
            tid_f = f.get("topic_id") or ""
            topic_tag = f" [topic={tid_f[:8]}]" if tid_f else ""
            lines.append(f"[{ts[:8]} {ts[8:12]} id={f['id']}{topic_tag}] {f['text']}")
        return "\n".join(lines)
    except Exception as e:
        return f"翻记忆的时候卡住了：{e}"


@register_tool(
    name="recall_by_topic",
    description="翻阅任务下的记忆碎片。返回碎片含 id/parent_id/link_type，用 trace_memory 追溯。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "任务ID前缀或完整ID，从 list_topics 获取。如 '0a8e' 或 '0a8e937309d6'"
            },
            "limit": {
                "type": "integer",
                "description": "返回条数，默认20",
                "default": 20
            }
        },
        "required": ["topic_id"]
    }
)
def recall_by_topic(topic_id: str, limit: int = 20):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        from db import get_db
        db = get_db()
        all_topics = db.list_topics()
        matched = [t for t in all_topics if t["id"].startswith(topic_id)]
        if not matched:
            return f"找不到 id 以 '{topic_id}' 开头的任务。用 list_topics 查看所有任务。"
        tid = matched[0]["id"]
        title = matched[0].get("title", "?")
        fragments = _memory_store.recall_by_topic(tid, limit=limit)
        if not fragments:
            return f"「{title}」(tid={tid[:8]}) 下没有记忆碎片。"
        lines = [f"——「{title}」(tid={tid[:8]}) 的记忆碎片，共 {len(fragments)} 条 ——"]
        for f in fragments:
            pid = f.get("parent_id") or ""
            lt = f.get("link_type") or ""
            link = f" ←[{lt}]{pid}" if lt and pid else (f" ←{pid}" if pid else "")
            w = f" ⭐{f['weight']:.1f}" if f.get("weight") else ""
            lines.append(f"[id={f['id']}{link}] [{f.get('ts','')[:8]} {f.get('ts','')[8:12]}]{w} {f['text']}")
        return "\n".join(lines)
    except Exception as e:
        return f"翻碎片时出错：{e}"


@register_tool(
    name="expand_compressed",
    description="展开已整理消息或整理节点。支持按锚点展开单条消息，或按节点ID逐层展开整理树。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "对话ID。不传则自动取当前话题。",
            },
            "anchor": {
                "type": "string",
                "description": "消息锚点编号（从0开始的字符串）。与传统整理消息配合使用。",
            },
            "node_id": {
                "type": "integer",
                "description": "整理树节点ID。展开该节点及其子节点到指定深度。与 anchor 二选一。",
            },
            "depth": {
                "type": "integer",
                "description": "展开深度。0=仅摘要, 1=含直接子节点, 2=含孙节点… 默认1。仅 node_id 模式有效。",
            },
        },
        "required": [],
    },
)
def expand_compressed(topic_id: str = "", anchor: str = "",
                      node_id: int = 0, depth: int = 1):
    """展开已整理消息或整理树节点。

    两种模式：
    1. anchor 模式（传统）：展开单条已整理消息的原文
    2. node_id 模式（嵌套）：展开整理树节点到指定深度，显示树形导航
    """
    try:
        from server import get_compressed_original
        from db import get_db

        db = get_db()
        _ensure_ct_table(db)

        if not topic_id:
            topic_id = db.get_active_topic_id() or ""

        # ── Mode 2: node_id expansion (nested tree) ──
        if node_id > 0:
            return _expand_by_node(db, topic_id, node_id, depth)

        # ── Mode 1: anchor expansion (traditional) ──
        if anchor:
            return _expand_by_anchor(topic_id, anchor)

        return "请指定 anchor（展开单条消息）或 node_id（展开整理树节点）。"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"展开失败：{e}"


def _expand_by_anchor(topic_id: str, anchor: str) -> str:
    """Traditional single-message expansion by anchor string."""
    from server import get_compressed_original
    original = get_compressed_original(topic_id, anchor)
    if original:
        return f"📖 展开已整理消息 (anchor={anchor}):\n\n{original}"
    from memory.fragment_store import get_store
    store = get_store()
    if store:
        fragments = store.recall(
            f"compressed:{topic_id}:{anchor}",
            top_k=3, threshold=0.0, layer="core",
        )
        for f in fragments or []:
            tags = (f.get("tags") or "").split(",")
            if f"compressed:{topic_id}:{anchor}" in tags:
                return f"📖 展开已整理消息 (anchor={anchor}):\n\n{f['text']}"
    return f"未找到已整理消息 (topic={topic_id}, anchor={anchor})。"


def _expand_by_node(db, topic_id: str, node_id: int, depth: int) -> str:
    """Expand a compression tree node to the specified depth."""
    row = db._fetchone(
        "SELECT * FROM compression_tree WHERE id = ? AND topic_id = ?",
        (node_id, topic_id)
    )
    if not row:
        return f"未找到整理节点 #{node_id} (topic={topic_id})。"

    node = dict(row)
    lines = []

    # Header
    depth_label = f"L{node['depth']}"
    lines.append(
        f"📂 整理节点 #{node['id']} [{depth_label}] "
        f"范围 [{node['anchor_start']}-{node['anchor_end']}] "
        f"({node['item_count']}项)"
    )
    lines.append(f"━" * 50)
    lines.append(node["summary_text"])
    lines.append("")

    # Children
    if depth > 0 and node["depth"] > 0:
        children = db._fetchall(
            """SELECT * FROM compression_tree
               WHERE parent_id = ? AND topic_id = ?
               ORDER BY anchor_start""",
            (node_id, topic_id)
        )
        children = [dict(c) for c in children]

        if children:
            lines.append(f"📋 内含 {len(children)} 个子整理（展开深度 {depth}）：")
            lines.append("")
            for i, child in enumerate(children):
                is_last = (i == len(children) - 1)
                connector = "└──" if is_last else "├──"
                child_label = f"L{child['depth']}"
                lines.append(
                    f"  {connector} #{child['id']} [{child_label}] "
                    f"[{child['anchor_start']}-{child['anchor_end']}] "
                    f"({child['item_count']}项)"
                )
                # Indented summary preview
                indent = "   " if is_last else "  │"
                preview = child["summary_text"][:120].replace("\n", " ").strip()
                lines.append(f"  {indent} {preview}...")

                if depth > 1:
                    # Recurse into child
                    child_expanded = _expand_by_node(
                        db, topic_id, child["id"], depth - 1
                    )
                    # Indent child output
                    for cl in child_expanded.split("\n"):
                        if cl.strip():
                            lines.append(f"  {indent} {cl}")
                else:
                    lines.append(
                        f"  {indent} ↳ expand_compressed(node_id={child['id']}, depth=1) 展开"
                    )
                lines.append("")

    return "\n".join(lines)
def fold_message(topic_id: str, anchor: str = "", summary: str = ""):  # 内部函数：供 compress_context 单条场景复用（已从工具列表隐藏）
    """整理一条对话消息，立即生效。AI 调完就能在上下文里看到整理版。

    不给 anchor 则自动找最旧未整理的用户/AI 消息。
    """
    try:
        from server import (_auto_compress, PER_MESSAGE_COMPRESS_CHARS,
                           _store_compressed_original, _llm_summarize)
        from db import get_db
        from memory.fragment_store import get_store
        db = get_db()

        # ── Resolve anchor: find target message ──
        raw = db.get_messages(topic_id, limit=200)
        target = None
        # 全局 anchor：话题内非tool消息的全局序号（窗口起点偏移 = 总非tool数 - 窗口内非tool数）
        try:
            _trow = db._fetchone(
                "SELECT COUNT(*) AS c FROM messages WHERE topic_id=? AND role!='tool'",
                (topic_id,))
            _total_nt = _trow["c"] if _trow else 0
        except Exception:
            _total_nt = 0
        _win_nt = sum(1 for m in raw if m.get("role") != "tool")
        anchor_idx = max(_total_nt - _win_nt, 0)

        if anchor:
            idx = int(anchor)
            count = anchor_idx  # 从全局基准起数（anchor 参数语义=全局序号）
            for m in raw:
                role = m.get("role", "")
                if role == "tool":
                    continue
                if count == idx:
                    target = m
                    break
                count += 1
        else:
            for m in raw:
                role = m.get("role", "")
                if role == "tool":
                    continue
                text = m.get("text", "")
                if text and not (text.startswith("[已压缩") or text.startswith("[已整理")):
                    target = m
                    break
                anchor_idx += 1

        if not target:
            return f"找不到要整理的消息（anchor={anchor}）。"

        text = target.get("text", "")
        if text.startswith("[已压缩") or text.startswith("[已整理"):
            return f"该消息已经整理过了。"

        # ── Compress immediately ──
        # 有 AI 理解文本直接用（认知整理模式），否则才走模型驱动摘要/机械截断
        if summary and summary.strip():
            compressed = f"[已整理 原文{len(text)}字 ↕] {summary.strip()[:300]}"
            original = text
        else:
            compressed, original = _auto_compress(text, llm_callable=_llm_summarize, force=True)
        if original is None:
            return f"消息太短（{len(text)}字），无需整理。"

        compressed_full = compressed + f"\n🔍 expand_compressed(\"{topic_id}\", \"{anchor or anchor_idx}\")"

        # ── 存储层永不动：messages 表保留原文（前端显示完整对话）──
        # 整理记录只写 compression_tree，AI 组装上下文时动态注入摘要视图
        db_id = target.get("id")
        if db_id:
            # Mark as hidden so folded message doesn't clutter future context builds
            try:
                existing_args = dict(target.get("args") or {})
            except Exception:
                existing_args = {}
            existing_args["hidden"] = True
            db._execute("UPDATE messages SET args = ? WHERE id = ?",
                        (json.dumps(existing_args, ensure_ascii=False), db_id))
            db._commit()
            _store_compressed_original(topic_id, str(anchor or anchor_idx), original, get_store())

            # 写整理树节点（单条区间，摘要自包含：前缀+提示）
            _ensure_ct_table(db)
            db._execute(
                "INSERT INTO compression_tree (topic_id, parent_id, depth, anchor_start, anchor_end, item_count, summary_text, msg_db_id, created_at) "
                "VALUES (?, NULL, 0, ?, ?, 1, ?, NULL, ?)",
                (topic_id, anchor_idx, anchor_idx, compressed_full, time.time())
            )
            db._commit()

        return {
            "folded": True,
            "anchor": str(anchor or anchor_idx),
            "db_id": db_id,
            "summary": compressed,
            "text": compressed_full,
        }
    except Exception as e:
        return f"整理失败：{e}"


# ══════════════════════════════════════════════════════
# Nested Compression Tree — v5.0
# ══════════════════════════════════════════════════════

MIN_COMPRESSION_ITEMS = 5  # 每次整理至少包含 5 条信息条目


def _ensure_ct_table(db):
    """Ensure compression_tree table exists (migration safety)."""
    try:
        db.conn.execute("SELECT id FROM compression_tree LIMIT 0")
    except Exception:
        db.conn.execute("""
            CREATE TABLE IF NOT EXISTS compression_tree (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id        TEXT NOT NULL,
                parent_id       INTEGER REFERENCES compression_tree(id) ON DELETE SET NULL,
                depth           INTEGER NOT NULL DEFAULT 0,
                anchor_start    INTEGER NOT NULL,
                anchor_end      INTEGER NOT NULL,
                item_count      INTEGER NOT NULL DEFAULT 0,
                summary_text    TEXT NOT NULL DEFAULT '',
                msg_db_id       INTEGER,
                created_at      REAL NOT NULL,
                FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE
            )
        """)


def _get_compressions_in_range(db, topic_id: str, anchor_start: int, anchor_end: int) -> list[dict]:
    """Return all compression tree nodes whose range intersects [anchor_start, anchor_end]."""
    _ensure_ct_table(db)
    rows = db._fetchall(
        """SELECT * FROM compression_tree
           WHERE topic_id = ?
             AND anchor_end >= ?
             AND anchor_start <= ?
           ORDER BY anchor_start""",
        (topic_id, anchor_start, anchor_end)
    )
    return [dict(r) for r in rows]


def _detect_range_conflict(
    topic_id: str,
    new_start: int,
    new_end: int,
    existing: list[dict],
) -> str | None:
    """Check for partial overlap conflicts.

    Returns None if no conflict.
    Returns error message string if conflict found.

    Rules:
    - Existing fully inside new range → OK, will become child
    - New range fully inside existing → REJECT, must expand first
    - Partial overlap (not fully contained either way) → REJECT
    - Exact match → OK, update existing (re-compress)
    """
    for ct in existing:
        ex_s, ex_e = ct["anchor_start"], ct["anchor_end"]

        # Exact match → re-compress
        if ex_s == new_start and ex_e == new_end:
            return None  # handled in caller

        # New fully contains existing → OK
        if new_start <= ex_s and new_end >= ex_e:
            continue

        # Existing fully contains new → REJECT
        if ex_s <= new_start and ex_e >= new_end:
            return (
                f"范围 [{new_start}, {new_end}] 已被整理节点 #{ct['id']} "
                f"（范围 [{ex_s}, {ex_e}]）完全包含。"
                f"请先 expand_compressed(node_id={ct['id']}) 展开后再整理。"
            )

        # Partial overlap → REJECT
        return (
            f"范围 [{new_start}, {new_end}] 与已有整理节点 #{ct['id']} "
            f"（范围 [{ex_s}, {ex_e}]）部分重叠。"
            f"嵌套整理要求范围必须完全包含或完全不相交。"
        )

    return None


def _count_items_in_range(
    indexed: list,
    anchor_start: int,
    anchor_end: int,
    compressions: list[dict],
) -> int:
    """Count how many info items are in the range.

    An item is:
    - A raw (non-compressed, non-compression-summary) message → 1
    - A compression summary message → counts as 1 node, its children are NOT counted
      (they're already folded inside the compression)

    The count determines whether MIN_COMPRESSION_ITEMS is met.
    """
    # Build set of anchor positions covered by existing compressions
    covered_by_compression = set()
    for ct in compressions:
        for pos in range(ct["anchor_start"], ct["anchor_end"] + 1):
            covered_by_compression.add(pos)

    count = 0
    for pos in range(anchor_start, anchor_end + 1):
        if pos >= len(indexed):
            break
        m, idx = indexed[pos]
        text = m.get("text", "")

        # Skip positions already covered by a sub-compression
        # (they count as the compression node itself, counted below)
        if pos in covered_by_compression and pos != anchor_start:
            # Only skip if not the exact start of a compression
            # The first message of a compression range is the summary itself
            continue

        # Compression summaries → each counts as 1 item
        if text.startswith("[上下文压缩"):
            count += 1
            continue

        # Already-folded messages → skip (they're inside a compression)
        if text == "📋":
            if pos not in covered_by_compression:
                # Orphaned folded message → still count
                count += 1
            continue

        # Normal messages
        count += 1

    # Add top-level compressions as items (each = 1)
    # Actually we already counted them above via "[上下文压缩" detection

    return count


def _create_compression_node(
    db,
    topic_id: str,
    parent_id: int | None,
    depth: int,
    anchor_start: int,
    anchor_end: int,
    item_count: int,
    summary_text: str,
    msg_db_id: int,
) -> int:
    """Insert a new compression tree node, return its id."""
    _ensure_ct_table(db)
    cur = db._execute(
        """INSERT INTO compression_tree
           (topic_id, parent_id, depth, anchor_start, anchor_end,
            item_count, summary_text, msg_db_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (topic_id, parent_id, depth, anchor_start, anchor_end,
         item_count, summary_text, msg_db_id, time.time())
    )
    db._commit()
    return cur.lastrowid


def _update_child_parents(db, child_ids: list[int], new_parent_id: int, new_depth: int):
    """Re-parent existing compression nodes under a new parent."""
    if not child_ids:
        return
    _ensure_ct_table(db)
    for cid in child_ids:
        db._execute(
            "UPDATE compression_tree SET parent_id = ?, depth = ? WHERE id = ?",
            (new_parent_id, new_depth, cid)
        )
    db._commit()


def _build_tree_nav_text(node_id: int, db, indent: str = "", max_depth: int = 3) -> str:
    """Build ASCII tree navigation text for a compression node and its descendants."""
    _ensure_ct_table(db)
    node = db._fetchone("SELECT * FROM compression_tree WHERE id = ?", (node_id,))
    if not node:
        return ""
    node = dict(node)

    lines = []
    depth = node["depth"]
    prefix = f"{indent}{'  ' * depth}"

    # Node label
    summary_preview = node["summary_text"][:80].replace("\n", " ").strip()
    lines.append(
        f"{prefix}├─ [L{depth}] 整理节点 #{node['id']} "
        f"({node['item_count']}项, 锚点 {node['anchor_start']}-{node['anchor_end']})"
    )
    if summary_preview:
        lines.append(f"{prefix}│  {summary_preview}...")
    lines.append(f"{prefix}│  ↳ expand_compressed(node_id={node['id']}, depth=1) 展开")

    # Children
    if max_depth > 0:
        children = db._fetchall(
            "SELECT * FROM compression_tree WHERE parent_id = ? ORDER BY anchor_start",
            (node_id,)
        )
        for child in children:
            child = dict(child)
            child_text = _build_tree_nav_text(child["id"], db, indent, max_depth - 1)
            if child_text:
                lines.append(child_text)

    return "\n".join(lines)


def _get_topic_tree_summary(db, topic_id: str, max_depth: int = 3) -> str:
    """Build a full compression tree summary for the topic."""
    _ensure_ct_table(db)
    roots = db._fetchall(
        """SELECT * FROM compression_tree
           WHERE topic_id = ? AND parent_id IS NULL
           ORDER BY anchor_start""",
        (topic_id,)
    )
    if not roots:
        return ""

    lines = ["【整理树】"]
    for root in roots:
        root = dict(root)
        lines.append(_build_tree_nav_text(root["id"], db, max_depth=max_depth))

    return "\n".join(lines)


@register_tool(
    name="organize_context",
    description="整理当前对话的连续消息（认知整理）：AI 理解后提取意图/关键信息/产出，原文归档可展开回溯。"
               "不传参数则整理全部未整理消息。ai_summary 传你的理解文本时直接作为整理摘要（不走裸LLM）。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "对话ID。不传则自动取当前话题。",
            },
            "anchor_start": {
                "type": "integer",
                "description": "起始消息编号（从0开始）。不传则从第一条开始。",
            },
            "anchor_end": {
                "type": "integer",
                "description": "结束消息编号。不传则到最后一条未整理消息。",
            },
            "purpose": {
                "type": "string",
                "description": "整理目的：总结结论 / 保留决策 / 归档产出。默认总结结论。",
            },
            "force": {
                "type": "boolean",
                "description": "是否重新整理已归档的消息。设为true时会展开原文重新提炼摘要，用于修正之前遗漏的信息。默认false（跳过已整理消息）。",
            },
            "ai_summary": {
                "type": "string",
                "description": "AI 提供的理解摘要（可选）。传了直接作为整理节点摘要（意图揣测+信息提取的产物），不调裸LLM；不传才走模型驱动生成。",
            },
        },
        "required": [],
    },
)
def compress_context(topic_id: str = "", anchor_start: int = -1, anchor_end: int = -1,
                     purpose: str = "", force: bool = False, ai_summary: str = ""):
    """批量归档一段连续对话 + 插入结构化摘要。支持嵌套整理。

    所有参数可选，自动取默认值。
    嵌套规则：
    - 范围内已有的整理节点自动成为子节点
    - 每次整理至少 MIN_COMPRESSION_ITEMS 条信息
    - 部分重叠的整理范围会报冲突
    - force=True 时重新整理已有整理节点（展开原文重提炼）
    """
    try:
        from server import (
            _auto_compress, _store_compressed_original, _llm_summarize,
            get_model_config, PER_MESSAGE_COMPRESS_CHARS,
            get_compressed_original,
        )
        from db import get_db
        from memory.fragment_store import get_store
        import json, urllib.request

        db = get_db()
        _ensure_ct_table(db)

        # ── Defaults: auto-detect topic ──
        if not topic_id:
            topic_id = db.get_active_topic_id() or ""
        if not topic_id:
            return "无法确定当前话题。请指定 topic_id 或先开始一个对话。"
        raw = db.get_messages(topic_id, limit=500)
        store = get_store() if _memory_enabled else None

        # ── Resolve anchors: build positional index (non-tool only, 全局序号) ──
        indexed = []  # [(msg_dict, global_idx)]
        count = 0
        for m in raw:
            role = m.get("role", "")
            if role == "tool":
                continue
            indexed.append((m, count))
            count += 1
        # 窗口起点偏移：折叠区间的 anchor 必须与组装侧一致（话题全局非tool序号）
        try:
            _trow = db._fetchone(
                "SELECT COUNT(*) AS c FROM messages WHERE topic_id=? AND role!='tool'",
                (topic_id,))
            _total_nt = _trow["c"] if _trow else 0
        except Exception:
            _total_nt = 0
        _win_nt = len(indexed)
        _base = max(_total_nt - _win_nt, 0)
        if _base:
            indexed = [(m, i + _base) for (m, i) in indexed]

        total_raw = len(raw)

        # ── Defaults: auto-select anchor range ──
        if len(indexed) == 0:
            role_counts = {}
            for m in raw:
                r = m.get("role", "?")
                role_counts[r] = role_counts.get(r, 0) + 1
            return (
                f"topic_id={topic_id} 中没有非工具消息可整理。\n"
                f"DB中共 {total_raw} 条消息，角色分布：{role_counts}。"
            )

        if anchor_start < 0:
            anchor_start = 0
        if anchor_end < 0:
            for i in range(len(indexed) - 1, -1, -1):
                m, idx = indexed[i]
                if "[已整理" not in (m.get("text") or "") \
                   and "[已压缩" not in (m.get("text") or "") \
                   and "[上下文压缩" not in (m.get("text") or "") \
                   and (m.get("text") or "") != "📋":
                    anchor_end = idx
                    break
            if anchor_end < 0:
                anchor_end = len(indexed) - 1

        if anchor_start >= len(indexed):
            return f"起始锚点 {anchor_start} 超出消息范围（共 {len(indexed)} 条，编号 0~{len(indexed)-1}）"
        if anchor_end >= len(indexed):
            anchor_end = len(indexed) - 1

        range_msgs = indexed[anchor_start:anchor_end + 1]
        if not range_msgs:
            return "指定的锚点范围内没有消息。"

        # ── Nested compression: detect existing compressions in range ──
        existing_cts = _get_compressions_in_range(db, topic_id, anchor_start, anchor_end)

        # ── Conflict detection ──
        conflict = _detect_range_conflict(topic_id, anchor_start, anchor_end, existing_cts)
        if conflict:
            return f"❌ 嵌套整理冲突：{conflict}"

        # ── Exact match → re-compress existing node ──
        exact_match = None
        for ct in existing_cts:
            if ct["anchor_start"] == anchor_start and ct["anchor_end"] == anchor_end:
                exact_match = ct
                break

        # ── Item count check (skip for exact match — already validated) ──
        if exact_match:
            item_count = exact_match["item_count"]
        else:
            item_count = _count_items_in_range(indexed, anchor_start, anchor_end, existing_cts)
            if item_count < MIN_COMPRESSION_ITEMS:
                return (
                    f"❌ 范围内只有 {item_count} 条信息条目，不满足最少 {MIN_COMPRESSION_ITEMS} 条的要求。\n"
                    f"建议扩大范围（当前 [{anchor_start}, {anchor_end}]，共 {len(range_msgs)} 个锚点，"
                    f"但其中 {len(range_msgs) - item_count} 个已被子整理覆盖）。\n"
                    f"可以往前后延伸 anchor_start/anchor_end 来凑够 {MIN_COMPRESSION_ITEMS} 条。"
                )
        # ── Determine parent and depth ──
        # Children = existing compressions fully inside new range
        child_nodes = [ct for ct in existing_cts
                       if ct["anchor_start"] >= anchor_start and ct["anchor_end"] <= anchor_end
                       and ct != exact_match]
        max_child_depth = max((ct["depth"] for ct in child_nodes), default=0)
        new_depth = max_child_depth + 1

        # ── Collect texts for LLM summarization ──
        texts_for_llm = []
        child_summaries = []
        for m, idx in range_msgs:
            role_label = "用户" if m.get("role") == "user" else "大眼"
            text = m.get("text", "")
            is_compressed = text.startswith("[已整理") or text.startswith("[已压缩") or text.startswith("[上下文压缩") or text == "📋"

            # Check if this message is a compression summary that's a child
            is_child_ct = False
            for ct in child_nodes:
                if ct["anchor_start"] == idx:
                    is_child_ct = True
                    child_summaries.append(
                        f"[子整理 L{ct['depth']} #{ct['id']}] {ct['summary_text'][:200]}"
                    )
                    break

            if is_child_ct:
                # Include child summary as context for LLM
                ct_text = next((ct["summary_text"] for ct in child_nodes
                               if ct["anchor_start"] == idx), text)
                texts_for_llm.append(f"[子整理 L?] {ct_text[:300]}")
            elif is_compressed:
                if force:
                    orig = get_compressed_original(topic_id, str(idx + 1)) \
                           or get_compressed_original(topic_id, str(idx))
                    text = orig if orig else text[:100]
                    texts_for_llm.append(f"[{role_label}] {text}")
                else:
                    texts_for_llm.append(f"[{role_label}] (已整理内容)")
            else:
                texts_for_llm.append(f"[{role_label}] {text}")

        combined = "\n\n".join(texts_for_llm)

        # ── Generate structured summary via LLM ──
        purpose_text = purpose or "总结结论"
        if child_summaries:
            nested_hint = (
                f"以下内容包含 {len(child_nodes)} 个已整理的子段落。"
                f"请在摘要中用 \"内含子整理: ...\" 标注它们的关键信息。"
            )
            purpose_text = f"{purpose_text}；{nested_hint}"
        if ai_summary and ai_summary.strip():
            # AI 认知整理模式：直接用 AI 提供的理解摘要，不调裸 LLM
            summary_text = ai_summary.strip()
        else:
            summary_text = _generate_compress_summary(combined, purpose_text)

        # ── Build tree navigation ──
        import datetime
        first_ts = (range_msgs[0][0].get("timestamp") or 0) / 1000.0
        last_ts = (range_msgs[-1][0].get("timestamp") or 0) / 1000.0
        def _fmt_ts(ts):
            return datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone(datetime.timedelta(hours=8))
            ).strftime("%m-%d %H:%M:%S")
        time_range = f"{_fmt_ts(first_ts)} ~ {_fmt_ts(last_ts)}"

        # Build tree nav for children
        tree_nav = ""
        if child_nodes:
            tree_nav = "\n📂 内含子整理：\n"
            for cn in sorted(child_nodes, key=lambda c: c["anchor_start"]):
                preview = cn["summary_text"][:100].replace("\n", " ").strip()
                tree_nav += (
                    f"  ├── [L{cn['depth']}] #{cn['id']} "
                    f"({cn['item_count']}项, [{cn['anchor_start']}-{cn['anchor_end']}])\n"
                    f"  │   {preview}...\n"
                    f"  │   ↳ expand_compressed(node_id={cn['id']}) 展开子整理\n"
                )

        full_summary = (
            f"[已整理 L{new_depth}] {time_range}  "
            f"({item_count}项)\n"
            f"{summary_text}"
            f"{tree_nav}"
            f"\n📋 expand_compressed(node_id={exact_match['id'] if exact_match else '???'}, depth=N) 逐层展开"
        )

        # ── Create or update compression tree node ──
        summary_ts = time.time()  # insert at end — avoids anchor position shifts

        if exact_match:
            # Re-compress: update existing node, regenerate summary
            node_id = exact_match["id"]
            # Replace placeholder if we didn't know the ID at summary construction
            # (already correct since we used exact_match['id'] above)
            db._execute(
                """UPDATE compression_tree
                   SET item_count = ?, summary_text = ?, depth = ?,
                       anchor_start = ?, anchor_end = ?
                   WHERE id = ?""",
                (item_count, full_summary, exact_match["depth"],
                 anchor_start, anchor_end, node_id)
            )
            db._commit()

            # 存储层永不动：不再更新 summary 消息文本（messages 表保留原文）
            # old_msg_id = exact_match.get("msg_db_id")
            # if old_msg_id:
            #     db.update_message_text(old_msg_id, full_summary)

            # Re-fold all messages in range
            fold_results = _fold_range_msgs(
                db, topic_id, range_msgs, indexed, anchor_start, anchor_end,
                existing_cts, force, store, get_compressed_original, _auto_compress,
                _store_compressed_original,
            )

            return {
                "compressed": True,
                "node_id": node_id,
                "depth": exact_match["depth"],
                "re_compressed": True,
                "summary_anchor": anchor_start,
                "summary_db_id": None,  # 不再有 summary 消息（存储层永不动）
                "summary_text": full_summary,
                "item_count": item_count,
                "child_nodes": [c["id"] for c in child_nodes],
                "folded_count": len(range_msgs),
                "folded": fold_results,
            }

        else:
            # New compression
            # 存储层永不动：不再向 messages 表插入 summary 消息（前端显示原始对话）
            # 摘要只进 compression_tree，AI 组装上下文时动态注入
            summary_db_id = None

            # Create tree node
            node_id = _create_compression_node(
                db, topic_id,
                parent_id=None,  # root-level for now
                depth=new_depth,
                anchor_start=anchor_start,
                anchor_end=anchor_end,
                item_count=item_count,
                summary_text=full_summary,
                msg_db_id=None,  # 不再关联 summary 消息
            )

            # Update full_summary with actual node_id (tree node only)
            full_summary = full_summary.replace(
                "expand_compressed(node_id=<待填入>, depth=N)",
                f"expand_compressed(node_id={node_id}, depth=N)"
            )
            db._execute(
                "UPDATE compression_tree SET summary_text = ? WHERE id = ?",
                (full_summary, node_id)
            )
            db._commit()
            # Re-parent child compressions
            child_ids = [c["id"] for c in child_nodes]
            if child_ids:
                _update_child_parents(db, child_ids, node_id, new_depth)

            # Fold each message in range
            fold_results = _fold_range_msgs(
                db, topic_id, range_msgs, indexed, anchor_start, anchor_end,
                existing_cts, force, store, get_compressed_original, _auto_compress,
                _store_compressed_original,
            )

            return {
                "compressed": True,
                "node_id": node_id,
                "depth": new_depth,
                "re_compressed": False,
                "summary_anchor": anchor_start,
                "summary_db_id": summary_db_id,
                "summary_text": full_summary,
                "item_count": item_count,
                "child_nodes": child_ids,
                "folded_count": len(range_msgs),
                "folded": fold_results,
            }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"上下文整理失败：{e}"


def _fold_range_msgs(db, topic_id, range_msgs, indexed, anchor_start, anchor_end,
                     existing_cts, force, store, get_compressed_original,
                     _auto_compress, _store_compressed_original):
    """Fold each message in the range, skipping compression summaries."""
    fold_results = []
    child_ct_starts = {ct["anchor_start"] for ct in existing_cts}

    for m, idx in range_msgs:
        text = m.get("text", "")
        is_ct_summary = idx in child_ct_starts and (text.startswith("[上下文压缩") or text.startswith("[已整理"))

        if is_ct_summary and not force:
            # Child compression summary — keep visible, don't fold
            fold_results.append({
                "anchor": str(idx),
                "db_id": m.get("id"),
                "is_child_compression": True,
            })
            continue

        is_compressed = text.startswith("[已整理") or text.startswith("[已压缩") or text.startswith("[上下文压缩") or text == "📋"

        if is_compressed and not force:
            fold_results.append({
                "anchor": str(idx),
                "db_id": m.get("id"),
                "already_compressed": True,
            })
            continue

        # Fold: get original if needed
        compress_text = text
        if is_compressed and force:
            orig = get_compressed_original(topic_id, str(idx)) \
                   or get_compressed_original(topic_id, str(idx))
            if orig:
                compress_text = orig

        compressed, original = _auto_compress(compress_text, llm_callable=None, force=True)
        if original is not None:
            db_id = m.get("id")
            if db_id:
                # 存储层永不动：不替换 content（前端显示原文），只标记 hidden 让 AI 组装时跳过
                # Mark as hidden so it doesn't re-appear in future context builds
                try:
                    existing_args = dict(m.get("args") or {})
                except Exception:
                    existing_args = {}
                existing_args["hidden"] = True
                db._execute("UPDATE messages SET args = ? WHERE id = ?",
                            (json.dumps(existing_args, ensure_ascii=False), db_id))
                db._commit()
            _store_compressed_original(topic_id, str(idx), original, store)
            fold_results.append({
                "anchor": str(idx),
                "db_id": db_id,
                "text": compressed,
            })
        else:
            fold_results.append({
                "anchor": str(idx),
                "db_id": m.get("id"),
                "too_short": True,
            })

    return fold_results



def _generate_compress_summary(texts_combined: str, purpose: str) -> str:
    """Call LLM to generate a structured compression summary."""
    try:
        from server import get_model_config
        import json, urllib.request

        llm_config = get_model_config()
        if not llm_config or not llm_config.api_key or not llm_config.base_url:
            return _fallback_summary(texts_combined)

        prompt = (
            f"请对以下连续对话进行结构化整理（目的：{purpose}），输出格式如下：\n"
            f"结论：<这段对话最终达成的结论或决策>\n"
            f"产出：<产生的代码、文件、数据、方案等>\n"
            f"遗留：<未解决的问题或需跟进的事项>\n"
            f"丢弃：<本次省略的非重要内容，仅记类别和数量，无则写无>\n"
            f"---\n"
            f"取舍纪律：\n"
            f"必须保留：结论与决策及理由、最终方案/代码/文件路径等具体产出、未解决的问题、错误教训及原因、用户表达的偏好和要求。\n"
            f"可以丢弃：重复尝试的中间过程（须保留最终结果与结论）、寒暄、客套、对已知内容的复述。\n"
            f"拿不准是否重要的一律按必须保留处理，不许丢弃。\n"
            f"总长控制在260字以内，只输出上述四行，不要前缀不要多余文字；丢弃行只记类别和数量，不展开具体内容。\n"
        )

        url = f"{llm_config.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_config.api_key}",
        }
        body = json.dumps({
            "model": llm_config.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": texts_combined[:24000]},
            ],
            "stream": False,
            "max_tokens": 2000,
            "temperature": 0.3,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        # ── 连接层瞬时故障自动重试（与 llm.py 主链路同款策略）──
        # 429/限流也算瞬时故障：compress 常在对话高峰并发调 LLM，被限流应重试而非直接放弃
        _TRANSIENT_MARKERS = ("remote end closed", "timed out", "timeout",
                              "connection reset", "connection aborted",
                              "connection refused", "eof occurred",
                              "temporarily unavailable", "no route to host",
                              "network is unreachable", "name or service not known",
                              "http error 429", "rate limit", "too many requests")
        # SSL context 复用 server 主链路的处理（_VERIFY_SSL 为 False 时跳过证书校验）
        try:
            from server import _VERIFY_SSL, _UNVERIFIED_SSL_CTX
            ssl_ctx = None if _VERIFY_SSL else _UNVERIFIED_SSL_CTX
        except Exception:
            ssl_ctx = None

        data = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                with urllib.request.urlopen(req, timeout=90, context=ssl_ctx) as resp:
                    data = json.loads(resp.read())
                break
            except Exception as e:
                s = str(e).lower()
                if attempt < max_attempts - 1 and any(m in s for m in _TRANSIENT_MARKERS):
                    wait = 1.5 * (attempt + 1)
                    print(f"[compress] transient error (attempt {attempt + 1}/{max_attempts}), "
                          f"retry in {wait}s: {e}")
                    time.sleep(wait)
                    continue
                print(f"[compress] summary LLM call failed, falling back to mechanical summary: {e}")
                return _fallback_summary(texts_combined)

        result = (data or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
        if result and len(result) > 5:
            return result.strip()
        print("[compress] summary LLM returned empty result, falling back to mechanical summary")
        return _fallback_summary(texts_combined)
    except Exception as e:
        print(f"[compress] summary generation error, falling back: {e}")
        return _fallback_summary(texts_combined)


def _fallback_summary(texts_combined: str) -> str:
    """Mechanical fallback when LLM unavailable. 尽力提取有信息量的摘要，避免"结论未生成"空转。"""
    lines = [l.strip() for l in texts_combined.split("\n") if l.strip()]
    # 结论：取最后一条非空消息（对话落点通常是结论/决策）
    tail = lines[-1][:120] if lines else ""
    # 产出：找含路径/动词/统计线索的最近行
    import re
    prod_pat = re.compile(r"(?:[\w/\\]+\.(?:py|json|md|js|html|txt|c|h)|节点|修复|完成|写入|新增|删除|更新|落地|验证|通过|失败|已压缩|已整理)", re.IGNORECASE)
    candidates = []
    for l in lines[-15:]:
        if prod_pat.search(l):
            candidates.append(l[:90])
        if len(candidates) >= 3:
            break
    prod = "；".join(candidates) if candidates else (lines[0][:120] if lines else "")
    return (
        f"结论：{tail or '（原文无有效内容）'}\n"
        f"产出：{prod or '无'}\n"
        f"遗留：无（机械摘要，可 expand_compressed 看原文）"
    )

@register_tool(
    name="list_skill_templates",
    description="列出可用的技能模板。AI 可以用这些模板快速创建新技能。",
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_skill_templates():
    try:
        from skills_scanner import list_templates
        templates = list_templates()
        if not templates:
            return "暂无可用模板。用 write_file 直接写 skills/新技能.md 即可。"
        lines = ["📋 可用技能模板："]
        for t in templates:
            lines.append(f"  • {t['name']} — {t['description']}")
        lines.append("")
        lines.append("用 create_skill(template='模板名', name='技能名', ...) 基于模板创建新技能。")
        return "\n".join(lines)
    except Exception as e:
        return f"获取模板列表失败: {e}"


@register_tool(
    name="create_skill",
    description="从模板创建新技能。生成规范格式的技能文件，立即可用（无需重启）。",
    parameters={
        "type": "object",
        "properties": {
            "template": {
                "type": "string",
                "description": "模板名称（用 list_skill_templates 查看可用模板）。默认 'generic'",
            },
            "name": {
                "type": "string",
                "description": "新技能的名称（用作文件名，如 '爬京东商品'）",
            },
            "purpose": {
                "type": "string",
                "description": "一句话描述这个技能做什么",
            },
            "tags": {
                "type": "string",
                "description": "逗号分隔的标签，如 '爬虫, web, 电商'",
            },
            "triggers": {
                "type": "string",
                "description": "逗号分隔的触发词，AI 看到这些词时自动联想到此技能",
            },
            "tools": {
                "type": "string",
                "description": "需要用到的工具，逗号分隔，如 'web_search, web_fetch, write_file'",
            },
        },
        "required": ["name", "purpose"],
    },
)
def create_skill(template: str = "generic", name: str = "", purpose: str = "",
                 tags: str = "", triggers: str = "", tools: str = ""):
    try:
        from skills_scanner import create_skill_from_template, build_skill_index
        path = create_skill_from_template(
            template, name,
            PURPOSE=purpose, TAGS=tags, TRIGGERS=triggers, TOOLS=tools,
        )
        if not path:
            return f"❌ 模板 '{template}' 不存在。用 list_skill_templates 查看可用模板。"
        return (
            f"✅ 技能已创建: {path}\n\n"
            f"用 read_file('{path}') 查看模板内容，edit_file 补充细节。\n"
            f"已热加载，下次对话即可使用。"
        )
    except Exception as e:
        return f"创建技能失败: {e}"


@register_tool(
    name="reflection_loop",
    description=(
        "跑一次反思回路——维护晶体记忆网络的健康。五项职责："
        "(1)建弱关联：给孤立晶体找相似伙伴；(2)涌现元晶：从相似晶体群提炼共通模式；"
        "(3)反熵修剪：删低置信度边、合并重复边；(4)自检缺口：发现高频实体但没有专门晶体的盲区；"
        "(5)提拔权威：多次验证没被推翻的晶体提为权威。"
        "聊完一个复杂话题后、感觉记忆杂乱时主动调用；正常对话会自动空闲触发，不必每次手动调。"
        "only 参数可只跑某一项。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "only": {
                "type": "string",
                "enum": ["", "weak_assoc", "meta", "prune", "gaps", "authority", "narrative_consolidation", "consolidate"],
                "description": "只跑某一项职责（默认全跑）。weak_assoc=建弱关联, meta=涌现元晶, prune=反熵修剪, gaps=自检缺口, authority=提拔权威, narrative_consolidation=叙事沉淀(整理story), consolidate=观察整合(吸取hindsight四动作)。",
            },
        },
        "required": [],
    },
)
def reflection_loop(only: str = "", **_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        from memory.reflection_loop import get_loop
        loop = get_loop()
        if only == "weak_assoc":
            n = loop.build_weak_associations()
            return f"建了 {n} 条弱关联。"
        if only == "meta":
            n = loop.emerge_meta_crystals()
            return f"涌现了 {n} 个元晶。"
        if only == "prune":
            n = loop.prune_entropy()
            return f"修剪了 {n} 条边。"
        if only == "gaps":
            gaps = loop.detect_gaps()
            if not gaps:
                return "没发现记忆盲区。"
            lines = [f"发现 {len(gaps)} 个记忆盲区："]
            for g in gaps[:10]:
                lines.append(f"- {g['suggestion']}（提及 {g['mention_count']} 次）")
            return "\n".join(lines)
        if only == "authority":
            n = loop.promote_authority()
            return f"提拔了 {n} 个权威晶体。"
        if only == "narrative_consolidation":
            n = loop.narrative_consolidation()
            return f"叙事沉淀：整理了 {n} 个故事。"
        if only == "consolidate":
            r = loop.consolidate_observations()
            return (f"观察整合：创建 {r['create']}，更新 {r['update']}，"
                    f"合并 {r['merge']}，矛盾 {r['contradict']}，跳过 {r['skipped']}。")
        # 全跑
        report = loop.run()
        lines = ["反思回路完成："]
        lines.append(f"- 回填实体：{report.get('entities_backfilled', 0)} 条记忆")
        lines.append(f"- 建弱关联：{report['weak_assoc']} 条")
        lines.append(f"- 涌现元晶：{report['meta_crystals']} 个")
        lines.append(f"- 反熵修剪：{report['pruned']} 条边")
        lines.append(f"- 记忆盲区：{report['gaps']} 个")
        lines.append(f"- 提拔权威：{report['promoted']} 个")
        lines.append(f"- 叙事沉淀：{report.get('stories_consolidated', 0)} 个故事")
        obs = report.get('observations_consolidated', {}) or {}
        lines.append(f"- 观察整合：创建{obs.get('create', 0)} 更新{obs.get('update', 0)} 合并{obs.get('merge', 0)} 矛盾{obs.get('contradict', 0)} 跳过{obs.get('skipped', 0)}")
        lines.append(f"- 提取文件思考：{report.get('file_thoughts_extracted', 0)} 条")
        if report["errors"]:
            lines.append(f"- 错误：{len(report['errors'])} 个")
            for e in report["errors"][:3]:
                lines.append(f"  · {e}")
        return "\n".join(lines)
    except Exception as e:
        return f"反思回路出错：{e}"


@register_tool(
    name="check_memory_gaps",
    description="查一下记忆里有哪些盲区——频繁提到但没专门记忆的实体。返回建议补全的晶体列表。",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
def check_memory_gaps(**_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        from memory.reflection_loop import get_loop
        gaps = get_loop().detect_gaps()
        if not gaps:
            return "记忆覆盖良好，没发现盲区。"
        lines = [f"发现 {len(gaps)} 个记忆盲区："]
        for g in gaps[:15]:
            lines.append(f"- 「{g['entity_name']}」（{g['entity_type']}，提及 {g['mention_count']} 次）")
        return "\n".join(lines)
    except Exception as e:
        return f"查盲区出错：{e}"


@register_tool(
    name="crystal_recall",
    description="搜记忆晶体，返回所有入口（自传/文件/关联）。带置信度衰减和权威标记。追因果链用 trace_memory，查盲区用 check_memory_gaps。",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜什么——人名、事件、话题，越具体越好。留空则浏览最近记忆。",
            },
            "tags": {
                "type": "string",
                "description": "按标签筛选，如 '错误,爬虫'。可选。",
            },
            "top_k": {
                "type": "integer",
                "description": "直接命中返回条数，默认10",
                "default": 10,
            },
        },
        "required": [],
    },
)
def crystal_recall(query: str = "", tags: str = "", top_k: int = 10, **_):
    _ensure_memory()
    if not _memory_enabled:
        return "记忆系统未启用"
    try:
        # ── 无关键词浏览模式：返回最近记忆锚点 ──
        if not query.strip() and not tags:
            anchors = _memory_store._anchors(10)
            if not anchors:
                return "脑子里一片空白，什么都想不起来。"
            lines = ["最近的记忆（浏览模式）："]
            for f in anchors:
                ts = f.get("ts", "")
                tid_f = f.get("topic_id") or ""
                topic_tag = f" [topic={tid_f[:8]}]" if tid_f else ""
                w = f" ⭐{f['weight']:.1f}" if f.get("weight") else ""
                lines.append(f"[{ts[:8]} {ts[8:12]} id={f['id']}{topic_tag}{w}] {f['text']}")
            lines.append("\n💭 想追某条的因果链？用 trace_memory(id 或关键词)")
            return "\n".join(lines)

        # ── 关键词/标签检索模式 ──
        direct_hits = []
        network_hits = []
        decay_warnings = []

        # 路径1a: 自传晶体 embedding 召回
        if query.strip():
            try:
                frags = _memory_store.recall(query, top_k=top_k, threshold=0.3)
                direct_hits.extend(frags)
            except Exception as e:
                print(f"[crystal_recall] 自传晶体 embedding 召回失败: {e}")

            # 路径1b: text 兜底（embedding 没命中时 LIKE 查询）
            text_results = _memory_store.search_by_text(query, limit=top_k)
            seen_ids = {f.get("id") for f in direct_hits}
            for tf in text_results:
                if tf.get("id") not in seen_ids:
                    direct_hits.append(tf)
                    seen_ids.add(tf.get("id"))

        # 路径1c: 文件晶体召回
        if query.strip():
            try:
                from memory.file_crystal_store import FileCrystalStore
                fcs = FileCrystalStore()
                files = fcs.recall(query, top_k=top_k)
                for fc in files:
                    fc["_node_type"] = "file"
                    direct_hits.append(fc)
            except Exception as e:
                print(f"[crystal_recall] 文件晶体召回失败: {e}")

        # 路径1d: 标签筛选（只对自传晶体）
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                if not direct_hits:
                    # 纯标签模式：取全部再筛
                    direct_hits = _memory_store.get_all(limit=200)
                filtered = []
                for h in direct_hits:
                    h_tags = (h.get("tags") or "").lower()
                    if any(t.lower() in h_tags for t in tag_list):
                        filtered.append(h)
                direct_hits = filtered

        # 路径1e: 历史对话消息召回（带前后文的"念头"）
        if query.strip():
            try:
                from memory.msg_vectors import recall_with_context
                msgs = recall_with_context(query, top_k=4)
                seen_msg_ids = {h.get("message_id") for h in direct_hits if h.get("message_id")}
                for m in msgs:
                    if m.get("message_id") in seen_msg_ids:
                        continue
                    m["_node_type"] = "message"
                    direct_hits.append(m)
            except Exception as e:
                print(f"[crystal_recall] 消息召回失败: {e}")

        # 路径2: 关系网扩展（走因果/派生/支持边）
        if direct_hits:
            try:
                from memory.cmn_retriever import get_retriever
                retriever = get_retriever()
                # 用直接命中的 id 走边扩展
                direct_ids = [h.get("id") for h in direct_hits if h.get("id")]
                network_hits = retriever._expand_via_ids(direct_ids, top_k=5)
            except Exception as e:
                print(f"[crystal_recall] 网络扩展失败: {e}")

        # 衰减警告
        for h in direct_hits + network_hits:
            decay = h.get("confidence_decay", 1.0)
            if decay < 0.5:
                decay_warnings.append({"id": h.get("id"), "decay": decay})

        if not direct_hits and not network_hits:
            return f"没找到关于「{query or tags}」的记忆。"

        # ── 全景呈现 ──
        lines = [f"回想「{query or tags}」："]

        # 消息命中单独成块（带上下文的"念头"，不占碎片名额）
        msg_hits = [h for h in direct_hits if h.get("_node_type") == "message"]
        other_hits = [h for h in direct_hits if h.get("_node_type") != "message"]

        # 直接命中（碎片/文件晶体）
        if other_hits:
            lines.append(f"\n【直接命中 {len(other_hits)} 条】")
            for i, h in enumerate(other_hits[:top_k], 1):
                mark = _format_crystal_mark(h)
                text = _format_crystal_text(h, 100)
                lines.append(f"  {i}. {mark} {text}")

        # 对话记忆（你说过的话 + 现场）
        if msg_hits:
            lines.append(f"\n【对话记忆 {len(msg_hits)} 条】（你说过的话，带上下文）")
            for i, h in enumerate(msg_hits[:4], 1):
                lines.append(_format_message_hit(i, h))

        # 关联扩展
        if network_hits:
            lines.append(f"\n【关联扩展 {len(network_hits)} 条】（走因果/派生边）")
            for i, h in enumerate(network_hits[:5], 1):
                mark = _format_crystal_mark(h)
                text = _format_crystal_text(h, 80)
                lines.append(f"  {i}. {mark} {text}")

        # 衰减警告
        if decay_warnings:
            lines.append(f"\n⚠️ {len(decay_warnings)} 条可能过期，用 verify_crystal 验证")

        # 入口提示
        lines.append("\n💭 追因果链: trace_memory(id 或关键词) / 查盲区: check_memory_gaps")
        return "\n".join(lines)
    except Exception as e:
        return f"回想出错：{e}"


def _format_message_hit(i: int, h: dict) -> str:
    """格式化消息命中：你说过的话 + 前后文（"念头"带现场）。"""
    import time as _t
    text = (h.get("text") or "").replace("\n", " ").strip()
    if len(text) > 90:
        text = text[:90] + "…"
    title = h.get("topic_title") or (h.get("topic_id") or "")[:8]
    ts = h.get("ts") or 0
    ts_str = _t.strftime("%m-%d %H:%M", _t.localtime(ts)) if ts else ""
    score = h.get("score", 0)
    lines = [f"  {i}. [对话][{ts_str}][{title}] 你说：{text} (score={score:.2f})"]
    ctx = h.get("context") or []
    for c in ctx[-3:]:
        ctext = (c.get("text") or "").replace("\n", " ").strip()
        if not ctext:
            continue
        if len(ctext) > 60:
            ctext = ctext[:60] + "…"
        role = "我说" if c.get("role") == "ai" else "你说"
        lines.append(f"      ↳ {role}：{ctext}")
    lines.append("      ↳ 翻阅完整对话: read_topic_messages")
    return "\n".join(lines)


def _format_crystal_mark(h: dict) -> str:
    """格式化晶体标记：[类型][权威][衰减]"""
    node_type = h.get("node_type") or h.get("_node_type") or "self"
    layer = h.get("layer") or "core"
    if node_type == "file":
        type_mark = "文件"
    elif layer == "knowledge":
        type_mark = "知识"  # 成体系知识晶体（vs 碎片）
    elif layer == "story":
        type_mark = "故事"  # 叙事记忆（情景记忆，带时间+因果）
    else:
        type_mark = "记忆"
    auth = "★权威" if h.get("authority_level") else ""
    decay = h.get("confidence_decay", 1.0)
    decay_mark = f"⚠️{decay:.2f}" if decay < 0.5 else ""
    parts = [type_mark, auth, decay_mark]
    return "[" + "".join(p for p in parts if p) + "]"


def _format_crystal_text(h: dict, max_len: int = 100) -> str:
    """格式化晶体文本：带 id 和时间戳"""
    text = (h.get("text") or h.get("summary") or "")[:max_len].replace("\n", " ")
    hid = h.get("id", "")
    ts = h.get("ts", "")
    ts_str = f"[{ts[:8]} {ts[8:12]}]" if ts else ""
    id_str = f"id={hid}" if hid else ""
    prefix = f"{ts_str} {id_str}".strip()
    return f"{prefix} {text}".strip() if prefix else text
