#!/usr/bin/env python3
"""Task management tools — rename tasks, manage workspace."""
import os
import sys
from .registry import register_tool

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
        db = get_db()
        tid = db.get_active_topic_id()
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
        db = get_db()
        tid = db.get_active_topic_id()
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
    description="翻阅任意任务的原始对话记录（溯源链最后一环，从碎片→话题→原始消息）。"
               "⚠ 翻阅结果占用上下文！看完立即 organize_context 或记要点，别留占地方。"
               "默认从最新往前翻；order='asc' 从最早往后翻；search 关键词过滤。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "任务ID前缀或完整ID，从 list_topics 获取。如 '0a8e' 或 '0a8e937309d6'"
            },
            "before_ts": {
                "type": "number",
                "description": "翻到此时刻之前。不传=最新。上下文墙标记中提供"
            },
            "search": {
                "type": "string",
                "description": "关键词过滤，只返回含此词的消息。不传=全部"
            },
            "limit": {
                "type": "integer",
                "description": "返回条数，默认30",
                "default": 30
            },
            "order": {
                "type": "string",
                "description": "'desc'(默认)=从最新往前翻；'asc'=从最早往后翻"
            }
        },
        "required": ["topic_id"]
    }
)
def read_topic_messages(topic_id: str, before_ts: float = None, search: str = "", limit: int = 30,
                        order: str = "desc"):
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

        asc = order == "asc"
        sort_dir = "ASC" if asc else "DESC"
        cmp_op = ">" if asc else "<"  # for before_ts / after_ts

        if before_ts:
            if search:
                rows = db.conn.execute(
                    f"SELECT role, text, ts FROM messages WHERE topic_id=? AND ts {cmp_op} ? AND text LIKE ? "
                    f"ORDER BY ts {sort_dir} LIMIT ?",
                    (tid, before_ts, f"%{search}%", limit + 1)
                ).fetchall()
            else:
                rows = db.conn.execute(
                    f"SELECT role, text, ts FROM messages WHERE topic_id=? AND ts {cmp_op} ? "
                    f"ORDER BY ts {sort_dir} LIMIT ?",
                    (tid, before_ts, limit + 1)
                ).fetchall()
        else:
            if search:
                rows = db.conn.execute(
                    f"SELECT role, text, ts FROM messages WHERE topic_id=? AND text LIKE ? "
                    f"ORDER BY ts {sort_dir} LIMIT ?",
                    (tid, f"%{search}%", limit + 1)
                ).fetchall()
            else:
                rows = db.conn.execute(
                    f"SELECT role, text, ts FROM messages WHERE topic_id=? "
                    f"ORDER BY ts {sort_dir} LIMIT ?",
                    (tid, limit + 1)
                ).fetchall()

        if not rows:
            return f"「{title}」(tid={tid[:8]}) 没有匹配的消息。"

        has_more = len(rows) > limit
        rows = rows[:limit]

        direction = "（从早到晚）" if asc else ""
        lines = [f"——「{title}」(tid={tid[:8]}) 的对话记录，共 {len(rows)} 条 {direction}——"]

        # For desc: rows are ts DESC, reverse to chronological. For asc: already chronological.
        display_rows = rows if asc else list(reversed(rows))
        for r in display_rows:
            role_label = "用户" if r["role"] == "user" else ("AI" if r["role"] == "ai" else r["role"])
            text = r["text"][:300] + ("..." if len(r["text"]) > 300 else "")
            lines.append(f"[{role_label}] {text}")

        if has_more and rows:
            if asc:
                latest = max(r["ts"] for r in rows)
                wall = f"\u2501" * 36
                lines.append(f"\n{wall} 📖 之后还有更多消息 — 继续往后翻 {wall}")
                lines.append(f"read_topic_messages(\"{tid[:8]}\", before_ts={latest}, limit={limit}, order='asc')")
            else:
                earliest = min(r["ts"] for r in rows)
                wall = f"\u2501" * 36
                lines.append(f"\n{wall} ⚠ 上下文边界 #{int(earliest)} — 更早的消息已截断 {wall}")
                lines.append(f"继续往前翻：read_topic_messages(\"{tid[:8]}\", before_ts={earliest}, limit={limit})")

        lines.append(
            "\n💡 提取完所需信息后：用 organize_context 整理这段翻出来的消息（避免长期占用上下文），"
            "再 organize_context 折叠本工具输出，然后继续当前任务。"
        )
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
        db = get_db()
        tid = db.get_meta("active_topic")
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
            from db import get_db
            tid = get_db().get_active_topic_id()
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
