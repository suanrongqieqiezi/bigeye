#!/usr/bin/env python3
"""
Domain Book — a book of domain/role instructions that AI can toggle on/off.
Multiple pages can be active at the same time; all active pages' content
is injected into the system prompt every round.
"""
import json
import os
import sys
from .registry import register_tool

_ROOT_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK_PATH = os.path.join(_ROOT_DIR, "data", "domain_book.json")

DEFAULT_BOOK = {
    "active_pages": [],
    "pages": {
        "default": {
            "title": "默认（无约束）",
            "tags": [],
            "content": "",
            "version": 1,
            "knowledge_refs": []
        }
    }
}


def _ensure_refs(book):
    """兼容存量：给所有页补 knowledge_refs 字段（缺省空列表）。"""
    for pg in book.get("pages", {}).values():
        if "knowledge_refs" not in pg:
            pg["knowledge_refs"] = []
    return book


def _load_book():
    """Load the domain book from disk."""
    if not os.path.exists(BOOK_PATH):
        _save_book(DEFAULT_BOOK)
        return dict(DEFAULT_BOOK)
    try:
        with open(BOOK_PATH, "r", encoding="utf-8") as f:
            return _ensure_refs(json.load(f))
    except Exception:
        return dict(DEFAULT_BOOK)


def _save_book(book):
    """Save the domain book to disk."""
    os.makedirs(os.path.dirname(BOOK_PATH), exist_ok=True)
    with open(BOOK_PATH, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)


# ── 任务级开页集（book_pages_overlay）────────────────
# domain_book.json 的 active_pages 是全局默认；每个任务可在 topic_meta
# 存一份覆盖集（整体替换语义，首次写入时以全局默认为基线副本）。
# 组装时：有覆盖集用覆盖集，否则用全局默认。

OVERLAY_KEY = "book_pages_overlay"


def _active_tid():
    try:
        from db import get_db
        return get_db().get_active_topic_id()
    except Exception:
        return None


def _get_overlay(tid):
    """读取任务覆盖集。无覆盖集返回 None（区别于空集 []）。"""
    if not tid:
        return None
    try:
        from db import get_db
        val = get_db().get_topic_meta(tid, OVERLAY_KEY)
        if val is None:
            return None
        data = json.loads(val)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _set_overlay(tid, pages):
    try:
        from db import get_db
        get_db().set_topic_meta(tid, OVERLAY_KEY, json.dumps(pages, ensure_ascii=False))
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def resolve_active_pages(book):
    """生效开页集 = 任务覆盖集（过滤已删页） || 全局默认。
    返回 (pages: list[str], source: "task" | "global")。"""
    tid = _active_tid()
    if tid:
        ov = _get_overlay(tid)
        if ov is not None:
            return [p for p in ov if p in book.get("pages", {})], "task"
    return list(book.get("active_pages", [])), "global"


def get_active_pages_info():
    """当前生效开页集的页面信息（任务覆盖集优先，回退全局默认）。
    Returns list of (title, page_id, content) tuples, or empty list."""
    book = _load_book()
    active, _src = resolve_active_pages(book)
    result = []
    for pid in active:
        if pid in book["pages"]:
            pg = book["pages"][pid]
            result.append((pg["title"], pid, pg["content"]))
    return result


# ── Tools ───────────────────────────────────────────

@register_tool(
    name="book_list_pages",
    description="查看领域说明书的目录。返回所有页面的ID、标题、标签和激活状态。想切换领域时先看看有什么可选的。",
    parameters={"type": "object", "properties": {}, "required": []}
)
def book_list_pages():
    book = _load_book()
    pages = book.get("pages", {})
    active, src = resolve_active_pages(book)
    if not pages:
        return "说明书是空的。用 book_create_page 创建一页。"
    src_label = "任务覆盖集，只影响当前任务" if src == "task" else "全局默认"
    lines = []
    for pid, pg in pages.items():
        status = "✅ 已开" if pid in active else "⏹ 关闭"
        tags = ", ".join(pg.get("tags", [])) if pg.get("tags") else "无标签"
        lines.append(f"  [{pid}] {pg['title']}（{tags}）{status}")
    return f"共 {len(pages)} 页，当前生效开页 {len(active)} 页（{src_label}）：\n" + "\n".join(lines)


@register_tool(
    name="book_read_page",
    description="读取某一页的完整内容。打开前建议先读一遍，确认符合需求。",
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "页面ID，从 book_list_pages 获取"}
        },
        "required": ["page_id"]
    }
)
def _fmt_knowledge_refs(page_id: str) -> str:
    """展示关联知识页：标题 + 版本 + 权威标记。失败优雅降级。"""
    try:
        from memory.knowledge_pages import get_page_meta
        book = _load_book()
        refs = book.get("pages", {}).get(page_id, {}).get("knowledge_refs", [])
        if not refs:
            return ""
        lines = [""]
        lines.append("关联知识页（藏书）：")
        metas = []
        for key in refs:
            meta = get_page_meta(key)
            metas.append((meta, key))
        # 权威优先：authority 降序，同权威按版本号降序
        metas.sort(key=lambda x: (x[0].get("authority", 0) if x[0] else 0,
                                  x[0].get("version", 0) if x[0] else 0), reverse=True)
        for meta, key in metas:
            if not meta:
                lines.append(f"  • {key}（知识页不存在）")
                continue
            auth = "◎权威" if meta.get("authority", 0) > 0 else ""
            lines.append(f"  • {meta.get('title', key)} v{meta.get('version', '?')} {auth}")
        return "\n".join(lines)
    except Exception:
        return ""


def book_read_page(page_id: str):
    book = _load_book()
    if page_id not in book["pages"]:
        return f"没有找到页面「{page_id}」。用 book_list_pages 查看所有可用页面。"
    pg = book["pages"][page_id]
    refs = _fmt_knowledge_refs(page_id)
    return f"=== {pg['title']}（{page_id}）v{pg.get('version', 1)} ===\n\n{pg['content']}{refs}"


@register_tool(
    name="book_turn_to",
    description="开关某一页。如果该页已开则关闭，如果已关则打开。可以同时开多页，所有打开的页面都会显示在提示词中。",
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "页面ID，从 book_list_pages 获取"}
        },
        "required": ["page_id"]
    }
)
def book_turn_to(page_id: str):
    book = _load_book()
    if page_id not in book["pages"]:
        return f"没有找到页面「{page_id}」。用 book_list_pages 查看所有可用页面，或用 book_create_page 创建新页面。"
    tid = _active_tid()
    if tid:
        # 任务段：从生效集（首次=全局默认副本）开始开关，写任务覆盖集，不碰全局
        active, _src = resolve_active_pages(book)
        scope_note = "任务覆盖集，只影响当前任务"
    else:
        active = list(book.get("active_pages", []))
        scope_note = "全局默认（当前无活跃任务）"
    if page_id in active:
        active.remove(page_id)
        if tid:
            if not _set_overlay(tid, active):
                return "写入任务覆盖集失败，开页状态未保存。请重试。"
        else:
            book["active_pages"] = active
            _save_book(book)
        return f"已关闭「{book['pages'][page_id]['title']}」。该页规则将不再生效（{scope_note}）。"
    active.append(page_id)
    pg = book["pages"][page_id]
    if tid:
        if not _set_overlay(tid, active):
            return "写入任务覆盖集失败，开页状态未保存。请重试。"
    else:
        book["active_pages"] = active
        _save_book(book)
    return f"已打开「{pg['title']}」（{scope_note}）。从现在起该页规则将生效。\n\n{pg.get('content', '')}"


@register_tool(
    name="book_search",
    description="按关键词搜索领域说明书的页面。不确定用什么领域时，先搜一下看看。",
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "搜索关键词，匹配页面标题、标签和内容"}
        },
        "required": ["keyword"]
    }
)
def book_search(keyword: str):
    book = _load_book()
    kw = keyword.lower()
    results = []
    for pid, pg in book.get("pages", {}).items():
        if pid == "default":
            continue
        if kw in pid.lower() or kw in pg["title"].lower():
            results.append((pid, pg["title"], "标题匹配"))
        elif any(kw in tag.lower() for tag in pg.get("tags", [])):
            results.append((pid, pg["title"], "标签匹配"))
        elif kw in pg.get("content", "").lower():
            idx = pg["content"].lower().index(kw)
            start = max(0, idx - 30)
            end = min(len(pg["content"]), idx + len(kw) + 30)
            snippet = pg["content"][start:end].replace("\n", " ")
            results.append((pid, pg["title"], f"内容匹配: ...{snippet}..."))
    if not results:
        return f"没有找到包含「{keyword}」的页面。"
    lines = [f"找到 {len(results)} 个匹配页面："]
    for pid, title, why in results:
        lines.append(f"  [{pid}] {title}（{why}）")
    return "\n".join(lines)


@register_tool(
    name="book_create_page",
    description="创建新的领域说明书页面。遇到新领域、新角色时，自己创造一页。创建后自动打开该页。",
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "页面唯一ID，简短英文，如 'writing_assistant'"},
            "title": {"type": "string", "description": "页面标题，一目了然，如 '写作助手'"},
            "content": {"type": "string", "description": "页面完整内容。告诉AI在这个领域/角色下应该怎么表现。包括：角色定义、回答规则、格式要求、注意事项等。"},
            "tags": {
                "type": "string",
                "description": "逗号分隔的标签，如 '写作,创意,文案'。帮助搜索时匹配。"
            }
        },
        "required": ["page_id", "title", "content"]
    }
)
def book_create_page(page_id: str, title: str, content: str, tags: str = ""):
    book = _load_book()
    if page_id in book["pages"]:
        return f"页面「{page_id}」已存在。用 book_edit_page 修改，或用其他 page_id 创建。"
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    book["pages"][page_id] = {
        "title": title,
        "tags": tag_list,
        "content": content,
        "version": 1
    }
    tid = _active_tid()
    if tid:
        active, _src = resolve_active_pages(book)
        if page_id not in active:
            active.append(page_id)
        _set_overlay(tid, active)
        _save_book(book)
        return f"已创建「{title}」（{page_id}）并自动打开（任务覆盖集，不影响全局默认，任务结束后该页回到关闭态）。该页规则从现在起生效。"
    active = book.get("active_pages", [])
    if page_id not in active:
        active.append(page_id)
        book["active_pages"] = active
    _save_book(book)
    return f"已创建「{title}」（{page_id}）并自动打开（全局默认）。该页规则从现在起生效。"


@register_tool(
    name="book_edit_page",
    description="修改已有领域说明书页面的内容。觉得规则过时了、或需要优化时调用。修改后版本号+1。",
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "要修改的页面ID"},
            "title": {"type": "string", "description": "新的标题（不修改则传空字符串）"},
            "content": {"type": "string", "description": "新的内容（不修改则传空字符串）"},
            "tags": {"type": "string", "description": "新的标签，逗号分隔（不修改则传空字符串）"}
        },
        "required": ["page_id"]
    }
)
def book_edit_page(page_id: str, title: str = "", content: str = "", tags: str = ""):
    book = _load_book()
    if page_id not in book["pages"]:
        return f"没有找到页面「{page_id}」。用 book_create_page 创建。"
    pg = book["pages"][page_id]
    if title:
        pg["title"] = title
    if content:
        pg["content"] = content
    if tags:
        pg["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    pg["version"] = pg.get("version", 1) + 1
    _save_book(book)
    return f"已更新「{pg['title']}」（{page_id}）v{pg['version']}。"


@register_tool(
    name="book_delete_page",
    description="删除领域说明书的一页。只删没用/过时/重复的页面，删前先 book_read_page 确认内容。"
                "默认页（default）不能删。删除后不可恢复。",
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "要删除的页面ID，从 book_list_pages 获取"},
            "confirm": {
                "type": "boolean",
                "description": "必须传 true 才会真删。默认 false 只预览要删的内容，不真删。",
                "default": False
            }
        },
        "required": ["page_id"]
    }
)
def book_delete_page(page_id: str, confirm: bool = False):
    book = _load_book()
    if page_id not in book["pages"]:
        return f"没有找到页面「{page_id}」。用 book_list_pages 查看所有页面。"
    if page_id == "default":
        return "默认页（default）不能删除。"

    pg = book["pages"][page_id]
    title = pg.get("title", "")
    content = pg.get("content", "")
    content_len = len(content)

    # 预览模式：不删，只展示要删的内容
    if not confirm:
        preview = content[:300] + ("…" if content_len > 300 else "")
        return (
            f"⚠️ 即将删除「{title}」（{page_id}）\n"
            f"内容长度: {content_len} 字\n"
            f"内容预览:\n{preview}\n\n"
            f"确认删除请再次调用，传 confirm=true。删除后不可恢复。"
        )

    # 真删：从 pages 移除 + 从全局默认和当前任务覆盖集移除
    del book["pages"][page_id]
    active = book.get("active_pages", [])
    if page_id in active:
        active.remove(page_id)
        book["active_pages"] = active
    tid = _active_tid()
    if tid:
        ov = _get_overlay(tid)
        if ov and page_id in ov:
            ov.remove(page_id)
            _set_overlay(tid, ov)
    _save_book(book)
    return f"已删除「{title}」（{page_id}）。如果误删，需要用 book_create_page 重建。"


@register_tool(
    name="book_link_knowledge",
    description="把知识页挂载到领域书页：领域书页的 knowledge_refs 追加知识页 page_key。"
                "挂载后 book_read_page 会展示关联知识页入口。可一次挂多个（逗号分隔）。",
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "领域书页面ID"},
            "knowledge_keys": {"type": "string", "description": "知识页 page_key 列表，逗号分隔。可用 knowledge_search 找 key。"}
        },
        "required": ["page_id", "knowledge_keys"]
    }
)
def book_link_knowledge(page_id: str, knowledge_keys: str):
    book = _load_book()
    if page_id not in book["pages"]:
        return f"没有找到页面「{page_id}」。用 book_list_pages 查看所有可用页面。"
    keys = [k.strip() for k in knowledge_keys.split(",") if k.strip()]
    refs = book["pages"][page_id].get("knowledge_refs", [])
    added = []
    missing = []
    for key in keys:
        try:
            from memory.knowledge_pages import get_page_meta
            meta = get_page_meta(key)
        except Exception:
            meta = None
        if not meta:
            missing.append(key)
            continue
        if key not in refs:
            refs.append(key)
            added.append(f"{key}({meta.get('title', '')})")
    book["pages"][page_id]["knowledge_refs"] = refs
    _save_book(book)
    msg = f"已挂载 {len(added)} 个知识页到「{book['pages'][page_id]['title']}」"
    if added:
        msg += ": " + ", ".join(added)
    if missing:
        msg += f"\n未找到（已跳过）: {', '.join(missing)}"
    return msg


@register_tool(
    name="book_unlink_knowledge",
    description="从领域书页移除知识页挂载。knowledge_key 是知识页 page_key。",
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "领域书页面ID"},
            "knowledge_key": {"type": "string", "description": "要移除的知识页 page_key"}
        },
        "required": ["page_id", "knowledge_key"]
    }
)
def book_unlink_knowledge(page_id: str, knowledge_key: str):
    book = _load_book()
    if page_id not in book["pages"]:
        return f"没有找到页面「{page_id}」。用 book_list_pages 查看所有可用页面。"
    refs = book["pages"][page_id].get("knowledge_refs", [])
    if knowledge_key not in refs:
        return f"「{knowledge_key}」未挂载在「{book['pages'][page_id]['title']}」上。当前挂载: {', '.join(refs) or '无'}"
    refs.remove(knowledge_key)
    book["pages"][page_id]["knowledge_refs"] = refs
    _save_book(book)
    return f"已从「{book['pages'][page_id]['title']}」移除「{knowledge_key}」。"
