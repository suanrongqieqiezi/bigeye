#!/usr/bin/env python3
"""
tool_router.py — 动态工具检索（长尾工具按意图注入）。

背景：工具越来越多，全量常驻不现实。借鉴 LangChain tool_retriever +
MCP 按需发现：CORE/PROTECTED/META 常驻（系统提示引用，不可裁），
SCENE + FOLDED（~63 个长尾）进入向量索引，每轮按用户最新意图检索
top-K 注入 schema，其余不占上下文。discover_tools 保留兜底。

基础设施复用：
  - memory/embedder.py  embed(text) → 512 维向量（ONNX BGE-small-zh）
  - memory/vec_index.py  VecIndex      sqlite-vec KNN 余弦检索

流程：
  build_index()  → 给所有长尾工具描述生成向量，存 sqlite-vec 表
  retrieve(query) → embed query → KNN → 返回最相关工具名列表
"""
import json
import os
import sys
import threading

# 允许独立运行/自测时找到项目根下的 memory/tools 包
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
ROOT_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else ROOT
INDEX_DB = os.path.join(ROOT_DIR, "data", "tool_index.db")
INDEX_TABLE = "idx_tool_defs"

# 每轮默认注入的长尾工具数（其余走 discover_tools 兜底）
DEFAULT_TOP_K = 8

# 进程内缓存：name -> {"rowid": int, "embedding": list}
_index_cache: dict[str, dict] = {}
_index_lock = threading.Lock()
_index_built = False


def _candidate_tools():
    """长尾工具集 = SCENE_TOOLS ∪ FOLDED 全部，排除常驻/元工具。"""
    from tools.registry import (
        _tools, CORE_TOOLS, PROTECTED_TOOLS, META_TOOLS,
        SCENE_TOOLS, get_folded_tool_names,
    )
    exclude = CORE_TOOLS | PROTECTED_TOOLS | META_TOOLS
    folded = get_folded_tool_names()
    names = set(SCENE_TOOLS) | set(folded)
    return [n for n in names if n in _tools and n not in exclude]


def build_index(force: bool = False) -> int:
    """为所有长尾工具生成向量索引。返回索引的工具数。

    进程内只构建一次；force=True 强制重建（工具注册变化后调用）。
    sqlite-vec 不可用时退化为纯内存索引（仍可检索，只是 KNN 变全扫描）。
    """
    global _index_built
    if _index_built and not force:
        return len(_index_cache)

    from memory.embedder import embed
    from memory.vec_index import VecIndex

    names = _candidate_tools()
    idx = VecIndex(INDEX_DB)
    has_vec = idx.ensure_table(INDEX_TABLE, 512)

    with _index_lock:
        _index_cache.clear()
        rowid = 1
        for name in names:
            t = _tools_get(name)
            if not t:
                continue
            desc = t.get("description") or ""
            # 检索文本 = 工具名 + 描述（BGE 对中英都有效）
            text = f"{name}: {desc}"
            try:
                emb = embed(text)
            except Exception:
                emb = None
            if not emb or not any(emb):
                continue  # 零向量跳过（embed 后端全挂时索引为空，检索自然降级）
            _index_cache[name] = {"rowid": rowid, "embedding": emb}
            if has_vec:
                try:
                    idx.insert(INDEX_TABLE, rowid, emb)
                except Exception:
                    pass  # 索引表写入失败不影响内存检索
            rowid += 1
        _index_built = True
    return len(_index_cache)


def _tools_get(name):
    """安全取工具定义（避免 import 循环）。"""
    try:
        from tools.registry import _tools
        return _tools.get(name, {}).get("definition")
    except Exception:
        return None


def retrieve(query: str, top_k: int = DEFAULT_TOP_K) -> list[str]:
    """按 query 检索最相关的长尾工具名。返回空列表表示无可用索引。"""
    if not query or not query.strip():
        return []
    build_index()  # lazy 构建

    from memory.embedder import embed
    from memory.vec_index import VecIndex

    try:
        qemb = embed(query.strip())
    except Exception:
        qemb = None
    if not qemb or not any(qemb) or not _index_cache:
        return []

    # 优先 sqlite-vec KNN；失败退化为内存全扫描（工具数 ~63，全扫也快）
    idx = VecIndex(INDEX_DB)
    try:
        hits = idx.query(INDEX_TABLE, qemb, top_k=top_k)
        if hits:
            rowid_to_name = {v["rowid"]: k for k, v in _index_cache.items()}
            names = [rowid_to_name.get(h["rowid"]) for h in hits]
            return [n for n in names if n]
    except Exception:
        pass

    # 内存余弦全扫描兜底
    from memory.embedder import cosine_sim
    scored = []
    for name, rec in _index_cache.items():
        sim = cosine_sim(qemb, rec["embedding"])
        scored.append((sim, name))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [n for _, n in scored[:top_k]]


def invalidate():
    """工具注册集合变化后调用，下次检索时重建索引。"""
    global _index_built
    _index_built = False
    with _index_lock:
        _index_cache.clear()


def index_stats() -> dict:
    """索引状态（供诊断/测试）。"""
    return {
        "built": _index_built,
        "indexed": len(_index_cache),
        "db": INDEX_DB,
        "table": INDEX_TABLE,
        "top_k_default": DEFAULT_TOP_K,
    }


if __name__ == "__main__":
    # 独立自测：先加载全部工具模块（否则 _tools 为空，索引 0 个）
    import importlib
    for _mod in ["tools.web_search", "tools.bash", "tools.memory_tools",
                 "tools.file_tools", "tools.web_fetch", "tools.task_tools",
                 "tools.discover", "tools.edit_tool", "tools.check_python",
                 "tools.code_ast", "tools.file_search", "tools.template_engine",
                 "tools.domain_book_tools", "tools.important_matters",
                 "tools.perspective", "tools.task_v4_tools", "tools.mindmap_tools",
                 "tools.meta_tools", "tools.ask_user", "tools.focus_tools",
                 "tools.image_gen_tool", "tools.book_import_tool", "tools.file_history",
                 "tools.rules_engine_tools", "tools.reminder_tools", "tools.workspace_tools"]:
        try:
            importlib.import_module(_mod)
        except Exception as _e:
            print(f"[selftest] skip {_mod}: {_e}")
    n = build_index()
    print(f"indexed tools: {n}")
    print(index_stats())
    for q in ["修改任务名字", "查看思维导图历史", "删除一条记忆", "折叠一段对话"]:
        print(f"query '{q}' → {retrieve(q, 3)}")
