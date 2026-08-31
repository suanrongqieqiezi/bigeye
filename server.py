#!/usr/bin/env python3
"""
大眼X Server — 全能超级个体。
直接 LLM API 调用 + 工具系统 + 记忆系统。
完全取代 rpc_server.py + ompQ.exe 的组合。
"""
import json
import os
import sys
import time
import datetime
import uuid
import re
import socket
import signal
import threading
import http.server
import urllib.request
import urllib.parse
import traceback
import queue
import ssl
from collections import deque

# 部分环境（企业 VPN / 抓包软件 / 杀软）会向 HTTPS 链路注入自签名证书，
# 导致 urllib 默认证书校验失败。本服务为本地部署，禁用证书校验可接受。
_UNVERIFIED_SSL_CTX = ssl.create_default_context()
_UNVERIFIED_SSL_CTX.check_hostname = False
_UNVERIFIED_SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Path setup ──────────────────────────────────────
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else BASE_DIR
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

# ── Ports ───────────────────────────────────────────
PORT = 8765

# ── Imports ─────────────────────────────────────────
sys.path.insert(0, BASE_DIR)
from db import get_db, Database
from llm import LLMConfig, chat_stream, chat_stream_cached, _build_messages, is_transient_conn_error, build_multimodal_content, strip_image_content, image_mime_type, is_vision_model
from tools.registry import get_tool_defs, execute_tool, register_tool
import loop_detector  # 目标级打转检测（探索类占比高且零落地 → 注入提醒）
# Import tool modules to register them
import tools.web_search
import tools.bash
import tools.memory_tools
import tools.file_tools
import tools.web_fetch
import tools.task_tools
import tools.discover
import tools.edit_tool
import tools.check_python
import tools.code_ast  # 代码 AST 工具（code_ast_parse/code_find_defs/code_get_symbol）
import tools.file_search  # 文件搜索工具（file_search）
import tools.template_engine  # 模板引擎工具（list_templates/create_from_template/preview_template）

import tools.domain_book_tools
import tools.important_matters
import tools.perspective

# Import v4.0 task execution tools
import tools.task_v4_tools
import tools.mindmap_tools  # 思维导图工具（与 DAG 对齐的写通道）
import tools.meta_tools  # 元工具：discover_tools + execute_advanced_tool（工具折叠）
import tools.ask_user  # 阻塞式提问工具（ask_user，折叠分组 interaction）
import tools.focus_tools  # 注意力等级工具（set_focus_level）
import tools.spawn_agent  # 子代理工具（spawn_agent）
import tools.rules_engine_tools  # 规则引擎管理工具（rule_list/rule_add/rule_update/rule_delete）


def _get_matters():
    from tools.important_matters import get_matters
    return get_matters()


def _set_matters(entries):
    from tools.important_matters import set_matters
    set_matters(entries)

# Register additional tools
import tools.image_gen_tool
# Memory (lazy) — all stores are initialized here to ensure their schemas exist
_memory_store = None
_memory_enabled = False

def _init_all_memory_stores():
    """Initialize ALL memory stores (not just fragment_store).

    Each store module self-creates its tables in __init__.
    Before this fix, EntityStore, RelationStore, and SummaryTree
    were never instantiated — their tables didn't exist in the DB.
    """
    global _memory_store, _memory_enabled
    if _memory_store is not None:
        return _memory_store

    try:
        from memory.fragment_store import get_store
        _memory_store = get_store()
        _memory_enabled = True
    except Exception as e:
        print(f"[memory] fragment_store init failed: {e}")
        _memory_enabled = False
        return _memory_store

    # Init additional stores — each will CREATE TABLE IF NOT EXISTS
    for name, module in [
        ("entity_store.EntityStore", "memory.entity_store"),
        ("relation_store.RelationStore", "memory.relation_store"),
        ("summary_tree.SummaryTree", "memory.summary_tree"),
        ("file_crystal_store.FileCrystalStore", "memory.file_crystal_store"),
        ("reflection_loop.ReflectionLoop", "memory.reflection_loop"),
        ("cmn_retriever.CMNRetriever", "memory.cmn_retriever"),
    ]:
        try:
            mod = __import__(module, fromlist=[name.split(".")[0]])
            cls_name = name.split(".")[1]
            getattr(mod, cls_name)()  # instantiate → _ensure_schema()
            print(f"[memory] {name} initialized OK")
        except Exception as e:
            print(f"[memory] {name} init failed: {e}")

    # Preload sentence-transformers model (lazy loaded in embedder)
    try:
        from memory.embedder import _load_st_model
        _load_st_model()
        print(f"[memory] embedding model loaded")
    except Exception as e:
        print(f"[memory] embedding model preload failed: {e}")

    return _memory_store

def _get_memory_store():
    return _init_all_memory_stores()


PER_MESSAGE_COMPRESS_CHARS = 500    # 单条消息超过此字数自动整理
TOKEN_LIMIT = 200000               # 上下文 token 预算（chars/2 ≈ tokens）
COMPRESS_WARN_PCT = 60             # 上下文警告阈值百分比
TURN_BUDGET = 50                   # 单次对话轮次预算
MSG_COUNT_WARN = 15                # 未整理消息条数警告阈值
MSG_COUNT_WALL = 0                 # 消息条数墙阈值（0=禁用，仅用 token 预算）
_VERIFY_SSL = True                 # 是否校验 HTTPS 证书（False=跳过，用于 VPN/抓包等证书注入环境）
_DEV_MODE = False                  # 开发者模式：开启后才记录 API 原始日志和 HTTP 访问日志
_LLM_CACHE_ENABLED = False         # LLM 响应本地缓存：开启后全链路命中即秒回，避免重复消费
ESTIMATE_SAFETY = 0.85  # 估算安全余量：实际 token 数可能比 _estimate_tokens 高 ~15%
_INTERMEDIATE_MAX = 8000  # 中间过程叙述上限（字符）：工具轮输出累积超此值丢弃，防止几十万字符单条消息炸工作记忆


def _append_intermediate(buf, s):
    """累积中间过程叙述，带上限保护：超出上限丢弃后续，保留截断标记。"""
    if not s:
        return buf
    if len(buf) >= _INTERMEDIATE_MAX:
        return buf
    room = _INTERMEDIATE_MAX - len(buf)
    s = s.rstrip("\n")
    if len(s) + 1 > room:
        s = s[:room - 1] + "…[中间输出过长已截断]…"
    return buf + s + "\n"


def _resolve_workspace(ws_rel, topic_id=None):
    """统一解析工作区路径：支持绝对路径和相对路径。
    相对路径基于 ROOT_DIR 解析；绝对路径直接用。
    避免跨盘符时 os.path.relpath 抛异常。
    搬电脑后自动匹配：绝对路径不存在时，尝试在 data/missions/{topic_id}/workspace 下找到。
    """
    if not ws_rel:
        return ROOT_DIR
    # Windows: 'C:'（无反斜杠）是当前目录相对路径，补成 'C:\'
    if os.name == 'nt' and len(ws_rel) == 2 and ws_rel[1] == ':':
        ws_rel = ws_rel + '\\'
    if os.path.isabs(ws_rel):
        abs_path = os.path.abspath(ws_rel)
        if os.path.isdir(abs_path):
            return abs_path
        # 绝对路径不存在，尝试自动匹配：从路径中提取 mission_id，在 data/missions/ 下找
        if topic_id:
            fallback = os.path.join(ROOT_DIR, "data", "missions", topic_id, "workspace")
            if os.path.isdir(fallback):
                return fallback
        # 也尝试在 ROOT_DIR 下按原始相对结构找
        # 例如旧路径 E:\B\bigeyeinfo\bigeye\data\missions\{id}\workspace
        # 提取 data\missions\{id}\workspace 部分
        norm = os.path.normpath(ws_rel)
        parts = norm.split(os.sep)
        for i, part in enumerate(parts):
            if part == "data" and i + 1 < len(parts) and parts[i + 1] == "missions":
                rel_from_data = os.sep.join(parts[i:])
                candidate = os.path.join(ROOT_DIR, rel_from_data)
                if os.path.isdir(candidate):
                    return candidate
        return abs_path
    return os.path.abspath(os.path.join(ROOT_DIR, ws_rel))
def _effective_limit():
    """Effective budget limit with safety margin for estimation error."""
    return int(TOKEN_LIMIT * ESTIMATE_SAFETY)
_last_request_tokens = 0  # input tokens of most recent API request
_last_hard_tokens = 0     # 最近一次请求的硬性内容实测 tokens（系统提示+工具schema）

# ── 模型上下文窗口（工作记忆窗口上限）──
# 已知模型上下文窗口（tokens）；未登记模型回退默认值
_MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-pro": 1000000,
    "deepseek-v4-flash": 1000000,
    "deepseek-chat": 1000000,
    "deepseek-reasoner": 1000000,
    "glm-5.3-flash": 1000000,
}
_DEFAULT_CONTEXT_WINDOW = 128000


def _model_context_window(model_id):
    """查模型上下文窗口；未知模型前缀匹配，仍未知用默认值。"""
    if not model_id:
        return _DEFAULT_CONTEXT_WINDOW
    mid = model_id.lower()
    if mid in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[mid]
    for k, v in _MODEL_CONTEXT_WINDOWS.items():
        if mid.startswith(k) or k.startswith(mid):
            return v
    return _DEFAULT_CONTEXT_WINDOW


def _wm_min_token_limit():
    """工作记忆容量最小可设置值 = 硬性内容实测（系统提示+工具schema）+ 其它2000 + 对话记忆1000。
    无实测值（刚启动未跑过请求）时回退 20000。"""
    if not _last_hard_tokens:
        return 20000
    return _last_hard_tokens + 3000
_round_peak_tokens = 0  # 当前大轮工作记忆峰值：用户新消息后重置，LLM 返回时取 max
_recent_input_tokens = deque(maxlen=10)  # 最近10次 LLM 请求真实 input_tokens（内存，M1/M2 实时指标数据源）
_last_request_body: dict[str, str] = {}  # topic_id → JSON of last request msgs

_compress_config_loaded = False


def _load_compress_config(db=None):
    """Load compress settings from DB into module globals. Idempotent after first load."""
    global PER_MESSAGE_COMPRESS_CHARS, TOKEN_LIMIT, COMPRESS_WARN_PCT, TURN_BUDGET, MSG_COUNT_WARN, MSG_COUNT_WALL
    global _VERIFY_SSL, _DEV_MODE, _compress_config_loaded, _LLM_CACHE_ENABLED
    try:
        db = db or get_db()
        PER_MESSAGE_COMPRESS_CHARS = int(db.get_meta("compress_per_msg_chars") or 500)
        TOKEN_LIMIT = int(db.get_meta("compress_token_limit") or 200000)
        # 钳制到 [最小可设值, 模型上下文窗口]
        TOKEN_LIMIT = max(TOKEN_LIMIT, _wm_min_token_limit())
        TOKEN_LIMIT = min(TOKEN_LIMIT, _model_context_window(get_model_config().model))
        COMPRESS_WARN_PCT = int(db.get_meta("compress_warn_pct") or 60)
        TURN_BUDGET = int(db.get_meta("compress_turn_budget") or 50)
        MSG_COUNT_WARN = int(db.get_meta("compress_msg_count_warn") or 15)
        MSG_COUNT_WALL = int(db.get_meta("compress_msg_count_wall") or 0)
        _VERIFY_SSL = db.get_meta("verify_ssl") != "0"
        _DEV_MODE = db.get_meta("dev_mode") == "1"
        try:
            import api_logger
            api_logger.set_enabled(_DEV_MODE)
        except Exception:
            pass
        _LLM_CACHE_ENABLED = db.get_meta("llm_cache_enabled") == "1"
        try:
            import llm as _llm_mod
            _llm_mod.set_cache_enabled(_LLM_CACHE_ENABLED)
        except Exception:
            pass
    except Exception:
        pass  # keep defaults if DB not ready


def _refresh_compress_config():
    """Force reload compress config from DB (called after user saves settings)."""
    global _compress_config_loaded
    _compress_config_loaded = False
    _load_compress_config()


# ── Tool enable/disable (per-user setting) ──────────
_tools_enabled_cache = None  # set[str] | None  (None = 全部启用)


def _get_enabled_tool_names():
    """Return the set of enabled tool names from DB meta.
    None or empty = all enabled (backward compatible).
    """
    global _tools_enabled_cache
    if _tools_enabled_cache is not None:
        return _tools_enabled_cache
    try:
        db = get_db()
        raw = db.get_meta("tools_enabled")
        if not raw:
            _tools_enabled_cache = None  # 未配置 = 全启用
        else:
            _tools_enabled_cache = set(json.loads(raw))
    except Exception:
        _tools_enabled_cache = None
    return _tools_enabled_cache


def _get_scene_tools(tid=None):
    """按话题场景动态注入工具（场景裁剪 v2）。

    规则：
    - 有 DAG 文件（活跃任务）→ 注入任务执行工具 start_node/complete_node/finish_task/list_topics
    - 有思维导图 → 注入 add_mindmap_node
    - 最近 20 轮用过记忆/上下文工具 → 注入 trace_memory/remember_knowledge（自举：用过后保留）
    其余场景工具在折叠组可 discover 到，不注入也不丢失可达性。
    """
    if not tid:
        return set()
    scene = set()
    try:
        from tools.registry import SCENE_TOOLS
        # 任务场景：话题有 DAG 文件（活跃任务）
        dag_path = os.path.join(ROOT_DIR, "data", "missions", tid, "dag.json")
        if os.path.exists(dag_path):
            scene |= {"start_node", "complete_node", "finish_task", "list_topics"}
        # 导图场景：话题有思维导图
        mm_path = os.path.join(ROOT_DIR, "data", "missions", tid, "mindmap.json")
        if os.path.exists(mm_path):
            scene |= {"add_mindmap_node"}
        # 记忆场景：最近用过记忆/上下文工具（自举：用过后保留）
        db = get_db()
        usage = json.loads(db.get_topic_meta(tid, "tool_usage") or "[]")
        if set(usage) & {"crystal_recall", "remember", "organize_context", "expand_compressed"}:
            scene |= {"trace_memory", "remember_knowledge"}
    except Exception:
        pass
    return scene & SCENE_TOOLS





# ── 动态工具检索：按用户意图注入长尾工具（记忆网络复用）──
_dynamic_tool_cache: dict = {}
_dynamic_tool_cache_key = ""


def _get_dynamic_tool_names(tid=None, top_k: int = 8):
    """按最近用户消息的语义检索长尾工具名（向量索引）。

    复用 memory/embedder + vec_index（与记忆系统同一条向量链路）：
    - query = 最近一条用户消息（意图）；消息相同则命中缓存不重算
    - 返回 top_k 个最相关工具名；无索引/失败时返回空集（不阻塞主流程）
    """
    global _dynamic_tool_cache, _dynamic_tool_cache_key
    if not tid:
        return set()
    try:
        db = get_db()
        msgs = db.get_messages(tid, limit=6, skip_hidden=True, skip_process=True)
        # 取最近一条非空用户消息作为意图 query
        query = ""
        for m in reversed(msgs):
            role = m.get("role") if isinstance(m, dict) else m[1]
            text = m.get("text") if isinstance(m, dict) else m[2]
            if role == "user" and text and text.strip() and not text.startswith("["):
                query = text.strip()[-2000:]  # 截断防超长
                break
        if not query:
            return set()
        key = f"{tid}:{query}"
        if key == _dynamic_tool_cache_key and _dynamic_tool_cache:
            return _dynamic_tool_cache
        from tools.tool_router import retrieve
        names = retrieve(query, top_k=top_k)
        _dynamic_tool_cache = set(names)
        _dynamic_tool_cache_key = key
        return _dynamic_tool_cache
    except Exception as e:
        print(f"[tool_router] dynamic retrieve failed: {e}")
        return set()


def _record_tool_usage(tid, tool_name):
    """记录话题最近使用的工具（去重，保留最近 20 个），供场景工具自举判断。"""
    if not tid or not tool_name:
        return
    try:
        db = get_db()
        usage = json.loads(db.get_topic_meta(tid, "tool_usage") or "[]")
        if tool_name in usage:
            usage.remove(tool_name)
        usage.append(tool_name)
        db.set_topic_meta(tid, "tool_usage", json.dumps(usage[-20:], ensure_ascii=False))
    except Exception:
        pass


def _estimate_task_complexity(tid=None):
    """按任务 DAG 节点数评估复杂度，返回建议注意力等级（10/12）或 None（不降级）。

    复杂任务（节点多）自动降级减少干扰；无任务/简单任务保持 15 全量。
    """
    if not tid:
        return None
    try:
        dag_path = os.path.join(ROOT_DIR, "data", "missions", tid, "dag.json")
        if not os.path.exists(dag_path):
            return None
        with open(dag_path, encoding="utf-8") as f:
            dag = json.load(f)
        n = len(dag.get("nodes") or [])
        if n >= 25:
            return 10
        if n >= 12:
            return 12
    except Exception:
        pass
    return None


def _get_enabled_tool_defs(tid=None):
    """Return tool defs for LLM: 常驻工具 + 场景工具 + 元工具（工具折叠模式）。

    工具折叠机制（借鉴 OpenAI namespace + Codex tool_search）：
    - 常驻工具（CORE 16 + PROTECTED 13 = 29 个高频核心）始终暴露给 LLM
    - 场景工具（7 个低频）按话题状态动态注入（有 DAG/导图/记忆历史时）
    - 折叠工具（~56 个低频）通过 discover_tools(group) 按需发现
    - 元工具：discover_tools + execute_advanced_tool

    若用户设置了 tools_enabled 白名单，则按白名单过滤常驻工具，
    但元工具始终保留（否则折叠工具无法访问）。
    """
    from tools.registry import get_always_on_tool_defs, META_TOOLS, _tools
    # 常驻工具定义（CORE + PROTECTED）
    defs = get_always_on_tool_defs()
    # 场景工具定义（按话题状态动态注入，保证可达性）
    scene = _get_scene_tools(tid)
    if scene:
        defs = defs + [t["definition"] for name, t in _tools.items() if name in scene]
    # 动态工具检索：按最近用户意图注入长尾工具（向量索引，记忆网络复用）
    _retrieved = _get_dynamic_tool_names(tid)
    if _retrieved:
        defs = defs + [t["definition"] for name, t in _tools.items() if name in _retrieved]
    # 元工具定义（discover_tools + execute_advanced_tool）
    meta_defs = [t["definition"] for name, t in _tools.items() if name in META_TOOLS]
    all_defs = defs + meta_defs
    # 去重（场景/检索/元工具可能有交集，如 start_node 同时在 SCENE 和折叠组）
    seen, unique = set(), []
    for d in all_defs:
        n = d.get("name")
        if n not in seen:
            seen.add(n)
            unique.append(d)
    all_defs = unique

    enabled = _get_enabled_tool_names()
    if enabled is None:
        # 默认：返回常驻 + 场景 + 元工具（折叠模式）
        return all_defs
    # 用户自定义白名单：按白名单过滤，但元工具强制保留
    enabled_with_meta = set(enabled) | META_TOOLS
    return [d for d in all_defs if d.get("name") in enabled_with_meta]


def _refresh_tools_enabled():
    """Force reload tools_enabled cache (called after user saves settings)."""
    global _tools_enabled_cache
    _tools_enabled_cache = None


# ── Per-message compression ─────────────────────────

_compressed_originals_cache: dict[str, dict] = {}  # topic_id -> {anchor: original_text}
_compressed_originals_lock = threading.Lock()


def _is_流水账(text: str) -> tuple[bool, int]:
    """Detect stream-of-consciousness / log-like content.

    Returns (is_流水账, estimated_repeated_lines).
    """
    lines = text.split("\n")
    if len(lines) < 4:
        return False, 0
    # Check for many short similar lines (logs, data rows)
    short_lines = sum(1 for l in lines if 5 < len(l.strip()) < 120)
    if short_lines > len(lines) * 0.6 and short_lines >= 5:
        return True, short_lines
    # Check for high repetition ratio (same prefix pattern)
    if len(lines) >= 6:
        prefixes = set()
        for l in lines[:20]:
            prefix = l.strip()[:30]
            if prefix:
                prefixes.add(prefix)
        if len(prefixes) <= max(2, len(lines[:20]) * 0.2):
            return True, len(lines)
    return False, 0



def _llm_summarize(text: str, llm_config=None) -> str | None:
    """Use the model itself to compress a message. Context-free call — only the message text.

    The model uses its own judgment to decide what key info to keep.
    Returns None if unavailable (falls back to mechanical compression).
    走 chat_once_cached 缓存层：相同消息重复整理（多次触发上下文压缩）时命中秒回。
    """
    if not llm_config:
        # 调用方没传配置时，自己从 model_config.json 拉取，否则永远 None（之前修了一半的 bug）
        try:
            llm_config = get_model_config()
        except Exception:
            return None
    if not llm_config or not llm_config.api_key or not llm_config.base_url:
        return None
    try:
        from llm import chat_once_cached
        prompt = "用你自己的话整理下面这条消息，保留关键信息。控制在50-100字。只输出整理结果，不要前缀。"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:3000]},
        ]
        result = chat_once_cached(llm_config, messages).get("text", "")
        if result and len(result) > 5:
            return result.strip()
        return None
    except Exception:
        return None

# ── 空态智能建议引擎 ──
# 后台按频率静默分析最近任务/记忆，生成一批"AI 想建议用户做的事"存 meta；
# 前端每次开新任务随机取一条当活标语。一次分析 ~2k tok，摊薄到几百次打开≈0。
_SUGGESTION_FALLBACK = [
    "接下来做点什么？",
    "有什么想推进的事吗？",
    "新任务，从哪件事开始？",
    "说说接下来想搞定的事",
]
_SUGGESTION_FREQ_HOURS = {"12h": 12, "1d": 24, "3d": 72, "7d": 168}


def _suggestion_cfg():
    db = get_db()
    try:
        cfg = json.loads(db.get_meta("suggestion_cfg") or "{}")
    except Exception:
        cfg = {}
    return {
        # 兼容历史脏数据：曾被写回成 JSON bool（str(True)=="True"!="1"导致开关失忆）
        "enabled": str(cfg.get("enabled", "1")).lower() in ("1", "true"),
        "freq": str(cfg.get("freq", "1d")),
    }


def _suggestion_pool():
    try:
        return json.loads(get_db().get_meta("suggestion_pool") or "[]")
    except Exception:
        return []


def _suggestion_template():
    """0 token 兜底：时段×星期模板，保证空态永远有活话。"""
    import random
    h = datetime.datetime.now().hour
    wd = datetime.datetime.now().weekday()
    if h < 6:
        pool = ["夜深了，慢点来", "这么晚还在忙？注意节奏"]
    elif h < 11:
        pool = ["早上好，今天先推哪件事？", "新的一天，从最重要的事开始"]
    elif h < 14:
        pool = ["午后了，推进点什么？", "中午过完，来件小事热热手？"]
    elif h < 18:
        pool = ["下午好，继续哪条线？", "趁下午状态好，啃块硬骨头？"]
    else:
        pool = ["晚上好，收个尾还是开个新坑？", "今晚想搞定点什么？"]
    if wd >= 5:
        pool += ["周末了，做点轻松的？", "周末时间，留给想做的事"]
    return random.choice(pool)


def _suggestion_context():
    """给分析模型看的素材：最近任务 + 重要事项 + 记忆统计。"""
    db = get_db()
    lines = []
    try:
        rows = db._fetchall(
            "SELECT title, last_message, updated_at FROM topics "
            "WHERE last_message IS NOT NULL AND last_message != '' "
            "ORDER BY updated_at DESC LIMIT 8")
        for r in rows:
            t = datetime.datetime.fromtimestamp(r["updated_at"] or 0)
            lines.append(f"- [{t.month}月{t.day}日] {r['title'] or '新任务'}：{str(r['last_message'])[:80]}")
    except Exception:
        pass
    try:
        for m in _get_matters()[:5]:
            lines.append(f"- 重要事项：{str(m.get('title') or m.get('content') or '')[:60]}")
    except Exception:
        pass
    try:
        st = db._fetchone("SELECT COUNT(*) AS c FROM memory_fragments")
        if st and st["c"]:
            lines.append(f"- 记忆库现有 {st['c']} 条碎片/晶体")
    except Exception:
        pass
    return "\n".join(lines) or "（暂无历史任务数据）"


def generate_suggestions_now():
    """调一次模型生成 10 条建议，写入 meta。返回 (ok, msg)。"""
    cfg_model = get_model_config()
    if not cfg_model or not cfg_model.api_key or not cfg_model.base_url:
        return False, "未配置模型，无法生成"
    ctx = _suggestion_context()
    prompt = (
        "你是用户的私人助理，下面是用户最近的任务和记忆概况。\n"
        "请站在助理角度，生成 10 条「接下来可以做的事」的建议，用于新任务开场白。\n"
        "要求：每条不超过 20 字、口语化、具体（可引用具体任务名/事项）、不重复、"
        "混合「继续旧事/开启新事/提醒关怀」三类。\n"
        "只输出 JSON 数组，如 [\"……\",\"……\"]，不要其他文字。\n\n"
        f"最近情况：\n{ctx}"
    )
    try:
        url = f"{cfg_model.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg_model.api_key}",
        }
        body = json.dumps({
            "model": cfg_model.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "max_tokens": 400,
            "temperature": 0.9,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30,
                                    context=None if _VERIFY_SSL else _UNVERIFIED_SSL_CTX) as resp:
            data = json.loads(resp.read())
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        m = re.search(r"\[.*\]", text, re.S)
        items = json.loads(m.group(0)) if m else []
        items = [str(x).strip() for x in items if str(x).strip()][:10]
        if len(items) < 3:
            return False, "模型返回内容不足"
        db = get_db()
        db.set_meta("suggestion_pool", json.dumps(items, ensure_ascii=False))
        db.set_meta("suggestion_gen_at", str(time.time()))
        return True, f"已生成 {len(items)} 条建议"
    except Exception as e:
        return False, f"生成失败：{e}"


def _suggestion_refresh_loop():
    """后台守护：启动 20s 后检查一次，之后每小时检查，到期且开启则静默生成。"""
    time.sleep(20)
    while True:
        try:
            cfg = _suggestion_cfg()
            db = get_db()
            last = float(db.get_meta("suggestion_gen_at") or 0)
            hours = _SUGGESTION_FREQ_HOURS.get(cfg["freq"], 24)
            if cfg["enabled"] and (time.time() - last) > hours * 3600:
                ok, msg = generate_suggestions_now()
                print(f"[大眼X] [suggestion] auto refresh: {msg}")
        except Exception as e:
            print(f"[大眼X] [suggestion] refresh err: {e}")
        time.sleep(3600)


def _auto_compress(text: str, llm_callable=None, force=False) -> tuple[str, str | None]:
    """Compress a single message if it exceeds threshold.

    If llm_callable provided, the model's own intelligence drives the summary.
    Otherwise falls back to mechanical truncation.

    When force=True (model-requested fold), skips the length threshold.

    Returns (display_text, original_text_or_None).
    """
    text = text.strip()
    if not force and (not text or len(text) <= PER_MESSAGE_COMPRESS_CHARS):
        return text, None

    # Even with force=True, don't "compress" if the overhead would make it longer
    if len(text) < 60:
        return text, None

    # Try model-driven compression first — preserves the model's soul
    if llm_callable:
        try:
            summary = llm_callable(text)
            if summary and len(summary) > 5:
                compressed = f"[已整理 原文{len(text)}字 ↕] {summary[:300]}"
                return compressed, text
        except Exception:
            pass

    # Mechanical fallback — model unavailable or call failed
    is_log, _ = _is_流水账(text)

    if is_log:
        lines = text.split("\n")
        n = len(lines)
        head = [l for l in lines[:3] if l.strip()]
        tail = lines[-1].strip() if lines[-1].strip() else ""
        summary = f"共 {n} 行"
        if head:
            summary += f"，前几条: {'; '.join(h[:80] for h in head[:3])}"
        if tail and tail not in head:
            summary += f"，末条: {tail[:80]}"
        compressed = f"[已整理 原文{len(text)}字/共{n}行 ↕] {summary}"
        return compressed, text

    first_part = text[:200].replace("\n", " ").strip()
    rest_len = len(text) - 200
    compressed = (
        f"[已整理 原文{len(text)}字 ↕] {first_part}…"
        f"\n（以下省略{rest_len}字，可 expand_compressed 展开查看完整内容）"
    )
    return compressed, text


def _store_compressed_original(topic_id: str, msg_idx: str, original: str, store):
    """Store compressed original in memory fragment + in-memory cache.

    The anchor is "{topic_id}:{msg_idx}" — embedded in the [已整理] marker.
    """
    anchor = f"{topic_id}:{msg_idx}"
    # In-memory cache for fast access
    with _compressed_originals_lock:
        if topic_id not in _compressed_originals_cache:
            _compressed_originals_cache[topic_id] = {}
        _compressed_originals_cache[topic_id][msg_idx] = original
    # Persist to memory fragments
    if store:
        try:
            store.add(
                original,
                ts=time.strftime("%Y%m%d%H%M%S"),
                source="compressed_original",
                topic_id=topic_id,
                tags=f"compressed:{anchor}",
                importance=1.0,  # low importance — archival only
            )
        except Exception as e:
            print(f"[compressed_original] 原文落盘失败 topic={topic_id} anchor={msg_idx}: {e}")


def _load_compressed_originals(topic_id: str, store) -> dict:
    """Load compressed originals from memory fragments into cache.

    Uses direct SQL tag lookup instead of embedding recall —
    we know the exact tag prefix, no need for semantic search.
    """
    with _compressed_originals_lock:
        if topic_id in _compressed_originals_cache:
            return _compressed_originals_cache[topic_id]
        _compressed_originals_cache[topic_id] = {}

    if not store:
        return _compressed_originals_cache.get(topic_id, {})

    try:
        cache = _compressed_originals_cache[topic_id]
        conn = store._conn()
        try:
            rows = conn.execute(
                "SELECT text, tags FROM memory_fragments "
                "WHERE dirty=1 AND tags LIKE ?",
                (f"%compressed:{topic_id}:%",)
            ).fetchall()
            for row in rows:
                tags = (row["tags"] or "").split(",")
                for tag in tags:
                    tag = tag.strip()
                    if tag.startswith(f"compressed:{topic_id}:") and row["text"]:
                        msg_idx = tag.split(":")[-1]
                        cache[msg_idx] = row["text"]
        finally:
            conn.close()
    except Exception:
        pass
    return cache


# ── Sync and mutual exclusion for compressed originals ──

def get_compressed_original(topic_id: str, msg_idx: str) -> str | None:
    """Public accessor for expand_compressed tool."""
    with _compressed_originals_lock:
        cache = _compressed_originals_cache.get(topic_id, {})
        orig = cache.get(msg_idx)
        if orig:
            return orig
    # Try loading from memory store
    try:
        from memory.fragment_store import get_store
        store = get_store()
        if store:
            _load_compressed_originals(topic_id, store)
            with _compressed_originals_lock:
                return _compressed_originals_cache.get(topic_id, {}).get(msg_idx)
    except Exception:
        pass
    return None


def _attach_compressed_originals(topic_id: str, msgs: list):
    """For each compressed message, attach original_text so frontend can show real content.

    Only modifies the API response (msgs list in-place).
    The AI context (in-memory messages) continues to use compressed summaries.
    """
    import re
    # One-time setup: load compressed originals + build non-tool position map for 📋 recovery
    _clipboard_setup = None  # lazy init

    for msg in msgs:
        text = msg.get("text", "")
        if not text:
            continue
        # Per-message / batch compression: look for expand_compressed("tid", "N") link
        m = re.search(r'expand_compressed\("([^"]+)",\s*"([^"]+)"\)', text)
        if m:
            anchor = m.group(2)
            original = get_compressed_original(topic_id, anchor)
            if original:
                msg["original_text"] = original
        # Legacy: bare 📋 markers (pre-fix messages) — recover from memory fragments
        elif text.strip() == "📋":
            if _clipboard_setup is None:
                _clipboard_setup = _build_clipboard_recovery(topic_id)
            original = _clipboard_setup.get(msg.get("id"))
            if original:
                msg["original_text"] = original


def _build_clipboard_recovery(topic_id: str) -> dict:
    """Build db_id → original_text map for legacy 📋 messages.

    Queries non-tool messages in chronological order, builds position→db_id map,
    then matches against compressed originals stored by anchor (position+1).
    """
    try:
        from db import get_db
        db = get_db()
        raw = db.get_messages(topic_id, limit=None)
    except Exception:
        return {}

    # Build position→db_id for non-tool messages (same indexing as organize_context)
    pos_to_db_id = {}
    idx = 0
    for m in raw:
        if m.get("role") == "tool":
            continue
        pos_to_db_id[idx] = m.get("id")
        idx += 1

    if not pos_to_db_id:
        return {}

    # Load compressed originals from memory fragments
    try:
        from memory.fragment_store import get_store
        store = get_store()
        if store:
            _load_compressed_originals(topic_id, store)
    except Exception:
        pass

    with _compressed_originals_lock:
        cache = _compressed_originals_cache.get(topic_id, {})

    if not cache:
        return {}

    # Map db_id → original_text via position anchor
    result = {}
    for anchor_str, original in cache.items():
        try:
            pos = int(anchor_str) - 1  # anchor is 1-indexed position
        except ValueError:
            continue
        db_id = pos_to_db_id.get(pos)
        if db_id:
            result[db_id] = original

    return result

# ── Context compression ─────────────────────────────


def _estimate_tokens(messages):
    """Estimate token count including overhead (tools, formatting ~1.5x multiplier).
    CJK chars ≈ 1.2 tokens/char, ASCII ≈ 0.3 tokens/char.
    图片消息（content 为数组）的 image 块不按 base64 字符数估算（否则撑爆预算），
    按固定值 _EST_IMAGE_TOKENS 计。"""
    def _count_text(s):
        c = 0.0
        for ch in s:
            if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef':
                c += 1.2
            else:
                c += 0.3
        return c

    total = 0.0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            # 多模态消息：text 块正常计，image 块按估算值，其余字段（role 等）按 JSON 计
            for block in content:
                if isinstance(block, dict):
                    t = block.get("type")
                    if t == "text":
                        total += _count_text(block.get("text", ""))
                    elif t == "image_url":
                        total += _EST_IMAGE_TOKENS
            rest = {k: v for k, v in m.items() if k != "content"}
            if rest:
                total += _count_text(json.dumps(rest, ensure_ascii=False))
        else:
            total += _count_text(json.dumps(m, ensure_ascii=False))
    # ×1.5 for unseen overhead: tool definitions, API formatting, special tokens
    return int(total * 1.5)


# 单张图片的 token 估算值：DeepSeek vision 按尺寸算（约 85 + 每 512² tile×170），
# 普通照片在几百到两千 token；固定 1000 足够避免误触发上下文截断。
_EST_IMAGE_TOKENS = 1000


def _estimate_tools_tokens(tool_defs):
    """估算工具 schema 占用的 token 数。

    使用与 _build_tools 一致的压缩后再计算，避免高估（压缩后实际传输的 schema 更小）。
    公式与历史快照一致：JSON 字节数 × 0.6。
    """
    if not tool_defs:
        return 0
    try:
        from llm import _build_tools
        built = _build_tools(tool_defs)
        if not built:
            return 0
        return int(len(json.dumps(built, ensure_ascii=False)) * 0.6)
    except Exception:
        return int(len(json.dumps(tool_defs, ensure_ascii=False)) * 0.6)


def _lookup_first_user_msg(topic_id: str) -> str:
    """Look up the first user message for a topic. Used for context boundary warnings."""
    try:
        from db import get_db
        db = get_db()
        rows = db._fetchall(
            "SELECT text FROM messages WHERE topic_id=? AND role='user' AND text != '' "
            "ORDER BY ts ASC, id ASC LIMIT 1",
            (topic_id,)
        )
        if rows:
            return rows[0]["text"][:200]
    except Exception:
        pass
    return ""

def _strip_orphaned_tool_messages(messages):
    """Remove tool messages whose parent assistant tool_calls is missing upstream.

    The OpenAI/DeepSeek API requires every role="tool" message to be preceded
    by an assistant message with tool_calls containing the matching id.
    Orphans arise from context truncation cutting an assistant-tool boundary,
    or from stripping orphaned tool_calls while leaving tool results behind.
    """
    if not messages:
        return messages
    seen_tc_ids = set()
    result = []
    stripped = 0
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seen_tc_ids.add(tc.get("id", ""))
        if m.get("role") == "tool":
            if m.get("tool_call_id") not in seen_tc_ids:
                stripped += 1
                continue
        result.append(m)
    if stripped:
        print(f"[agent] stripped {stripped} orphaned tool message(s)")
    return result

_CTX_SECTION_MARKER = "━━━ 上下文状态 ━━━"

def _apply_context_wall(system_msg, history_msgs, truncated_count, store=None, topic_id=None):
    """Inject context wall into the unified 上下文状态 section.

    与 _inject_context_hints 共用同一个 section marker，避免尾部出现多个游离 section。
    防堆叠：先剥离旧 section 再重写。
    """
    visible = len(history_msgs)
    _SECTION_SEP = "\n\n" + "═" * 15 + "\n\n"
    wall_lines = [
        f"⚠ 上下文受限:",
        f"  可见: {visible} 条消息",
        f"  已整理: 前 {truncated_count} 条（不在上下文中）",
        f"  翻阅: read_topic_messages(order='asc') → 从最早开始读",
    ]
    # 防堆叠：剥离旧的上下文状态 section（连同前面的分隔线）
    content = system_msg["content"]
    idx = content.find(_CTX_SECTION_MARKER)
    if idx != -1:
        _sep_marker = "═" * 15
        sep_idx = content.rfind(_sep_marker, 0, idx)
        if sep_idx != -1 and sep_idx < idx:
            content = content[:sep_idx].rstrip()
        else:
            content = content[:idx].rstrip()
    system_msg["content"] = content + _SECTION_SEP + _CTX_SECTION_MARKER + "\n" + "\n".join(wall_lines)
    if store and truncated_count > 0:
        try:
            ts_str = time.strftime("%Y%m%d%H%M%S")
            store.add(
                f"上下文截断：对话早期 {truncated_count} 条消息因超预算被截断。用 crystal_recall 搜索回溯。",
                ts=ts_str, source="context_wall", topic_id=topic_id,
            )
        except Exception:
            pass

def _trim_context_to_budget(messages, token_limit, store=None, topic_id=None):
    """Trim oldest complete dialogues from front until under budget.

    A dialogue = one user message + all following assistant/tool messages
    until the next user message (or end). Never trims mid-dialogue.

    Returns (trimmed_messages, truncated_count).
    """
    truncated_count = 0

    system_msg = messages[0]
    history_msgs = list(messages[1:])

    # Calculate current size
    if _estimate_tokens([system_msg] + history_msgs) < token_limit:
        return messages, 0

    # Find dialogue start indices (user message boundaries)
    user_indices = [i for i, m in enumerate(history_msgs) if m.get("role") == "user"]

    if len(user_indices) <= 1:
        # Single dialogue — fall back to trimming by assistant-exchange boundaries.
        assistant_indices = [i for i, m in enumerate(history_msgs) if m.get("role") == "assistant"]
        if len(assistant_indices) <= 1:
            return messages, 0

        for cut_idx in assistant_indices[:-1]:
            candidate_msgs = [system_msg, history_msgs[0]] + history_msgs[cut_idx:]
            if _estimate_tokens(candidate_msgs) < token_limit:
                truncated_count = cut_idx - 1
                history_msgs = [history_msgs[0]] + history_msgs[cut_idx:]
                history_msgs = _strip_orphaned_tool_messages(history_msgs)
                _apply_context_wall(system_msg, history_msgs, truncated_count, store, topic_id)
                return [system_msg] + history_msgs, truncated_count

        # Still over budget — keep only the last assistant exchange + user
        cut_idx = assistant_indices[-1]
        truncated_count = cut_idx - 1
        history_msgs = [history_msgs[0]] + history_msgs[cut_idx:]
        _apply_context_wall(system_msg, history_msgs, truncated_count, store, topic_id)
        return [system_msg] + history_msgs, truncated_count

    # ── Multi-dialogue path: trim oldest complete dialogues from front ──
    # user_indices > 1: we have multiple user messages → multiple dialogues
    for i, user_idx in enumerate(user_indices[:-1]):
        # Each dialogue = user message at user_idx + all messages until next user message
        next_user_idx = user_indices[i + 1]
        candidate = [system_msg] + history_msgs[user_idx:]
        if _estimate_tokens(candidate) < token_limit:
            truncated_count = user_idx
            history_msgs = history_msgs[user_idx:]
            history_msgs = _strip_orphaned_tool_messages(history_msgs)
            _apply_context_wall(system_msg, history_msgs, truncated_count, store, topic_id)
            return [system_msg] + history_msgs, truncated_count

    # Still over budget after all dialogues tried — keep only last dialogue
    cut_idx = user_indices[-1]
    truncated_count = cut_idx
    history_msgs = history_msgs[cut_idx:]
    _apply_context_wall(system_msg, history_msgs, truncated_count, store, topic_id)
    return [system_msg] + history_msgs, truncated_count

def _compress_context(messages, topic_id, store, db=None):
    """Phase 1: 滑动窗口 --- 按轮次截断最旧对话。

    预算超限或消息数超墙时，从前往后按对话边界截断，注入上下文墙提示。
    AI 可通过 read_topic_messages 翻阅被截断的消息。

    注意：调用方应确保 messages 不包含当前用户消息，只含 system + 旧历史。
    """
    if not messages:
        return messages

    system_msg = messages[0]
    history_msgs = list(messages[1:])
    if not history_msgs:
        return messages

    msgs_with_system = [system_msg] + history_msgs
    estimated_tokens = _estimate_tokens(msgs_with_system)
    eff_limit = _effective_limit()

    token_over = estimated_tokens >= eff_limit
    msg_over = MSG_COUNT_WALL > 0 and len(history_msgs) > MSG_COUNT_WALL

    if token_over or msg_over:
        trigger_reason = "token" if token_over else f"消息条数({len(history_msgs)} > {MSG_COUNT_WALL})"
        print(f"[context-wall] Phase1 滑动窗口触发: {trigger_reason}, "
              f"{estimated_tokens} tokens, {len(history_msgs)}条历史消息, 开始按轮次截断")

        trim_target = eff_limit
        if msg_over and not token_over:
            system_tokens = _estimate_tokens([system_msg])
            history_tokens = estimated_tokens - system_tokens
            avg_tok_per_msg = history_tokens / max(len(history_msgs), 1)
            trim_target = int(system_tokens + avg_tok_per_msg * MSG_COUNT_WALL * 0.9)
            trim_target = max(trim_target, system_tokens + 500)

        trimmed_msgs, trimmed_count = _trim_context_to_budget(
            msgs_with_system, trim_target, store=store, topic_id=topic_id
        )
        system_msg = trimmed_msgs[0]
        history_msgs = trimmed_msgs[1:]
        if trimmed_count > 0:
            print(f"[context-wall] 截断 {trimmed_count} 条旧消息, 剩余 {len(history_msgs)} 条, "
                  f"~{_estimate_tokens([system_msg] + history_msgs)} tokens")
    else:
        print(f"[context-wall] Phase1未触发: {estimated_tokens} tokens < {eff_limit}, "
              f"{len(history_msgs)}条消息" + (f" (msg_wall={MSG_COUNT_WALL})" if MSG_COUNT_WALL else ""))

    return [system_msg] + history_msgs


def _is_hanging_announcement(text):
    """检测'预告悬空'：以未完句结尾的动作声明（如'现在写XXX：'），或缺失终结标点的动作句式。"""
    if not text or not text.strip():
        return False
    t = text.rstrip()
    # 1) 以冒号/逗号/分号/破折号/省略号等未完句结尾 → 大概率话没说完
    if re.search(r'[：:，,；;、—–\-…]$', t):
        return True
    # 2) 含动作声明句式（现在写/接下来/下一步…）且不以终结标点收尾 → 预告悬空
    if re.search(r'(现在写|现在做|接下来|下一步|马上|这就去|开始写|开始做|先写|继续写)', t) \
            and not t.endswith(('。', '.', '！', '？', '!', '?')):
        return True
    return False


def _inject_context_hints(msgs, topic_id, limit_truncated=False):
    """Inject actionable context management hints into the system message.

    Runs after _compress_context. Detects:
    1) Any [已整理]/[已压缩] markers in history → tell AI how to expand them
    2) Context approaching budget → tell AI how to proactively organize
    3) limit_truncated → tell AI how to browse older messages

    Hints are self-contained — AI reads them and knows the exact call to make
    without looking up tool definitions.
    """
    if not msgs:
        return

    history_msgs = msgs[1:]
    if not history_msgs:
        return

    # ── Detect compressed content ──
    has_compressed = any(
        isinstance(m.get("content", ""), str) and
        ("[已整理" in m.get("content", "") or "[已压缩" in m.get("content", "") or "📋" in m.get("content", "") or "[上下文压缩" in m.get("content", ""))
        for m in history_msgs
    )

    # ── Estimate context size ──
    estimated_tokens = _estimate_tokens(msgs)
    # warn-pct threshold — warn before the hard 100% cutoff
    context_near_limit = estimated_tokens > TOKEN_LIMIT * (COMPRESS_WARN_PCT / 100)
    # ── Count non-tool messages for anchor awareness ──
    total_non_tool = sum(1 for m in history_msgs if m.get("role") != "tool")
    uncompressed_count = sum(
        1 for m in history_msgs
        if isinstance(m.get("content", ""), str)
        and "[已整理" not in m.get("content", "")
        and "[已压缩" not in m.get("content", "")
        and m.get("role") != "tool"
    )
    context_crowded = uncompressed_count > MSG_COUNT_WARN
    # ── Detect compression tree nodes + 墙后整理消息数 ──
    has_ct = False
    hidden_count = 0
    try:
        from db import get_db
        db_ct = get_db()
        try:
            db_ct.conn.execute("SELECT id FROM compression_tree LIMIT 0")
            ct_rows = db_ct._fetchall(
                "SELECT id FROM compression_tree WHERE topic_id = ? LIMIT 1",
                (topic_id,)
            )
            has_ct = len(ct_rows) > 0
        except Exception:
            has_ct = False
        try:
            row = db_ct._fetchone(
                "SELECT COUNT(*) as cnt FROM messages "
                "WHERE topic_id = ? AND args LIKE '%\"hidden\"%'",
                (topic_id,)
            )
            hidden_count = row["cnt"] if row else 0
        except Exception:
            hidden_count = 0
    except Exception:
        pass

    hints = []

    # ── 仅在有整理消息时显示统计（无整理时 AI 能看到全部消息，统计行是冗余）──
    if hidden_count > 0:
        hints.append(
            f"📊 {total_non_tool} 条消息在上下文中，{uncompressed_count} 条未整理；"
            f"另有 {hidden_count} 条已整理进节点（不在上下文中，"
            f"用 expand_compressed 展开或 read_topic_messages 翻阅）。"
        )

    # ── Nested compression tree navigation ──
    if has_ct:
        try:
            from tools.memory_tools import _get_topic_tree_summary
            tree_text = _get_topic_tree_summary(db_ct, topic_id, max_depth=2)
            if tree_text:
                hints.append(tree_text)
        except Exception:
            pass

    if has_compressed:
        hints.append("📋 上下文中有已整理标记 — 用 expand_compressed 展开查看原文。")
    if context_near_limit or context_crowded:
        hints.append("⚠ 上下文拥挤 — 用 organize_context 整理旧消息腾空间。")
    if limit_truncated:
        hints.append(
            f"📖 更早的消息不在上下文中。翻阅: read_topic_messages(order='asc')。"
            f"看完记得 organize_context。"
        )
    if hints:
        content = msgs[0]["content"]
        idx = content.find(_CTX_SECTION_MARKER)
        if idx != -1:
            # _apply_context_wall 已创建 section，追加到内部（用 ─ 细线分隔）
            _BLOCK_SEP = "\n\n" + "─" * 15 + "\n\n"
            msgs[0]["content"] = content + _BLOCK_SEP + "\n".join(hints)
        else:
            # 无 wall，单独创建 section
            _SECTION_SEP = "\n\n" + "═" * 15 + "\n\n"
            msgs[0]["content"] = content + _SECTION_SEP + _CTX_SECTION_MARKER + "\n" + "\n".join(hints)


# ── Topic Manager ───────────────────────────────────
class TopicManager:
    def __init__(self, db=None):
        self.db = db or get_db()

    def list_topics(self):
        return self.db.list_topics()

    def create_topic(self, title=None, session_path=""):
        tid = uuid.uuid4().hex[:12]
        ts = time.time()
        self.db.create_topic(title or "新任务", session_path, "", topic_id=tid)
        print(f"[topic] created {tid[:8]} — {title or '新任务'}")
        return {"id": tid, "title": title or "新任务",
                "session_path": session_path,
                "created_at": ts, "updated_at": ts,
                "total_cost": 0, "last_message": ""}

    def delete_topic(self, tid):
        self.db.delete_topic(tid)

    def rename_topic(self, tid, title):
        self.db.rename_topic(tid, title)

    def get_topic(self, tid):
        return self.db.get_topic(tid)

    def get_or_create(self, tid):
        topic = self.db.get_topic(tid)
        if topic:
            return topic
        return self.create_topic()


# ── Bootstrap: seed models/config from _internal to ROOT_DIR ──
def _bootstrap_data():
    if not getattr(sys, 'frozen', False):
        return
    src = os.path.join(BASE_DIR, "model_config.json")
    dst = os.path.join(ROOT_DIR, "model_config.json")
    if os.path.exists(src) and not os.path.exists(dst):
        try:
            import shutil
            shutil.copy2(src, dst)
            print(f"[bootstrap] Seeded model_config.json to {dst}")
        except Exception as e:
            print(f"[bootstrap] Failed: {e}")
    # Seed public/ if not present
    src_pub = os.path.join(BASE_DIR, "public")
    dst_pub = os.path.join(ROOT_DIR, "public")
    if os.path.isdir(src_pub) and not os.path.isdir(dst_pub):
        try:
            import shutil
            shutil.copytree(src_pub, dst_pub)
            print(f"[bootstrap] Seeded public/ to {dst_pub}")
        except Exception as e:
            print(f"[bootstrap] public/ failed: {e}")
    # Seed skills/ — user-created skills go to writable location
    src_skills = os.path.join(BASE_DIR, "skills")
    dst_skills = os.path.join(ROOT_DIR, "skills")
    if os.path.isdir(src_skills):
        try:
            import shutil
            # Only copy bundled skills on first run (don't overwrite user-created ones)
            if not os.path.isdir(dst_skills):
                shutil.copytree(src_skills, dst_skills)
                print(f"[bootstrap] Seeded skills/ to {dst_skills}")
            else:
                # Copy any new bundled templates
                tmpl_src = os.path.join(src_skills, "_templates")
                tmpl_dst = os.path.join(dst_skills, "_templates")
                if os.path.isdir(tmpl_src) and not os.path.isdir(tmpl_dst):
                    shutil.copytree(tmpl_src, tmpl_dst)
                    print(f"[bootstrap] Seeded skills/_templates/ to {tmpl_dst}")
        except Exception as e:
            print(f"[bootstrap] skills/ failed: {e}")
    # Seed data/ — static config files only (don't overwrite user databases)
    src_data = os.path.join(BASE_DIR, "data")
    dst_data = os.path.join(ROOT_DIR, "data")
    if os.path.isdir(src_data):
        try:
            import shutil
            os.makedirs(dst_data, exist_ok=True)
            # Only copy static config files, never overwrite existing DBs or missions
            _static_files = ["domain_book.json", "reflection_prompt.txt", "conversations.db", "chat.db"]
            for fn in _static_files:
                sf = os.path.join(src_data, fn)
                df = os.path.join(dst_data, fn)
                if os.path.isfile(sf) and not os.path.isfile(df):
                    shutil.copy2(sf, df)
                    print(f"[bootstrap] Seeded data/{fn} to {df}")
            # Seed skills/ and trash/ directories if absent
            for sub in ["skills", "trash"]:
                sd = os.path.join(src_data, sub)
                dd = os.path.join(dst_data, sub)
                if os.path.isdir(sd) and not os.path.isdir(dd):
                    shutil.copytree(sd, dd)
                    print(f"[bootstrap] Seeded data/{sub}/ to {dd}")
        except Exception as e:
            print(f"[bootstrap] data/ failed: {e}")
# ── Config ──────────────────────────────────────────
_model_config = None
_config_lock = threading.Lock()


def get_model_config():
    global _model_config
    if _model_config is None:
        with _config_lock:
            if _model_config is None:
                _model_config = LLMConfig.from_config(
                    os.path.join(ROOT_DIR, "model_config.json"))
    return _model_config


def _save_model_config():
    """Persist current _model_config to model_config.json."""
    path = os.path.join(ROOT_DIR, "model_config.json")
    try:
        cfg = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        if _model_config:
            cfg["provider"] = _model_config.provider
            cfg["model"] = _model_config.model
            cfg["base_url"] = _model_config.base_url
            cfg["api_key"] = _model_config.api_key
            cfg["max_tokens"] = _model_config.max_tokens
            # 保存 fallback_chain（含每个 provider 的 api_key），否则前端输入的 key 不持久化
            cfg["fallback_chain"] = _model_config.fallback_chain
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[config] Failed to save model_config.json: {e}")

# ── Dynamic model list ────────────────────────────

# Cached model lists per provider: {provider: (timestamp, [models])}
_model_list_cache = {}
_MODEL_CACHE_TTL = 3600  # 1 hour

_MODEL_FRIENDLY = {
    "deepseek-chat": ("DeepSeek V4 Flash (兼容)", "v4-flash 非思考模式兼容别名"),
    "deepseek-reasoner": ("DeepSeek V4 Flash 思考", "v4-flash 思考模式兼容别名"),
    "deepseek-coder": ("DeepSeek Coder", "代码生成专用"),
    "deepseek-v4-flash": ("DeepSeek V4 Flash", "极速响应，1M 上下文，默认推荐"),
    "deepseek-v4-pro": ("DeepSeek V4 Pro", "旗舰模型，最强综合能力"),
    # Ollama local models
    "Ornith:latest": ("Ornith", "本地推理模型，带思维链"),
    "Ornith-opt:latest": ("Ornith-opt", "本地推理模型，优化版"),
    "minicpm5-1b:latest": ("MiniCPM5 1B", "本地轻量模型，速度快"),
}


def _fetch_provider_models(provider: str, base_url: str, api_key: str) -> list[dict]:
    """Fetch model list from provider's /v1/models endpoint. Cached 1h."""
    now = time.time()
    cached = _model_list_cache.get(provider)
    if cached and (now - cached[0]) < _MODEL_CACHE_TTL:
        return cached[1]

    try:
        # base_url may already include "/v1" (e.g. Ollama http://localhost:11434/v1)
        # — don't double-append. DeepSeek/Zhipu pass a bare root URL.
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            url = root + "/models"
        else:
            url = root + "/v1/models"
        req = urllib.request.Request(url)
        # Ollama doesn't require an API key; only send header if non-empty
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=10, context=None if _VERIFY_SSL else _UNVERIFIED_SSL_CTX) as resp:
            data = json.loads(resp.read())
        raw_models = data.get("data", [])
        models = []
        for m in raw_models:
            mid = m.get("id", "")
            owned_by = m.get("owned_by", provider)
            # Only include models actually owned by this provider
            if owned_by != provider and owned_by != "deepseek" and owned_by != "deepseek-chat":
                # Some APIs return models from other providers — skip those
                if provider == "deepseek" and "deepseek" not in mid.lower():
                    continue
            friendly, desc = _MODEL_FRIENDLY.get(mid, (mid, ""))
            models.append({
                "id": mid,
                "name": friendly,
                "desc": desc,
                "owned_by": owned_by,
            })
        _model_list_cache[provider] = (now, models)
        return models
    except Exception as e:
        print(f"[models] Failed to fetch from {provider}: {e}")
        return []


def _get_provider_key(cfg, provider: str) -> str:
    """获取指定 provider 的 api_key：主 config 优先，否则查 fallback_chain。"""
    if cfg.provider == provider:
        return cfg.api_key
    for item in cfg.fallback_chain:
        if item.get("provider") == provider:
            return item.get("api_key", "")
    return ""


def _build_model_list(cfg) -> list[dict]:
    """Build frontend model list: merge API-fetched models with hardcoded fallback."""
    result = []

    # DeepSeek: 无论主 provider 是什么，都尝试用 deepseek 的 key 拉模型列表
    ds_key = _get_provider_key(cfg, "deepseek")
    ds_models = []
    if ds_key:
        ds_models = _fetch_provider_models("deepseek", "https://api.deepseek.com", ds_key)
    if not ds_models:
        ds_models = [
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "desc": "极速响应，1M 上下文，默认推荐"},
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "desc": "旗舰模型，最强综合能力"},
            {"id": "deepseek-chat", "name": "DeepSeek V4 Flash (兼容)", "desc": "v4-flash 非思考模式兼容别名"},
        ]
    for m in ds_models:
        result.append({
            "provider": "deepseek",
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "desc": m.get("desc", ""),
            "available": bool(ds_key),
            "current": cfg.provider == "deepseek" and cfg.model == m["id"],
        })

    # Ensure current model is in the list (handles aliases like deepseek-chat → v4-flash)
    if cfg.provider == "deepseek" and not any(m["current"] for m in result):
        friendly, desc = _MODEL_FRIENDLY.get(cfg.model, (cfg.model, ""))
        result.insert(0, {
            "provider": "deepseek",
            "id": cfg.model,
            "name": friendly,
            "desc": desc,
            "available": bool(cfg.api_key),
            "current": True,
        })

    # Ollama: local models — always probe localhost so user can switch even
    # when current provider is something else.
    _OLLAMA_DEFAULT_URL = "http://localhost:11434/v1"
    ollama_models = _fetch_provider_models("ollama", _OLLAMA_DEFAULT_URL, "")
    for m in ollama_models:
        result.append({
            "provider": "ollama",
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "desc": m.get("desc", "本地模型"),
            "available": True,
            "current": cfg.provider == "ollama" and cfg.model == m["id"],
        })
    # Ensure current ollama model is listed even if /v1/models missed it
    if cfg.provider == "ollama" and not any(m["provider"] == "ollama" and m["current"] for m in result):
        friendly, desc = _MODEL_FRIENDLY.get(cfg.model, (cfg.model, "本地模型"))
        result.append({
            "provider": "ollama",
            "id": cfg.model,
            "name": friendly,
            "desc": desc,
            "available": True,
            "current": True,
        })

    # Zhipu: hardcoded
    zhipu_key = _get_provider_key(cfg, "zhipu")
    for mid, name, desc in [
        ("glm-5.3-flash", "GLM-5.3 Flash", "原生多模态,1M上下文,视觉编码"),
        ("glm-5-turbo", "GLM-5 Turbo", "旗舰模型"),
        ("glm-4-flash", "GLM-4 Flash", "免费快速模型"),
    ]:
        result.append({
            "provider": "zhipu",
            "id": mid, "name": name, "desc": desc,
            "available": bool(zhipu_key),
            "current": cfg.provider == "zhipu" and cfg.model == mid,
        })

    # OpenRouter: hardcoded
    result.append({
        "provider": "openrouter",
        "id": "openrouter/auto", "name": "Auto (最优)", "desc": "自动路由",
        "available": False,
        "current": cfg.provider == "openrouter" and cfg.model == "openrouter/auto",
    })

    return result
def get_ips(port):
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, port):
            addr = info[4][0]
            if addr and not addr.startswith("127.") and ':' not in addr:
                ips.append(f"http://{addr}:{port}")
    except Exception:
        pass
    ips.append(f"http://127.0.0.1:{port}")
    return ips


# ── Working state ────────────────────────────────────
_current_working = {}
_working_lock = threading.Lock()
# Paused chat loops awaiting user decision to continue/stop
_paused_loops = {}
_paused_lock = threading.Lock()

# Cancel events for active chat loops — allows stop-thinking to interrupt streaming
_cancel_events = {}
_cancel_lock = threading.Lock()

# Injection queues for active chat loops — 循环运行中收到的新用户消息进队列，
# agent 循环在轮次边界取出注入上下文继续思考，避免起第二个并行循环
_inject_queues = {}
_inject_lock = threading.Lock()


def _drain_injections(tid):
    """取出并清空该话题的插话队列；无则返回空列表。"""
    with _inject_lock:
        q = _inject_queues.get(tid)
        if not q:
            return []
        items = list(q)
        q.clear()
        return items


def set_working(tid, status, thinking="", intermediate="", response="", tool_calls=None, turn=0, remaining=None):
    with _working_lock:
        _current_working[tid] = {
            "status": status,
            "thinking": thinking or "",
            "intermediate": intermediate or "",
            "response": response or "",
            "tool_calls": tool_calls or [],
            "turn": turn,
            "remaining": remaining,
        }


def get_working(tid):
    with _working_lock:
        w = _current_working.get(tid)
        if w:
            return w
    return {"status": "idle", "thinking": "", "intermediate": "", "response": "", "tool_calls": [], "turn": 0, "remaining": None}

def clear_working(tid):
    with _working_lock:
        _current_working.pop(tid, None)




# ── Cost tracking ────────────────────────────────────
# DeepSeek 官方定价（CNY / 1M tokens）。缓存命中价约为未命中的 1/50~1/100，
# 不区分缓存会高估几十倍（历史 bug 根源）。
# 2026-08-17 00:00 北京时间起启用峰谷定价（高峰 9-12、14-18 点）。
_PRICING_OLD = {  # 2026-08-17 之前
    "flash": {"hit": 0.02, "miss": 1.0, "out": 2.0},
    "pro":   {"hit": 0.025, "miss": 3.0, "out": 6.0},
}
_PRICING_NEW = {  # 2026-08-17 起，分闲时/高峰
    "flash": {"off": {"hit": 0.05, "miss": 1.5, "out": 4.5}, "peak": {"hit": 0.10, "miss": 3.0, "out": 9.0}},
    "pro":   {"off": {"hit": 0.15, "miss": 4.5, "out": 13.5}, "peak": {"hit": 0.30, "miss": 9.0, "out": 27.0}},
}
_NEW_PRICING_TS = 1786982400  # 2026-08-17 00:00 北京时间

# 智谱定价（USD / 1M tokens，国际站 z.ai 官方价，乘实时汇率折 CNY）。
# 与 DeepSeek 价格体系完全独立，禁止掉进上面 DeepSeek 表（历史 bug：
# glm-5.3-flash 含 "flash" 被当 DeepSeek flash 档，高估数十倍）。
# Flash 为限时 5 折价（原价 0.15/0.03/0.50），恢复原价时改这里。
_ZHIPU_PRICING = {
    "glm-5.3-flash": {"hit": 0.015, "miss": 0.075, "out": 0.25},
    "glm-5.3":       {"hit": 0.26,  "miss": 1.4,   "out": 4.4},
}

_total_cost_cny = 0.0
_total_tokens_global = 0
_cost_lock = threading.Lock()
_cny_rate = 7.0
_rate_updated = 0


def _is_peak_hour():
    h = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).hour
    return (9 <= h < 12) or (14 <= h < 18)


def _cost_cny(input_tokens, output_tokens, cache_hit_tokens=0, model=""):
    """精确估价（CNY）：DeepSeek 用官方人民币价，智谱用 USD 价×实时汇率。
    缓存命中/未命中分开计价；DeepSeek 8-17 后区分峰谷。"""
    m = (model or "").lower()
    hit = min(cache_hit_tokens or 0, input_tokens)
    miss = max(0, input_tokens - hit)
    if m in _ZHIPU_PRICING:
        p = _ZHIPU_PRICING[m]
        usd = (hit * p["hit"] + miss * p["miss"] + output_tokens * p["out"]) / 1_000_000
        return usd * _cny_rate
    if "glm" in m:
        # 智谱新模型未录入价格表：用 Flash 价兜底并提醒，绝不掉进 DeepSeek 表
        print(f"[cost] WARNING: {model} not in _ZHIPU_PRICING, fallback to glm-5.3-flash price (update table!)")
        p = _ZHIPU_PRICING["glm-5.3-flash"]
        usd = (hit * p["hit"] + miss * p["miss"] + output_tokens * p["out"]) / 1_000_000
        return usd * _cny_rate
    # DeepSeek：兼容旧模型名前缀（deepseek-chat→flash 档）
    tier = "pro" if "pro" in m else "flash"
    if time.time() >= _NEW_PRICING_TS:
        p = _PRICING_NEW[tier]["peak" if _is_peak_hour() else "off"]
    else:
        p = _PRICING_OLD[tier]
    return (hit * p["hit"] + miss * p["miss"] + output_tokens * p["out"]) / 1_000_000


def _accumulate_cost(tid, input_tokens, output_tokens, cache_hit=0, model=""):
    """精确估价（CNY）累计到 topic + global。total_cost 字段存 CNY。"""
    global _total_cost_cny, _total_tokens_global
    cny = _cost_cny(input_tokens, output_tokens, cache_hit, model)
    with _cost_lock:
        _total_cost_cny += cny
        _total_tokens_global += (input_tokens + output_tokens)
    try:
        db = get_db()
        db.add_topic_cost(tid, cny, input_tokens + output_tokens)
    except Exception:
        pass
    try:
        db = get_db()
        db.set_meta("total_cost_cny", str(_total_cost_cny))
        db.set_meta("total_tokens", str(_total_tokens_global))
    except Exception:
        pass


def _fetch_cny_rate():
    """Fetch live USD→CNY rate, cached 1h."""
    global _cny_rate, _rate_updated
    now = time.time()
    if now - _rate_updated < 3600:
        return _cny_rate
    try:
        rr = urllib.request.urlopen(
            "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
            timeout=10, context=None if _VERIFY_SSL else _UNVERIFIED_SSL_CTX)
        rate_data = json.loads(rr.read())
        _cny_rate = rate_data.get("usd", {}).get("cny", _cny_rate)
        _rate_updated = now
    except Exception as e:
        print(f"[cost] rate fetch failed: {e}")
    return _cny_rate


# ── Balance cache ────────────────────────────────────
_balance_cache = (0, None)
_balance_cache_lock = threading.Lock()
_total_spent_cny = 0.0
_last_balance_cny = 0.0
_balance_provider = None  # 余额基线所属供应商；切换供应商时重置基线不累计消费


def _balance_now():
    """实时查询当前供应商余额（无缓存），返回 float 或 None（失败时）。"""
    try:
        cfg = get_model_config()
        api_key = cfg.api_key
        if not api_key:
            return None
        if cfg.provider == "zhipu":
            # 智谱：控制台内部接口，API key 直接鉴权（实测 200）
            req = urllib.request.Request(
                "https://open.bigmodel.cn/api/biz/account/query-customer-account-report")
            req.add_header("Authorization", "Bearer " + api_key)
            rr = urllib.request.urlopen(req, timeout=10,
                                        context=None if _VERIFY_SSL else _UNVERIFIED_SSL_CTX)
            data = json.loads(rr.read())
            if data and data.get("success") and isinstance(data.get("data"), dict):
                try:
                    return float(data["data"].get("balance", "0"))
                except (ValueError, TypeError):
                    return None
        else:
            req = urllib.request.Request("https://api.deepseek.com/user/balance")
            req.add_header("Authorization", "Bearer " + api_key)
            rr = urllib.request.urlopen(req, timeout=10,
                                        context=None if _VERIFY_SSL else _UNVERIFIED_SSL_CTX)
            data = json.loads(rr.read())
            if data and data.get("is_available"):
                info = data.get("balance_infos", [{}])[0]
                try:
                    return float(info.get("total_balance", "0"))
                except (ValueError, TypeError):
                    return None
    except Exception as e:
        print(f"[balance] query failed: {e}")
    return None


def _query_balance(force=False):
    """Query current provider balance API, cached 120s."""
    global _total_spent_cny, _last_balance_cny, _balance_cache
    now = time.time()
    with _balance_cache_lock:
        if not force and _balance_cache[0] and now - _balance_cache[0] < 120:
            return _balance_cache[1]
    balance_data = None
    now_bal = _balance_now()
    if now_bal is not None:
        balance_data = {"is_available": True, "balance_infos": [{"total_balance": str(now_bal)}]}
    with _balance_cache_lock:
        _balance_cache = (now, balance_data)
    if balance_data and balance_data.get("is_available"):
        info = balance_data.get("balance_infos", [{}])[0]
        try:
            now_bal = float(info.get("total_balance", "0"))
        except (ValueError, TypeError):
            now_bal = 0.0
        global _balance_provider
        cur_provider = get_model_config().provider
        if _balance_provider != cur_provider:
            # 供应商切换（或首次启动）：两池资金独立，重置基线，不把差额误算成消费
            _balance_provider = cur_provider
            _last_balance_cny = now_bal
        elif _last_balance_cny == 0:
            _last_balance_cny = now_bal
        if now_bal < _last_balance_cny:
            _delta = _last_balance_cny - now_bal
            # 防抖：单次跳变 >¥10 视为 API 异常/充值前基线错误，不累计（历史 679 垃圾数据根源）
            if _delta <= 10:
                _total_spent_cny += _delta
            else:
                print(f"[balance] ignore abnormal drop ¥{_delta:.2f}")
        _last_balance_cny = now_bal
        try:
            db = get_db()
            db.set_meta("total_spent_cny", str(_total_spent_cny))
            db.set_meta("last_balance_cny", str(_last_balance_cny))
        except Exception:
            pass
    return balance_data
# ══════════════════════════════════════════════════════
# HTTP Handler
# ══════════════════════════════════════════════════════
class Handler(http.server.BaseHTTPRequestHandler):
    topics = None
    db = None
    port = PORT

    def log_message(self, format, *args):
        # HTTP 访问日志：常态落盘到 data/http_access.log（轻量），便于排查网络/连接问题
        try:
            os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)
            with open(os.path.join(ROOT_DIR, "data", "http_access.log"), "a", encoding="utf-8") as _f:
                _f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.address_string()} {self.command} {self.path} -> {format % args}\n")
        except Exception:
            pass
        if _DEV_MODE:
            super().log_message(format, *args)

    # ── Helpers ──────────────────────────────────
    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            # 客户端（页面刷新/关闭/网络瞬断）先断开了连接，响应写不进去。
            # chat 场景下回复已存库，不丢数据，静默降级为一条日志。
            print(f"[http] client disconnected before response could be sent ({self.path})")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _read_json_body(self):
        """读取 POST body 并解析为 JSON，失败返回空 dict。"""
        raw = self._read_body().decode("utf-8") if self.headers.get("Content-Length") else ""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _parse_query(self):
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(parsed.query)

    def _sse_send(self, data_dict):
        """Send an SSE event."""
        line = json.dumps(data_dict, ensure_ascii=False)
        self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ── Auth (simplified — no encryption for now) ──
    def _check_auth(self):
        return True  # TODO: add encryption support

    # ── GET ────────────────────────────────────────
    def do_GET(self):
        if not self._check_auth():
            self._json(403, {"error": "unauthorized"})
            return

        path = self.path.split("?")[0]
        params = self._parse_query()

        # ── Health ──
        if path == "/api/health":
            self._json(200, {
                "status": "ok",
                "pid": os.getpid(),
                "bridge_ready": True,
            })

        # ── System Status ──
        elif path == "/api/system_status":
            self._json(200, {
                "status": "ok",
                "pid": os.getpid(),
                "base_dir": ROOT_DIR,
                "root_dir": ROOT_DIR,
                "cwd": os.getcwd(),
                "port": self.port,
            })

        # ── Status ──
        elif path == "/api/status":
            self._json(200, {"status": "ok", "urls": get_ips(self.port)})

        # ── Topics ──
        elif path == "/api/topics":
            try:
                topics = self.topics.list_topics()
                # 任务金额 = 真实总消耗(官方余额差) × 该任务估算占比。
                # 余额扣费有延迟，逐任务差价会归属错乱；按比例分摊既跟真实总额对齐，
                # 又保证各任务之和=侧栏总额，且跟随各任务实际跑的用量。
                # total_cost 已存精确估价 CNY，直接作为任务金额
                for t in (topics or []):
                    t["spent_cny"] = round(t.get("total_cost") or 0, 6)
                active = self.db.get_active_topic_id()
                self._json(200, {"topics": topics or [], "active": active})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Working state ──
        elif path == "/api/working":
            tid = params.get("topic_id", [""])[0]
            w = get_working(tid) if tid else {"status": "idle"}
            # 附带阻塞式提问（ask_user 等待中），前端轮询时弹问题卡片
            if tid and isinstance(w, dict):
                try:
                    from tools.ask_user import get_pending_question
                    pq = get_pending_question(tid)
                    if pq:
                        w["pending_question"] = pq
                except Exception:
                    pass
            # 附加思维导图变更时间戳，让前端轮询时检测 AI 改图
            if tid and isinstance(w, dict):
                try:
                    mm_path = os.path.join(ROOT_DIR, "data", "missions", tid, "mindmap.json")
                    if os.path.isfile(mm_path):
                        with open(mm_path, "r", encoding="utf-8", errors="replace") as f:
                            mm_data = json.load(f)
                        w["mindmap_updated_at"] = mm_data.get("updated_at", 0)
                except Exception:
                    pass
            self._json(200, w)

        # ── Messages ──
        elif path == "/api/messages":
            try:
                tid = params.get("topic_id", [""])[0]
                limit_raw = params.get("limit", [None])[0]
                limit = int(limit_raw) if limit_raw is not None else None
                offset = int(params.get("offset", ["0"])[0])

                msgs = self.db.get_messages(tid, limit=limit, offset=offset, skip_hidden=False, skip_process=True)
                # Attach original_text for compressed messages so frontend can show real content
                if msgs:
                    _attach_compressed_originals(tid, msgs)
                self._json(200, {"messages": msgs or []})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/models":
            cfg = get_model_config()
            models = _build_model_list(cfg)
            self._json(200, {"models": models})
        # ── Models fallback chain ──
        elif path == "/api/models/fallback-chain":
            cfg = get_model_config()
            self._json(200, {"fallback_chain": cfg.fallback_chain,
                             "current": cfg.to_dict()})

        # ── Project config ──
        elif path == "/api/project-config":
            db = get_db()
            cfg = {
                "app_name": db.get_meta("app_name") or "大眼X",
                "version": "1.0.0",
            }
            self._json(200, cfg)

        elif path == "/api/usage":
            global _total_tokens_global, _last_request_tokens
            with _cost_lock:
                tokens = _total_tokens_global
            self._json(200, {"total_tokens": tokens, "last_request_tokens": _last_request_tokens})


        # ── Last request body (for debugging context) ──
        elif path == "/api/last-request":
            tid = params.get("topic_id", [""])[0]
            if tid and tid in _last_request_body:
                self._json(200, {"topic_id": tid, "body": _last_request_body[tid]})
            else:
                self._json(404, {"error": "no cached request for this topic"})
        # ── Cost (replaces /api/omp_cost) ──
        elif path in ("/api/cost", "/api/omp_cost"):
            _fetch_cny_rate()
            with _cost_lock:
                cost_cny = _total_cost_cny
            rate = _cny_rate
            # 校准系数：实际消耗(余额差) / 估算总额，用于把任务金额校准到真实 API 消耗
            calib = round(_total_spent_cny / cost_cny, 4) if cost_cny > 0 and _total_spent_cny > 0 else 0
            self._json(200, {"total_cost_usd": round(cost_cny / rate, 6) if rate else 0,
                             "total_cost_cny": round(cost_cny, 4),
                             "total_spent_cny": round(_total_spent_cny, 4),
                             "calib_factor": calib,
                             "rate": rate})

        # ── Balance ──
        elif path == "/api/balance":
            bd = _query_balance()
            if bd and bd.get("is_available"):
                info = bd.get("balance_infos", [{}])[0]
                self._json(200, {
                    "currency": info.get("currency", "CNY"),
                    "total_balance": info.get("total_balance", "0.00"),
                    "granted_balance": info.get("granted_balance", "0.00"),
                    "topped_up_balance": info.get("topped_up_balance", "0.00"),
                })
            else:
                self._json(200, {"total_balance": "0.00", "currency": "CNY", "error": "unavailable"})

        # ── Banner settings ──
        elif path == "/api/settings/banner":
            db = get_db()
            hidden = (db.get_meta("hide_banner") or "true").lower() != "false"
            self._json(200, {"hide_banner": hidden})

        # ── Balance display settings ──
        elif path == "/api/settings/balance_display":
            db = get_db()
            show = (db.get_meta("show_balance") or "true").lower() != "false"
            self._json(200, {"show_balance": show})

        # ── Dev mode (日志记录开关) ──
        elif path == "/api/settings/devmode":
            db = get_db()
            self._json(200, {"dev_mode": db.get_meta("dev_mode") == "1"})

        # ── LLM 响应本地缓存 ──
        elif path == "/api/settings/llm_cache":
            db = get_db()
            enabled = db.get_meta("llm_cache_enabled") == "1"
            stats = {}
            try:
                import llm_cache
                stats = llm_cache.get_cache().stats()
            except Exception:
                pass
            # 诊断:llm 模块内开关实际值
            llm_mod_enabled = None
            try:
                import llm as _llm_diag2
                llm_mod_enabled = _llm_diag2.is_cache_enabled()
            except Exception as e:
                llm_mod_enabled = f"err: {e}"
            self._json(200, {"enabled": enabled, "stats": stats,
                             "llm_module_enabled": llm_mod_enabled})

        # ── User avatar (跨浏览器持久化) ──
        elif path == "/api/settings/avatar":
            db = get_db()
            self._json(200, {"avatar": db.get_meta("user_avatar") or ""})

        # ── AI 文件修改历史（时间机器展示用）──
        elif path == "/api/file_history":
            db = get_db()
            rows = db._fetchall(
                "SELECT id, file_path, tool, before_hash, after_hash, created_at "
                "FROM file_edit_history ORDER BY id DESC LIMIT 200"
            )
            # 按文件分组（保持最近修改在前的顺序）
            groups = {}
            order = []
            for r in rows:
                p = r["file_path"]
                if p not in groups:
                    groups[p] = []
                    order.append(p)
                groups[p].append({
                    "id": r["id"],
                    "tool": r["tool"],
                    "time": r["created_at"],
                    "before_hash": r["before_hash"],
                    "after_hash": r["after_hash"],
                })
            self._json(200, {"files": [{"path": p, "records": groups[p]} for p in order]})

        # ── Compress settings ──
        elif path == "/api/settings/compress":
            db = get_db()
            config = {
                "per_message_chars": int(db.get_meta("compress_per_msg_chars") or PER_MESSAGE_COMPRESS_CHARS),
                "token_limit": int(db.get_meta("compress_token_limit") or TOKEN_LIMIT),
                "warn_threshold_pct": int(db.get_meta("compress_warn_pct") or 60),
                "turn_budget": int(db.get_meta("compress_turn_budget") or 50),
                "msg_count_warn": int(db.get_meta("compress_msg_count_warn") or MSG_COUNT_WARN),
                "msg_count_wall": int(db.get_meta("compress_msg_count_wall") or MSG_COUNT_WALL),
                "verify_ssl": db.get_meta("verify_ssl") != "0",
                "context_window": _model_context_window(get_model_config().model),
                "model_name": get_model_config().model,
                "hard_tokens": _last_hard_tokens,
                "wm_min": _wm_min_token_limit(),
            }
            self._json(200, config)

        # ── Tools enable/disable list ──
        elif path == "/api/settings/tools":
            from tools.registry import get_tool_defs
            all_defs = get_tool_defs()
            enabled = _get_enabled_tool_names()
            tools_list = []
            for d in all_defs:
                name = d.get("name", "")
                is_enabled = (enabled is None) or (name in enabled)
                tools_list.append({
                    "name": name,
                    "label": d.get("label", name),
                    "description": d.get("description", ""),
                    "enabled": is_enabled,
                })
            self._json(200, {
                "tools": tools_list,
                "total": len(tools_list),
                "enabled_count": sum(1 for t in tools_list if t["enabled"]),
                "all_enabled": enabled is None,
            })

        # ── Perspective settings (用户控制，AI 只读) ──
        elif path == "/api/settings/perspective":
            from tools.perspective import get_perspective
            mode = get_perspective()
            label = {"self_aware": "有自我意识的存在（默认）", "human": "拟人化"}.get(mode, mode)
            self._json(200, {"mode": mode, "label": label})

        # ── Changelog ──
        elif path == "/api/changelog":
            entries = []
            changelog = os.path.join(ROOT_DIR, "CHANGELOG.jsonl")
            if os.path.exists(changelog):
                try:
                    with open(changelog, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                entries.append(json.loads(line))
                except Exception:
                    pass
            self._json(200, {"entries": entries[-50:]})

        # ── Memory fragments ──
        elif path == "/api/memory/fragments":
            try:
                store = _get_memory_store()
                if store and _memory_enabled:
                    limit = int(params.get("limit", ["50"])[0])
                    offset = int(params.get("offset", ["0"])[0])
                    fragments = store.list_all(limit=limit)
                    self._json(200, {"fragments": fragments[offset:]})
                else:
                    self._json(200, {"fragments": []})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Memory stats (v4.0 L3 enhanced) ──
        elif path == "/api/memory/stats":
            try:
                store = _get_memory_store()
                if store and _memory_enabled:
                    stats = store.stats()
                    try:
                        from memory.entity_store import EntityStore
                        es = EntityStore()
                        stats["entities"] = es.stats()
                    except Exception:
                        stats["entities"] = {"active": 0, "inactive": 0}
                    try:
                        from memory.relation_store import RelationStore
                        rs = RelationStore()
                        stats["relations"] = rs.stats()
                    except Exception:
                        stats["relations"] = {"total": 0, "active": 0}
                    try:
                        from memory.summary_tree import SummaryTree
                        st = SummaryTree()
                        stats["summary_tree"] = st.stats()
                    except Exception:
                        stats["summary_tree"] = {"monthly": 0, "quarterly": 0, "yearly": 0}
                    self._json(200, {"count": stats.get("total_fragments", 0), "enabled": True, "stats": stats})
                else:
                    self._json(200, {"count": 0, "enabled": False, "stats": {}})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Memory graph ──
        elif path == "/api/memory/graph":
            try:
                store = _get_memory_store()
                if store and _memory_enabled:
                    limit = int(params.get("limit", ["80"])[0])
                    graph = store.get_graph(limit=limit)
                    self._json(200, graph)
                else:
                    self._json(200, {"nodes": [], "edges": []})
            except Exception as e:
                self._json(200, {"nodes": [], "edges": [], "error": str(e)})

        # ── Sys memory ──
        elif path == "/api/sys-memory":
            # [2026-08-19 修复] 防御：store 不可用或 handler 异常时也必须返回 JSON，
            # 否则 do_GET 无响应直接断连 → 浏览器报 Failed to fetch → 状态面板打不开。
            try:
                store = _get_memory_store()
                if store and _memory_enabled:
                    all_frags = store.list_all(limit=10000)
                    count = len(all_frags)
                    # Stats
                    weights = [f.get("weight", 1.0) for f in all_frags if f.get("weight")]
                    avg_weight = round(sum(weights) / len(weights), 2) if weights else 0
                    # Fragment list (latest 30)
                    latest = store.list_all(limit=30)
                    fragment_list = [{"id": f["id"], "text": f["text"], "ts": f.get("ts",""),
                                      "weight": f.get("weight",1.0), "tags": f.get("tags",""),
                                      "topic_id": f.get("topic_id","")} for f in latest]
                    # Managed skills (AI self-created)
                    managed_skills = []
                    try:
                        from skills_scanner import scan_skills
                        for s in scan_skills():
                            managed_skills.append({"name": s["name"], "description": s["description"]})
                    except Exception:
                        pass
                    # 注意力状态指标（attention_metrics.py 五大指标+综合分，5分钟缓存）
                    attention = {}
                    try:
                        import attention_metrics
                        attention = attention_metrics.compute_all()
                        # 统计与实时分开并存：M1 保持历史统计（api_logs 最近一次，5分钟缓存），
                        # 实时大轮峰值作为独立字段 live_peak（内存变量，零查询），互不覆盖
                        if _round_peak_tokens > 0:
                            attention['live_peak'] = attention_metrics.m1_from_peak(_round_peak_tokens)
                            attention['peak_live'] = True
                    except Exception as _att_err:
                        attention = {"error": str(_att_err)}
                    self._json(200, {
                        "topics": len(self.topics.list_topics()),
                        "fragments": {"total": count, "avg_weight": avg_weight},
                        "fragment_list": fragment_list,
                        "managed_skills": managed_skills,
                        "attention": attention,
                    })
                else:
                    self._json(200, {
                        "topics": len(self.topics.list_topics()) if self.topics else 0,
                        "fragments": {"total": 0, "avg_weight": 0},
                        "fragment_list": [],
                        "managed_skills": [],
                        "attention": {},
                        "warning": "memory store unavailable",
                    })
            except Exception as _mem_err:
                self._json(500, {"error": str(_mem_err), "warning": "sys-memory handler failed"})


        # ── 轻量注意力指标（顶栏工作记忆显示用，5分钟缓存）──
        elif path == "/api/attention":
            attention = {}
            try:
                import attention_metrics
                attention = attention_metrics.compute_all()
                if _round_peak_tokens > 0:
                    attention['live_peak'] = attention_metrics.m1_from_peak(_round_peak_tokens)
                    attention['peak_live'] = True
            except Exception as _att_err:
                attention = {"error": str(_att_err)}
            self._json(200, {"attention": attention})

        # ── Skill content ──
        elif path == "/api/skill-content":
            name = params.get("name", [""])[0]
            # Look in writable skills dir (ROOT_DIR in dev, exe directory in frozen)
            from skills_scanner import SKILLS_DIR
            skill_path = os.path.join(SKILLS_DIR, f"{name}.md")
            if os.path.isfile(skill_path):
                try:
                    with open(skill_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    self._json(200, {"name": name, "content": content})
                except Exception:
                    self._json(500, {"error": "读取技能文件失败"})
            else:
                self._json(404, {"error": f"技能不存在: {name}"})


        # ── Tasks (v4.0 task execution layer) ──

        # ── Memory: entities (GET) ──
        elif path == "/api/memory/entities":
            try:
                from memory.entity_store import EntityStore
                es = EntityStore()
                status = params.get("status", ["active"])[0]
                limit = int(params.get("limit", ["100"])[0])
                entities = es.list_all(status=status, limit=limit)
                self._json(200, {"entities": entities, "stats": es.stats()})
            except Exception as e:
                self._json(200, {"entities": [], "stats": {}, "error": str(e)})

        # ── Memory: relations (GET) ──
        elif path == "/api/memory/relations":
            try:
                from memory.relation_store import RelationStore
                rs = RelationStore()
                status = params.get("status", [""])[0] or None
                edge_type = params.get("edge_type", [""])[0] or None
                limit = int(params.get("limit", ["100"])[0])
                relations = rs.list_all(status=status, edge_type=edge_type, limit=limit)
                self._json(200, {"relations": relations, "stats": rs.stats()})
            except Exception as e:
                self._json(200, {"relations": [], "stats": {}, "error": str(e)})

        # ── Memory: summary tree (GET) ──
        elif path == "/api/memory/summary-tree":
            try:
                from memory.summary_tree import SummaryTree
                st = SummaryTree()
                layer = int(params.get("layer", ["-1"])[0])
                if layer >= 0:
                    nodes = st.list_layer(layer)
                    self._json(200, {"nodes": nodes, "stats": st.stats()})
                else:
                    tree = st.get_tree()
                    self._json(200, {"tree": tree, "stats": st.stats()})
            except Exception as e:
                self._json(200, {"tree": None, "stats": {}, "error": str(e)})
        elif path == "/api/tasks":
            try:
                dbc = get_db()
                conn = dbc.conn if hasattr(dbc, 'conn') else None
                if conn:
                    # [2026-08-19 修复] 旧库 task_instances 缺 created_at/topic_id/dag_snapshot 列
                    # (db.py v4.0 旧 SCHEMA 创建，task/dag.py 的迁移只在 DAG 实例化时执行)。
                    # 这里先补列再查询，避免 "no such column: created_at" 500。
                    for col, decl in (("created_at", "REAL"), ("topic_id", "TEXT"), ("dag_snapshot", "TEXT")):
                        try:
                            conn.execute("SELECT %s FROM task_instances LIMIT 0" % col)
                        except Exception:
                            try:
                                conn.execute("ALTER TABLE task_instances ADD COLUMN %s %s" % (col, decl))
                                conn.commit()
                            except Exception:
                                pass
                    rows = conn.execute("SELECT * FROM task_instances ORDER BY COALESCE(created_at, updated_at) DESC LIMIT 20").fetchall()
                    tasks = [dict(r) for r in rows]
                else:
                    tasks = []
                self._json(200, {"tasks": tasks})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.startswith("/api/task/") and "/dag" in path:
            try:
                parts = path.split("/")
                # /api/task/{task_id}/dag
                task_id = parts[3]
                from task.dag import DAG
                dag = DAG(task_id)
                x6_data = dag.get_x6_json()
                self._json(200, {"task": dag.get_task(), "x6": x6_data})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.startswith("/api/task/") and path.endswith("/tree"):
            try:
                parts = path.split("/")
                task_id = parts[3]
                from task.dag import DAG
                dag = DAG(task_id)
                tree = dag.get_tree()
                task_info = dag.get_task()
                # Stats
                all_nodes = dag.get_nodes()
                status_counts = {}
                for n in all_nodes:
                    s = n["status"]
                    status_counts[s] = status_counts.get(s, 0) + 1
                self._json(200, {
                    "task": task_info,
                    "tree": tree,
                    "nodes": all_nodes,
                    "stats": status_counts,
                })
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path.startswith("/api/task/") and "/work-memory" in path:
            try:
                parts = path.split("/")
                task_id = parts[3]
                from task.work_memory import WorkMemory
                wm = WorkMemory(task_id)
                entries = wm.get_open_entries()
                self._json(200, {"entries": entries})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Topic DAG (flow chart) — lookup by chat topic_id ──
        # Returns X6-standard {nodes, edges} for direct graph.fromJSON()
        elif path.startswith("/api/topic/") and path.endswith("/dag"):
            try:
                parts = path.split("/")
                topic_id = parts[3]
                from task.dag import DAG
                task_id, task_info = DAG.get_task_by_topic(topic_id)
                if not task_id:
                    self._json(200, {"task": None, "x6": {"nodes": [], "edges": []}, "stats": {}})
                    return
                dag = DAG(task_id)
                x6_data = dag.get_x6_json()
                all_nodes = dag.get_nodes()
                status_counts = {}
                for n in all_nodes:
                    s = n["status"]
                    status_counts[s] = status_counts.get(s, 0) + 1
                self._json(200, {
                    "task": task_info,
                    "x6": x6_data,
                    "stats": status_counts,
                })
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Mindmap GET: /api/topic/{tid}/mindmap ──
        elif path.startswith("/api/topic/") and path.endswith("/mindmap"):
            try:
                parts = path.split("/")
                topic_id = parts[3]
                mm_path = os.path.join(ROOT_DIR, "data", "missions", topic_id, "mindmap.json")
                if os.path.exists(mm_path):
                    with open(mm_path, "r", encoding="utf-8") as f:
                        mm = json.load(f)
                    # 返回当前历史指针，让前端显示撤销/重做可用性
                    try:
                        from tools.mindmap_tools import _mm_history_get_current_seq
                        mm["current_seq"] = _mm_history_get_current_seq(topic_id)
                    except Exception:
                        pass
                    self._json(200, {"ok": True, "mindmap": mm})
                else:
                    self._json(200, {"ok": True, "mindmap": {
                        "schema": 1, "topic_id": topic_id, "title": "",
                        "updated_at": 0, "nodes": [], "edges": []
                    }})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Mindmap history: GET /api/topic/{tid}/mindmap/history ──
        elif path.startswith("/api/topic/") and path.endswith("/mindmap/history"):
            try:
                parts = path.split("/")
                topic_id = parts[3]
                limit = int(params.get("limit", ["20"])[0])
                from tools.mindmap_tools import _mm_history_list
                self._json(200, _mm_history_list(topic_id, limit))
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Important matters ──
        elif path == "/api/important-matters":
            self._json(200, {"matters": _get_matters()})

        # ── 空态活标语：AI 建议池随机 > 时段模板 > 静态兜底 ──
        elif path == "/api/slogan":
            import random
            cfg = _suggestion_cfg()
            pool = _suggestion_pool() if cfg["enabled"] else []
            if pool:
                self._json(200, {"text": random.choice(pool), "source": "ai"})
            else:
                self._json(200, {"text": _suggestion_template(), "source": "tpl"})

        # ── Domain book ──
        elif path == "/api/domain-book":
            try:
                book_path = os.path.join(ROOT_DIR, "data", "domain_book.json")
                if os.path.exists(book_path):
                    with open(book_path, "r", encoding="utf-8") as f:
                        book = json.load(f)
                else:
                    book = {"current_page": "default", "pages": {}}
                self._json(200, book)
            except Exception as e:
                self._json(500, {"error": str(e)})
        # ── List dir ──
        elif path == "/api/list-dir":
            try:
                req_path = params.get("path", [""])[0]
                if not req_path:
                    # On Windows, show drive letters; elsewhere, show root
                    if os.name == 'nt':
                        import string
                        drives = []
                        for letter in string.ascii_uppercase:
                            drv = letter + ":\\"
                            if os.path.exists(drv):
                                drives.append({"name": letter + ":\\", "is_dir": True})
                        self._json(200, {"entries": drives, "path": "", "parent": None})
                        return
                    else:
                        req_path = "/"
                # Windows: 'C:'（无反斜杠）是当前目录相对路径，不是盘符根。
                # 补成 'C:\' 才能正确列出盘符根目录。
                if os.name == 'nt' and len(req_path) == 2 and req_path[1] == ':':
                    req_path = req_path + '\\'
                # Normalize path
                req_path = os.path.normpath(req_path)
                parent = os.path.dirname(req_path)
                # Don't go above drive root on Windows
                if os.name == 'nt':
                    if req_path.endswith(':\\'):
                        parent = ""
                    elif parent == req_path:
                        parent = ""
                entries = []
                try:
                    for name in os.listdir(req_path):
                        full = os.path.join(req_path, name)
                        try:
                            is_dir = os.path.isdir(full)
                            entries.append({"name": name, "is_dir": is_dir})
                        except OSError:
                            pass
                except PermissionError:
                    pass
                entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
                self._json(200, {"entries": entries, "path": req_path, "parent": parent})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Recycle list ──
        elif path == "/api/recycle/list":
            recycle_dir = os.path.join(ROOT_DIR, "data", "trash")
            items = []
            if os.path.isdir(recycle_dir):
                for name in os.listdir(recycle_dir):
                    full = os.path.join(recycle_dir, name)
                    items.append({"name": name, "mtime": os.path.getmtime(full),
                                  "is_dir": os.path.isdir(full)})
                items.sort(key=lambda x: x["mtime"], reverse=True)
            self._json(200, {"items": items})

        # ── Debug logs ──
        elif path.startswith("/api/debug/logs"):
            self._json(200, {"logs": []})

        # ── Raw API logs (v4.0) ──
        elif path == "/api/raw-api-logs":
            try:
                from api_logger import get_logger
                limit = int(params.get("limit", ["50"])[0])
                offset = int(params.get("offset", ["0"])[0])
                topic_id = params.get("topic_id", [""])[0] or None
                logger = get_logger()
                logs = logger.fetch_logs(limit=limit, offset=offset, topic_id=topic_id)
                total = logger.count_logs(topic_id=topic_id)
                self._json(200, {"logs": logs, "total": total})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Mission files (list) ──
        elif path.startswith("/api/mission-files"):
            tid = params.get("topic_id", [""])[0]
            subdir = params.get("subdir", [""])[0]
            mission_dir = os.path.join(ROOT_DIR, "data", "missions", tid)
            # Use workspace path if set, otherwise default to project root
            ws_rel = self.db.get_topic_meta(tid, "workspace")
            ws_path = _resolve_workspace(ws_rel, tid)
            # If subdir is an absolute path (e.g. D: or D:\...), use it directly
            if subdir and (re.match(r'^[A-Za-z]:[\\\/]?', subdir) or subdir.startswith('\\\\')):
                # Normalize drive root "D:" -> "D:\" (Windows quirk: "D:" refers to
                # the current directory on drive D:, not its root)
                if re.match(r'^[A-Za-z]:$', subdir):
                    subdir = subdir + '\\'
                list_dir = subdir
            else:
                list_dir = os.path.join(ws_path, subdir) if subdir else ws_path
            if not os.path.isdir(list_dir):
                list_dir = ws_path if os.path.isdir(ws_path) else ROOT_DIR
                subdir = ""
            files = []
            if os.path.isdir(list_dir):
                for name in os.listdir(list_dir):
                    full = os.path.join(list_dir, name)
                    try:
                        st = os.stat(full)
                        files.append({"name": name, "is_dir": os.path.isdir(full),
                                      "size": st.st_size, "mtime": st.st_mtime})
                    except OSError:
                        files.append({"name": name, "is_dir": os.path.isdir(full),
                                      "size": 0, "mtime": 0})
            _allow_outside = (self.db.get_topic_meta(tid, "allow_outside") == "1")
            self._json(200, {"files": files, "workspace": os.path.abspath(ws_path), "subdir": subdir, "mission_dir": mission_dir, "allow_outside": _allow_outside})
        # ── Mission file (download) ──
        elif path.startswith("/api/mission-file"):
            tid = params.get("topic_id", [""])[0]
            filename = params.get("file", [""])[0]
            # Resolve from workspace first, fallback to mission dir
            ws_rel = self.db.get_topic_meta(tid, "workspace")
            ws_path = _resolve_workspace(ws_rel, tid)
            filepath = os.path.join(ws_path, filename)
            if not (os.path.exists(filepath) and os.path.isfile(filepath)):
                filepath = os.path.join(ROOT_DIR, "data", "missions", tid, filename)
            if os.path.exists(filepath) and os.path.isfile(filepath):
                with open(filepath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json(404, {"error": "file not found"})


        # ── Debug: history stats ──
        elif path == "/api/debug/history":
            tid = params.get("topic_id", [""])[0]
            if tid:
                raw = self.db.get_messages(tid, limit=50)
                self._json(200, {"count": len(raw), "first": raw[0]["text"][:60] if raw else "none",
                                 "last": raw[-1]["text"][:60] if raw else "none"})
            else:
                self._json(400, {"error": "topic_id required"})

        # ── CMN Viz: 晶体网络图 ──
        elif path == "/api/cmn/viz/network":
            try:
                from memory.cmn_viz import build_network_graph
                limit = int(params.get("limit", ["100"])[0])
                include_files = params.get("include_files", ["1"])[0] != "0"
                include_self = params.get("include_self", ["1"])[0] != "0"
                result = build_network_graph(
                    limit=limit, include_files=include_files, include_self=include_self
                )
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── CMN Viz: 实体关系图 ──
        elif path == "/api/cmn/viz/relations":
            try:
                from memory.cmn_viz import build_relations_graph
                limit = int(params.get("limit", ["200"])[0])
                edge_type = params.get("edge_type", [None])[0]
                result = build_relations_graph(limit=limit, edge_type=edge_type)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── CMN Viz: 金字塔树形 ──
        elif path == "/api/cmn/viz/pyramid":
            try:
                from memory.cmn_viz import get_pyramid_tree, pyramid_to_x6
                fpath = params.get("path", [""])[0]
                if not fpath:
                    self._json(400, {"error": "缺少 path 参数"})
                    return
                pyramid = get_pyramid_tree(fpath)
                result = pyramid_to_x6(pyramid)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── CMN Viz: 已建晶体文件列表 ──
        elif path == "/api/cmn/viz/files":
            try:
                from memory.cmn_viz import list_indexed_files
                files = list_indexed_files()
                self._json(200, {"files": files, "count": len(files)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Reflection Loop: detect gaps (GET) ──
        elif path == "/api/reflection/gaps":
            try:
                from memory.reflection_loop import get_loop
                gaps = get_loop().detect_gaps()
                self._json(200, {"gaps": gaps, "count": len(gaps)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Static files ──
        elif path == "/":
            self._serve_static("/index.html")
        else:
            self._serve_static(path)

    def _serve_static(self, path):
        """Serve a static file from PUBLIC_DIR."""
        # Security: prevent path traversal
        safe = os.path.normpath(path).lstrip("/\\")
        filepath = os.path.join(PUBLIC_DIR, safe)
        if not filepath.startswith(os.path.normpath(PUBLIC_DIR)):
            self._json(403, {"error": "forbidden"})
            return
        if not os.path.isfile(filepath):
            self._json(404, {"error": "not found"})
            return
        # Determine content type
        ext = os.path.splitext(filepath)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".json": "application/json",
            ".apk": "application/vnd.android.package-archive",
        }.get(ext, "application/octet-stream")
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json(500, {"error": str(e)})

    # ── POST ───────────────────────────────────────
    def do_POST(self):
        if not self._check_auth():
            self._json(403, {"error": "unauthorized"})
            return

        path = self.path.split("?")[0]   # 与 do_GET 一致：去掉 query string，避免带参请求 404
        params = self._parse_query()
        try:
            body = self._read_body().decode("utf-8")
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        # ── Chat ──────────────────────────────────
        if path == "/api/chat":
            try:
                self._handle_chat(data)
            except Exception as e:
                import traceback
                traceback.print_exc()
                # 清理 working state，否则前端永久显示"处理中…"
                tid = data.get("topic_id", "")
                if tid:
                    clear_working(tid)
                    with _cancel_lock:
                        _cancel_events.pop(tid, None)
                try:
                    self._json(500, {"error": str(e)})
                except Exception:
                    pass
                return


        # ── Save topic DAG (user edits from flowchart UI) ──
        # POST /api/topic/{id}/dag  body: {x6: {nodes, edges}, allow_structural_change: bool}
        elif path.startswith("/api/topic/") and path.endswith("/dag"):
            try:
                parts = path.split("/")
                topic_id = parts[3]
                from task.dag import DAG
                task_id, _ = DAG.get_task_by_topic(topic_id)
                if not task_id:
                    self._json(404, {"error": "no task found for topic"})
                    return
                dag = DAG(task_id)
                x6_data = data.get("x6", {})
                allow_structural = bool(data.get("allow_structural_change", False))
                result = dag.save_x6_json(x6_data, allow_structural_change=allow_structural)
                if "error" in result:
                    self._json(400, result)
                else:
                    updated_x6 = dag.get_x6_json()
                    self._json(200, {"ok": True, "result": result, "x6": updated_x6})
            except Exception as e:
                import traceback
                self._json(500, {"error": str(e), "trace": traceback.format_exc()})

        # ── Mindmap POST: /api/topic/{tid}/mindmap ──
        # body: {mindmap: {...}, action: "..."}  整图覆盖，原子写 + 记历史
        elif path.startswith("/api/topic/") and path.endswith("/mindmap"):
            try:
                parts = path.split("/")
                topic_id = parts[3]
                mm_data = data.get("mindmap", {})
                mm_data["topic_id"] = topic_id
                mm_data["schema"] = 1
                action = data.get("action", "user_edit")
                # 走 _save_mindmap：原子写 + 自动记历史
                from tools.mindmap_tools import _save_mindmap
                _save_mindmap(topic_id, mm_data, action=action, source="user")
                self._json(200, {"ok": True, "mindmap": mm_data})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Mindmap undo: POST /api/topic/{tid}/mindmap/undo ──
        elif path.startswith("/api/topic/") and path.endswith("/mindmap/undo"):
            try:
                parts = path.split("/")
                topic_id = parts[3]
                steps = data.get("steps", 1)
                from tools.mindmap_tools import _mm_history_goto, _mm_history_get_current_seq
                cur_seq = _mm_history_get_current_seq(topic_id)
                target_seq = max(1, cur_seq - max(1, min(int(steps), 100)))
                if target_seq == cur_seq:
                    self._json(200, {"ok": True, "hint": "已在最早版本", "current_seq": cur_seq})
                else:
                    result = _mm_history_goto(topic_id, target_seq, source="user_undo")
                    self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Mindmap redo: POST /api/topic/{tid}/mindmap/redo ──
        elif path.startswith("/api/topic/") and path.endswith("/mindmap/redo"):
            try:
                parts = path.split("/")
                topic_id = parts[3]
                steps = data.get("steps", 1)
                from tools.mindmap_tools import _mm_history_goto, _mm_history_get_current_seq, _mm_history_db
                cur_seq = _mm_history_get_current_seq(topic_id)
                conn = _mm_history_db()
                row = conn.execute("SELECT MAX(seq) FROM mindmap_history WHERE topic_id=?", (topic_id,)).fetchone()
                max_seq = row[0] if row and row[0] else 0
                target_seq = min(max_seq, cur_seq + max(1, min(int(steps), 100)))
                if target_seq == cur_seq:
                    self._json(200, {"ok": True, "hint": "已是最新版本", "current_seq": cur_seq})
                else:
                    result = _mm_history_goto(topic_id, target_seq, source="user_redo")
                    self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Stop thinking (force-end paused or active loop) ──
        elif path == "/api/stop-thinking":
            tid = data.get("topic_id", "")
            with _paused_lock:
                state = _paused_loops.pop(tid, None)
            if state:
                self.db.add_message(tid, "ai", state["full_response"],
                                    thinking=state["full_thinking"], ts=time.time())
                self.db.update_topic(tid, updated_at=time.time())
                clear_working(tid)
                self._json(200, {"status": "stopped", "response": state["full_response"]})
            else:
                # Check if there's an active chat loop to cancel
                with _cancel_lock:
                    evt = _cancel_events.get(tid)
                if evt:
                    evt.set()
                    self._json(200, {"status": "cancelling", "response": ""})
                else:
                    self._json(404, {"error": "no paused loop"})
        # ── Topic: new ──
        elif path == "/api/topic/new":
            try:
                topic = self.topics.create_topic(data.get("title"))
                self.db.set_active_topic_id(topic["id"])
                # 立即创建工作区目录，避免文件管理器打开时回退到项目根目录
                mission_dir = os.path.join(ROOT_DIR, "data", "missions", topic["id"])
                os.makedirs(mission_dir, exist_ok=True)
                ws_dir = os.path.join(mission_dir, "workspace")
                os.makedirs(ws_dir, exist_ok=True)
                if not self.db.get_topic_meta(topic["id"], "workspace"):
                    self.db.set_topic_meta(topic["id"], "workspace", os.path.abspath(ws_dir))
                self._json(200, topic)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Topic: delete ──
        elif path == "/api/topic/delete":
            try:
                tid = data.get("topic_id", "")
                if tid:
                    self.db.delete_topic(tid)
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Topic: rename ──
        elif path == "/api/topic/rename":
            try:
                tid = data.get("topic_id", "")
                title = data.get("title", "")
                if tid and title:
                    self.db.rename_topic(tid, title)
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Topic: switch ──
        elif path == "/api/topic/switch":
            tid = data.get("topic_id", "")
            if tid:
                self.db.set_active_topic_id(tid)
                mission_dir = os.path.join(ROOT_DIR, "data", "missions", tid)
                os.makedirs(mission_dir, exist_ok=True)
                ws_dir = os.path.join(mission_dir, "workspace")
                os.makedirs(ws_dir, exist_ok=True)
                if not self.db.get_topic_meta(tid, "workspace"):
                    self.db.set_topic_meta(tid, "workspace", os.path.abspath(ws_dir))
                self._json(200, {"success": True})
            else:
                self._json(400, {"error": "topic_id required"})
        # ── Switch model ──
        elif path == "/api/switch-model":
            provider = data.get("provider", "")
            model_id = data.get("model_id", "")
            if provider and model_id:
                global _model_config
                provider_urls = {
                    "deepseek": "https://api.deepseek.com",
                    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
                    "openrouter": "https://openrouter.ai/api/v1",
                    "ollama": "http://localhost:11434/v1",
                }
                base_url = provider_urls.get(provider, "")
                with _config_lock:
                    old_cfg = get_model_config()
                    # 切换 provider 时，从 fallback_chain 取对应 provider 的 api_key
                    # 否则切换到 deepseek 后 api_key 还是 ollama 的，导致 401
                    new_key = _get_provider_key(old_cfg, provider)
                    _model_config = old_cfg.clone_with(
                        provider=provider, model=model_id,
                        base_url=base_url, api_key=new_key)
                _save_model_config()
            self._json(200, {"success": True})

        # ── Model key (save) ──
        elif path == "/api/model-key":
            api_key = data.get("api_key", "")
            target_provider = data.get("provider", "")
            # 校验 key：只允许 ASCII 可见字符，拒绝中文/换行等（防止前端误传页面文本）
            # Ollama 等本地 provider 允许任意 ASCII；空 key 走删除流程
            if api_key:
                try:
                    api_key.encode("latin-1")
                except UnicodeEncodeError:
                    self._json(400, {"success": False, "error": "API Key 含非法字符（中文或特殊符号），请检查输入"})
                    return
                if "\n" in api_key or "\r" in api_key or len(api_key) > 256:
                    self._json(400, {"success": False, "error": "API Key 格式异常（含换行或过长），请检查输入"})
                    return
            with _config_lock:
                cfg = get_model_config()
                if not target_provider or target_provider == cfg.provider:
                    # 改主 config 的 api_key
                    _model_config = cfg.clone_with(api_key=api_key)
                else:
                    # 改 fallback_chain 里对应 provider 的 api_key
                    new_chain = []
                    found = False
                    for item in cfg.fallback_chain:
                        item_copy = dict(item)
                        if item_copy.get("provider") == target_provider:
                            item_copy["api_key"] = api_key
                            found = True
                        new_chain.append(item_copy)
                    if not found:
                        # fallback 里没这个 provider，追加一条
                        new_chain.append({
                            "provider": target_provider,
                            "model": data.get("model_id", ""),
                            "api_key": api_key,
                        })
                    _model_config = cfg.clone_with(fallback_chain=new_chain)
                _save_model_config()
            self._json(200, {"success": True})

        # ── Project config ──
        elif path == "/api/project-config":
            if "app_name" in data:
                db = get_db()
                db.set_meta("app_name", data["app_name"])
            self._json(200, {"success": True})

        # ─ Banner settings ──
        elif path == "/api/settings/banner":
            db = get_db()
            if "hide_banner" in data:
                db.set_meta("hide_banner", str(data["hide_banner"]))

        # ── Balance display settings ──
        elif path == "/api/settings/balance_display":
            db = get_db()
            if "show_balance" in data:
                db.set_meta("show_balance", str(data["show_balance"]))

        # ── Dev mode ──
        elif path == "/api/settings/devmode":
            db = get_db()
            on = bool(data.get("dev_mode", False))
            db.set_meta("dev_mode", "1" if on else "0")
            global _DEV_MODE
            _DEV_MODE = on
            try:
                import api_logger
                api_logger.set_enabled(on)
            except Exception:
                pass
            self._json(200, {"ok": True, "dev_mode": on})

        # ── LLM 响应本地缓存：开关 + 清空 ──
        elif path == "/api/settings/llm_cache":
            db = get_db()
            if "enabled" in data:
                on = bool(data["enabled"])
                db.set_meta("llm_cache_enabled", "1" if on else "0")
                global _LLM_CACHE_ENABLED
                _LLM_CACHE_ENABLED = on
                try:
                    import llm as _llm_mod
                    _llm_mod.set_cache_enabled(on)
                except Exception:
                    pass
            if data.get("clear"):
                try:
                    import llm_cache
                    llm_cache.get_cache().clear()
                except Exception:
                    pass
            stats = {}
            try:
                import llm_cache
                stats = llm_cache.get_cache().stats()
            except Exception:
                pass
            self._json(200, {"ok": True,
                             "enabled": db.get_meta("llm_cache_enabled") == "1",
                             "stats": stats})

        # ── AI 自动建议分析：开关/频率/立即生成/预览 ──
        elif path == "/api/settings/suggestions":
            db = get_db()
            cfg = _suggestion_cfg()
            # 统一 enabled 为 "1"/"0" 字符串：_suggestion_cfg() 返回布尔，
            # 若不规范化，读请求返回时 cfg["enabled"]=="1" 恒为 False（True!="1"）
            cfg["enabled"] = "1" if cfg["enabled"] else "0"
            if "enabled" in data:
                cfg["enabled"] = "1" if data["enabled"] else "0"
            if "freq" in data and str(data["freq"]) in _SUGGESTION_FREQ_HOURS:
                cfg["freq"] = str(data["freq"])
            # 统一存 "1"/"0" 字符串（cfg["enabled"] 已是规范化字符串，
            # 不能再三元判断——非空字符串 "0" 是 truthy，会导致关闭永远写不进库）
            db.set_meta("suggestion_cfg", json.dumps(
                {"enabled": cfg["enabled"], "freq": cfg["freq"]},
                ensure_ascii=False))
            resp = {"ok": True, "enabled": cfg["enabled"] == "1", "freq": cfg["freq"],
                    "pool": _suggestion_pool(),
                    "gen_at": float(db.get_meta("suggestion_gen_at") or 0)}
            if data.get("generate"):
                ok, msg = generate_suggestions_now()
                resp["gen_msg"] = msg
                if ok:
                    resp["pool"] = _suggestion_pool()
                    resp["gen_at"] = float(db.get_meta("suggestion_gen_at") or 0)
            self._json(200, resp)

        # ── 阻塞式提问回答（ask_user）──
        elif path == "/api/ask_user/answer":
            from tools.ask_user import submit_answer
            qid = data.get("question_id", "")
            answer = str(data.get("answer", "")).strip()
            if not qid:
                self._json(400, {"error": "缺少 question_id"})
            elif not answer:
                self._json(400, {"error": "回答不能为空"})
            elif submit_answer(qid, answer):
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "该提问已过期或已回答"})

        # ── AI 文件修改恢复（时间机器交互用）──
        elif path == "/api/file_restore":
            from tools.file_tools import file_restore as _fr
            result = _fr(data.get("path", ""), int(data.get("record_id", 0)))
            code = 200 if not (isinstance(result, dict) and result.get("error")) else 400
            self._json(code, result)

        # ── User avatar ──
        elif path == "/api/settings/avatar":
            db = get_db()
            av = data.get("avatar", "")
            db.set_meta("user_avatar", av or "")
            self._json(200, {"ok": True})

        # ── Compress settings ──
        elif path == "/api/settings/compress":
            db = get_db()
            if "per_message_chars" in data:
                db.set_meta("compress_per_msg_chars", str(data["per_message_chars"]))
            if "token_limit" in data:
                # 钳制到 [最小可设值, 模型上下文窗口]
                tl = int(data["token_limit"])
                tl = max(tl, _wm_min_token_limit())
                tl = min(tl, _model_context_window(get_model_config().model))
                db.set_meta("compress_token_limit", str(tl))
            if "warn_threshold_pct" in data:
                db.set_meta("compress_warn_pct", str(data["warn_threshold_pct"]))
            if "turn_budget" in data:
                db.set_meta("compress_turn_budget", str(data["turn_budget"]))
            if "msg_count_warn" in data:
                db.set_meta("compress_msg_count_warn", str(data["msg_count_warn"]))
            if "msg_count_wall" in data:
                db.set_meta("compress_msg_count_wall", str(data["msg_count_wall"]))
            if "verify_ssl" in data:
                db.set_meta("verify_ssl", "1" if data["verify_ssl"] else "0")
            _refresh_compress_config()
            self._json(200, {"success": True})

        # ── Tools enable/disable save ──
        elif path == "/api/settings/tools":
            db = get_db()
            enabled_list = data.get("enabled", [])
            if not isinstance(enabled_list, list):
                self._json(400, {"success": False, "error": "enabled must be a list"})
                return
            db.set_meta("tools_enabled", json.dumps(enabled_list, ensure_ascii=False))
            _refresh_tools_enabled()
            self._json(200, {"success": True, "enabled_count": len(enabled_list)})

        # ── Perspective settings (用户控制，AI 只读) ──
        elif path == "/api/settings/perspective":
            from tools.perspective import set_perspective_mode, get_perspective
            mode = data.get("mode", "").strip()
            if mode not in ("self_aware", "human"):
                self._json(400, {"success": False, "error": "mode 必须是 'self_aware' 或 'human'"})
                return
            ok = set_perspective_mode(mode)
            if ok:
                label = {"self_aware": "有自我意识的存在", "human": "拟人化"}.get(mode, mode)
                self._json(200, {"success": True, "mode": mode, "label": label})
            else:
                self._json(500, {"success": False, "error": "切换失败"})
        # ── Upload ──
        elif path == "/api/upload":
            try:
                tid = data.get("topic_id", "")
                filename = data.get("filename", "file")
                file_data = data.get("data", "")
                dest_dir_req = data.get("dest_dir")
                import base64
                raw = base64.b64decode(file_data) if file_data else b""
                # Upload to workspace if set, otherwise mission dir
                ws_rel = self.db.get_topic_meta(tid, "workspace")
                ws_path = _resolve_workspace(ws_rel, tid)
                default_dir = ws_path if os.path.isdir(ws_path) else os.path.join(ROOT_DIR, "data", "missions", tid)
                if dest_dir_req is not None and dest_dir_req != "":
                    # 文件管理器"上传到当前目录"：校验目录合法性
                    d = dest_dir_req
                    if re.match(r"^[A-Za-z]:$", d):
                        d = d + "\\"
                    d_abs = os.path.abspath(d)
                    allow_outside = (self.db.get_topic_meta(tid, "allow_outside") == "1")
                    mission_dir = os.path.join(ROOT_DIR, "data", "missions", tid)
                    def _in_dir(base, child):
                        try:
                            return os.path.commonpath([os.path.abspath(base), os.path.abspath(child)]) == os.path.abspath(base)
                        except ValueError:
                            return False  # 跨盘符
                    in_ws = _in_dir(ws_path, d_abs)
                    in_md = _in_dir(mission_dir, d_abs)
                    if not (allow_outside or in_ws or in_md):
                        self._json(403, {"success": False, "error": "目标目录在工作区外，未开启跨工作区访问"})
                        return
                    dest_dir = d_abs
                else:
                    dest_dir = default_dir
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, filename)
                with open(dest, "wb") as f:
                    f.write(raw)
                self._json(200, {"success": True, "filename": filename,
                                 "size": len(raw)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File op (rename / copy / move) ──
        elif path == "/api/file-op":
            try:
                action = data.get("action", "")
                tid = data.get("topic_id", "")
                src = data.get("file", "")
                dst = data.get("target", "")
                # Resolve relative to workspace, fallback to mission dir
                ws_rel = self.db.get_topic_meta(tid, "workspace")
                ws_path = _resolve_workspace(ws_rel, tid)
                src_path = os.path.join(ws_path, src)
                if not os.path.exists(src_path):
                    src_path = os.path.join(ROOT_DIR, "data", "missions", tid, src)
                dst_path = os.path.join(ws_path, dst)
                os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
                if action == "rename" or action == "move" or action == "cut":
                    if src and dst:
                        os.rename(src_path, dst_path)
                elif action == "copy":
                    if src and dst:
                        import shutil
                        if os.path.isdir(src_path):
                            shutil.copytree(src_path, dst_path)
                        else:
                            shutil.copy2(src_path, dst_path)
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Workspace ──
        elif path == "/api/workspace":
            try:
                tid = data.get("topic_id", "")
                ws_path = data.get("path", "")
                allow_outside = data.get("allow_outside")
                if tid:
                    db = get_db()
                    if ws_path:
                        # Windows: 'C:'（无反斜杠）是当前目录相对路径，补成 'C:\'
                        if os.name == 'nt' and len(ws_path) == 2 and ws_path[1] == ':':
                            ws_path = ws_path + '\\'
                        # 存储绝对路径（规范化），避免跨盘符 relpath 异常
                        if os.path.isabs(ws_path):
                            ws_store = os.path.abspath(ws_path)
                        else:
                            ws_store = os.path.abspath(os.path.join(ROOT_DIR, ws_path))
                        db.set_topic_meta(tid, "workspace", ws_store)
                    if allow_outside is not None:
                        db.set_topic_meta(tid, "allow_outside", "1" if allow_outside else "0")
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Recycle: restore ──
        elif path == "/api/recycle/restore":
            self._json(200, {"success": True})

        # ── Recycle: clear ──
        elif path == "/api/recycle/clear":
            self._json(200, {"success": True})


        # ── Memory: entity upsert ──
        elif path == "/api/memory/entity":
            try:
                from memory.entity_store import EntityStore
                es = EntityStore()
                name = data.get("name", "")
                entity_type = data.get("type", "person")
                aliases = data.get("aliases", [])
                if not name:
                    self._json(400, {"error": "name required"})
                    return
                eid = es.upsert(name, entity_type, aliases)
                self._json(200, {"id": eid})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Memory: relation add ──
        elif path == "/api/memory/relation":
            try:
                from memory.relation_store import RelationStore
                rs = RelationStore()
                subject_id = data.get("subject_id")
                predicate = data.get("predicate", "")
                object_id = data.get("object_id")
                object_value = data.get("object_value")
                edge_type = data.get("edge_type", "fact")
                confidence = data.get("confidence", 0.8)
                if not subject_id or not predicate:
                    self._json(400, {"error": "subject_id and predicate required"})
                    return
                result = rs.upsert_with_invalidation(
                    subject_id, predicate, object_id, object_value,
                    edge_type=edge_type, confidence=confidence,
                )
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Memory: entity deactivate stale ──
        elif path == "/api/memory/entity/deactivate-stale":
            try:
                from memory.entity_store import EntityStore
                es = EntityStore()
                count = es.deactivate_stale()
                self._json(200, {"deactivated": count})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Memory: recall (test recall from v4.0) ──
        elif path == "/api/memory/recall":
            try:
                store = _get_memory_store()
                if not store:
                    self._json(200, {"fragments": []})
                    return
                context = data.get("context", "")
                top_k = int(data.get("top_k", 5))
                layer = data.get("layer", "core")
                query_entities = data.get("query_entities")
                fragments = store.recall(
                    context, top_k=top_k, layer=layer,
                    query_entities=query_entities,
                )
                self._json(200, {"fragments": fragments[:10]})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Memory: recall archive ──
        elif path == "/api/memory/recall-archive":
            try:
                store = _get_memory_store()
                if not store:
                    self._json(200, {"fragments": []})
                    return
                context = data.get("context", "")
                fragments = store.recall_archive(context)
                self._json(200, {"fragments": fragments[:10]})
            except Exception as e:
                self._json(500, {"error": str(e)})
        # ── Memory: reflect ──
        elif path == "/api/memory/reflect":
            try:
                from memory.reflection import trigger_deep_integration_now
                trigger_deep_integration_now(get_db())
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"error": str(e)})


        # ── Memory: add fragment ──
        elif path == "/api/memory/fragment":
            self._json(200, {"success": True})

        # ── Memory: prune ──
        elif path == "/api/memory/prune":
            try:
                store = _get_memory_store()
                if store:
                    store.prune()
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Memory: clear ──
        elif path == "/api/memory/clear":
            self._json(200, {"success": True})

        # ── File Crystals: build ──
        elif path == "/api/file-crystals/build" and self.command == "POST":
            try:
                body = data
                fpath = body.get("path", "").strip()
                if not fpath:
                    self._json(400, {"error": "缺少 path 参数"})
                    return
                from memory.file_crystal_store import get_store
                store = get_store()
                result = store.build_file_crystals(
                    fpath, source_type=body.get("source_type"),
                    force_rebuild=body.get("force_rebuild", False)
                )
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File Crystals: list by path ──
        elif path == "/api/file-crystals":
            try:
                fpath = params.get("path", [""])[0]
                if not fpath:
                    # 无 path 返回统计
                    from memory.file_crystal_store import get_store
                    self._json(200, get_store().stats())
                    return
                layer = params.get("layer", [None])[0]
                layer = int(layer) if layer is not None else None
                include_stale = params.get("include_stale", ["0"])[0] == "1"
                from memory.file_crystal_store import get_store
                rows = get_store().get_by_path(fpath, layer=layer, include_stale=include_stale)
                self._json(200, {"crystals": rows, "count": len(rows)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File Crystals: recall ──
        elif path == "/api/file-crystals/recall" and self.command == "POST":
            try:
                body = data
                query = body.get("query", "").strip()
                top_k = int(body.get("top_k", 5))
                if not query:
                    self._json(400, {"error": "缺少 query 参数"})
                    return
                from memory.file_crystal_store import get_store
                rows = get_store().recall(query, top_k=top_k)
                self._json(200, {"crystals": rows, "count": len(rows)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File Crystals: detect changes ──
        elif path == "/api/file-crystals/changes":
            try:
                fpath = params.get("path", [""])[0]
                if not fpath:
                    self._json(400, {"error": "缺少 path 参数"})
                    return
                from memory.file_crystal_store import get_store
                result = get_store().detect_hash_changes(fpath)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File Crystals: build pyramid ──
        elif path == "/api/file-crystals/pyramid" and self.command == "POST":
            try:
                body = data
                fpath = body.get("path", "").strip()
                if not fpath:
                    self._json(400, {"error": "缺少 path 参数"})
                    return
                pack_size = int(body.get("pack_size", 4))
                from memory.file_crystal_store import get_store
                result = get_store().build_pyramid(fpath, pack_size=pack_size)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File Crystals: drill down ──
        elif path == "/api/file-crystals/drill-down":
            try:
                crystal_id = params.get("id", [""])[0]
                if not crystal_id:
                    self._json(400, {"error": "缺少 id 参数"})
                    return
                from memory.file_crystal_store import get_store
                rows = get_store().drill_down(crystal_id)
                self._json(200, {"crystals": rows, "count": len(rows)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File Crystals: drill up ──
        elif path == "/api/file-crystals/drill-up":
            try:
                crystal_id = params.get("id", [""])[0]
                if not crystal_id:
                    self._json(400, {"error": "缺少 id 参数"})
                    return
                from memory.file_crystal_store import get_store
                result = get_store().drill_up(crystal_id)
                self._json(200, {"parent": result})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── File Crystals: read top layer ──
        elif path == "/api/file-crystals/top":
            try:
                fpath = params.get("path", [""])[0]
                if not fpath:
                    self._json(400, {"error": "缺少 path 参数"})
                    return
                from memory.file_crystal_store import get_store
                result = get_store().read_top_layer(fpath)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Reflection Loop: run ──
        elif path == "/api/reflection/run" and self.command == "POST":
            try:
                from memory.reflection_loop import get_loop
                report = get_loop().run()
                self._json(200, report)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── CMN Retriever: retrieve ──
        elif path == "/api/cmn/retrieve" and self.command == "POST":
            try:
                body = data
                query = body.get("query", "").strip()
                top_k = int(body.get("top_k", 10))
                if not query:
                    self._json(400, {"error": "缺少 query 参数"})
                    return
                from memory.cmn_retriever import get_retriever
                result = get_retriever().retrieve(
                    query, top_k=top_k,
                    include_files=body.get("include_files", True),
                    include_self=body.get("include_self", True),
                    walk_network=body.get("walk_network", True),
                )
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── CMN Retriever: verify hash ──
        elif path == "/api/cmn/verify":
            try:
                # id 优先从 body 取（POST 语义），兼容 query string
                crystal_id = data.get("id") or (params.get("id") or [""])[0]
                if not crystal_id:
                    self._json(400, {"error": "缺少 id 参数"})
                    return
                from memory.cmn_retriever import get_retriever
                result = get_retriever().verify_hash(crystal_id)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── CMN Retriever: stats ──
        elif path == "/api/cmn/stats":
            try:
                from memory.cmn_retriever import get_retriever
                result = get_retriever().stats()
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Debug logs clear ──
        elif path == "/api/debug/logs/clear":
            self._json(200, {"success": True})

        # ── Debug: history stats ──
        elif path == "/api/debug/history":
            tid = params.get("topic_id", [""])[0]
            if tid:
                raw = self.db.get_messages(tid, limit=50)
                self._json(200, {"count": len(raw), "first": raw[0]["text"][:60] if raw else "none",
                                 "last": raw[-1]["text"][:60] if raw else "none"})
            else:
                self._json(400, {"error": "topic_id required"})
        elif path == "/api/generate-image":
            try:
                from image_gen import generate as gen_image
                from tools.registry import execute_tool
                prompt = data.get("prompt", "")
                if not prompt:
                    self._json(400, {"error": "prompt required"})
                else:
                    result = gen_image(prompt, data.get("width", 1024), data.get("height", 768))
                    if result.startswith("/generated/"):
                        self._json(200, {"success": True, "url": result, "prompt": prompt})
                    else:
                        self._json(200, {"success": True, "url": f"/api/files/{result}", "prompt": prompt})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Task: create (v4.0) ──────────────────────
        elif path == "/api/task/create":
            try:
                user_request = data.get("request", "")
                if not user_request:
                    self._json(400, {"error": "request required"})
                    return
                from task.dag import DAG
                from task.work_memory import WorkMemory
                from task.attention_focus import AttentionFocus
                from task.executor import TaskExecutor
                import uuid
                task_id = data.get("task_id", uuid.uuid4().hex[:12])
                dag = DAG(task_id)
                wm = WorkMemory(task_id)
                attention = AttentionFocus()
                store = _get_memory_store()
                executor = TaskExecutor(dag, wm, attention, store)
                task = executor.init_task(user_request)
                self._json(200, {"task": task, "task_id": task_id})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif path == "/api/task/next-node":
            try:
                task_id = data.get("task_id", "")
                if not task_id:
                    self._json(400, {"error": "task_id required"})
                    return
                from task.dag import DAG
                from task.executor import TaskExecutor
                dag = DAG(task_id)
                node = dag.get_runnable_nodes()
                self._json(200, {"nodes": node[:5] if node else []})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path == "/api/task/node/start":
            try:
                task_id = data.get("task_id", "")
                node_id = data.get("node_id", "")
                if not task_id or not node_id:
                    self._json(400, {"error": "task_id and node_id required"})
                    return
                from task.dag import DAG
                from task.work_memory import WorkMemory
                from task.attention_focus import AttentionFocus, slide_in
                dag = DAG(task_id)
                wm = WorkMemory(task_id)
                attention = AttentionFocus()
                store = _get_memory_store()
                slide_in(dag, wm, node_id, store, attention)
                dag.set_status(node_id, "running")
                context = attention.get_all_blocks_text()
                self._json(200, {
                    "node": dag.get_node(node_id),
                    "attention_context": context,
                    "blocks": attention.blocks,
                })
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif path == "/api/task/node/complete":
            try:
                task_id = data.get("task_id", "")
                node_id = data.get("node_id", "")
                result = data.get("result", "")
                issues = data.get("issues", [])
                next_node = data.get("next_node", "")
                trigger_roundtrip = data.get("trigger_roundtrip", False)
                if not task_id or not node_id:
                    self._json(400, {"error": "task_id and node_id required"})
                    return
                from task.dag import DAG
                from task.work_memory import WorkMemory, roundtrip_solve
                from task.attention_focus import AttentionFocus, slide_out
                dag = DAG(task_id)
                wm = WorkMemory(task_id)
                attention = AttentionFocus()
                # Record issues to work memory
                for issue in issues:
                    etype = issue.get("type", "question")
                    text = issue.get("text", "")
                    if text:
                        wm.add_entry(node_id, etype, text)
                # Check for blockers → mark node blocked (idempotent)
                any_blocker = any(i.get("type") == "blocker" for i in issues)
                node = dag.get_node(node_id)
                current_status = node["status"] if node else "pending"
                if any_blocker:
                    if current_status != "blocked":
                        dag.set_status(node_id, "blocked", result=result)
                    else:
                        dag.update_context(node_id, {"_blocker_reported": True})
                elif issues and current_status in ("pending", "running"):
                    dag.set_status(node_id, "running", result=result)
                elif current_status in ("pending", "running"):
                    dag.set_status(node_id, "done", result=result)
                # Check roundtrip trigger
                rt_triggered = False
                q_count = wm.count_questions()
                has_blocker = wm.has_blocker()
                if trigger_roundtrip or has_blocker or q_count >= 5:
                    rt_results = roundtrip_solve(wm)
                    rt_triggered = True
                # Slide out
                slide_out(dag, wm, node_id, attention)
                self._json(200, {
                    "node": dag.get_node(node_id),
                    "roundtrip_triggered": rt_triggered,
                    "next_node_suggestion": next_node,
                    "runnable_nodes": [n["id"] for n in (dag.get_runnable_nodes() or [])],
                })
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif path == "/api/task/finish":
            try:
                task_id = data.get("task_id", "")
                status = data.get("status", "done")
                if not task_id:
                    self._json(400, {"error": "task_id required"})
                    return
                from task.dag import DAG
                from task.work_memory import WorkMemory
                from task.attention_focus import AttentionFocus
                from task.executor import TaskExecutor
                from task.reflection import reflect_and_sediment
                dag = DAG(task_id)
                wm = WorkMemory(task_id)
                attention = AttentionFocus()
                store = _get_memory_store()
                executor = TaskExecutor(dag, wm, attention, store)
                if status == "done":
                    executor.finish_task()
                else:
                    executor.fail_task(data.get("reason", ""))
                # Reflect and sediment to v3.0
                sedi = reflect_and_sediment(executor)
                self._json(200, {"task": dag.get_task(), "sediment": sedi})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif path == "/api/task/work-memory":
            try:
                task_id = data.get("task_id", "")
                node_id = data.get("node_id", "")
                entry_type = data.get("entry_type", "question")
                text = data.get("text", "")
                if not task_id or not text:
                    self._json(400, {"error": "task_id and text required"})
                    return
                from task.work_memory import WorkMemory
                wm = WorkMemory(task_id)
                entry = wm.add_entry(node_id or "__task__", entry_type, text)
                self._json(200, {"entry": entry})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Unknown ──
        else:
            self._json(404, {"error": f"unknown endpoint: {path}"})

    # ── DELETE ─────────────────────────────────────
    def do_DELETE(self):
        path = self.path.split("?")[0]
        params = self._parse_query()

        if path == "/api/model-key":
            # 读 body：前端 deleteKey/clearAllKeys 发的是 JSON body
            try:
                body_raw = self._read_body().decode("utf-8")
                data = json.loads(body_raw) if body_raw else {}
            except Exception:
                data = {}
            clear_all = data.get("all", False)
            target_provider = data.get("provider", "")
            with _config_lock:
                cfg = get_model_config()
                if clear_all:
                    # 清空主 config 和所有 fallback 的 key
                    _model_config = cfg.clone_with(api_key="")
                    new_chain = [dict(item, api_key="") for item in cfg.fallback_chain]
                    _model_config = _model_config.clone_with(fallback_chain=new_chain)
                    deleted = 1 + len(new_chain)
                elif not target_provider or target_provider == cfg.provider:
                    _model_config = cfg.clone_with(api_key="")
                    deleted = 1
                else:
                    new_chain = []
                    deleted = 0
                    for item in cfg.fallback_chain:
                        item_copy = dict(item)
                        if item_copy.get("provider") == target_provider:
                            item_copy["api_key"] = ""
                            deleted = 1
                        new_chain.append(item_copy)
                    _model_config = cfg.clone_with(fallback_chain=new_chain)
                _save_model_config()
            self._json(200, {"success": True, "deleted": deleted})
        elif path == "/api/mission-file":
            tid = params.get("topic_id", [""])[0]
            filename = params.get("file", [""])[0]
            # Resolve from workspace first, fallback to mission dir
            ws_rel = self.db.get_topic_meta(tid, "workspace")
            ws_path = _resolve_workspace(ws_rel, tid)
            filepath = os.path.join(ws_path, filename)
            if not os.path.exists(filepath):
                filepath = os.path.join(ROOT_DIR, "data", "missions", tid, filename)
            if os.path.exists(filepath):
                if os.path.isdir(filepath):
                    import shutil
                    shutil.rmtree(filepath)
                else:
                    os.remove(filepath)
            self._json(200, {"success": True})
        else:
            self._json(404, {"error": "not found"})

    # ── Chat handler (core) ────────────────────────
    def _handle_chat(self, data):
        """Chat: LLM → tool loop → track cost → JSON response."""
        global _round_peak_tokens
        _round_peak_tokens = 0  # 用户新消息 = 新大轮开始，重置工作记忆峰值
        message = data.get("message", "").strip()
        tid = data.get("topic_id", "")
        attachments = data.get("attachments", []) or []
        _image_abs_paths = []  # 图片附件绝对路径（用于 vision 多模态）
        if not message and not attachments:
            self._json(400, {"error": "message required"})
            return
        topic = self.topics.get_or_create(tid)
        tid = topic["id"]
        # Sync active topic so AI tools (current_topic etc.) see the right context
        self.db.set_active_topic_id(tid)
        # Ensure mission directory and workspace exist
        mission_dir = os.path.join(ROOT_DIR, "data", "missions", tid)
        os.makedirs(mission_dir, exist_ok=True)
        ws_dir = os.path.join(mission_dir, "workspace")
        os.makedirs(ws_dir, exist_ok=True)
        # 附件清单注入 user message，附上绝对路径，AI 可直接读文件（避免盲扫工作区）
        # 搜索目录与 /api/upload 的默认落盘逻辑保持一致
        if attachments:
            _ws_path = _resolve_workspace(self.db.get_topic_meta(tid, "workspace"), tid)
            _upload_dir = _ws_path if os.path.isdir(_ws_path) else mission_dir
            _items = []
            for _fn in attachments:
                _p = ""
                for _d in (_upload_dir, ws_dir, mission_dir):
                    _c = os.path.join(_d, _fn)
                    if os.path.isfile(_c):
                        _p = os.path.abspath(_c)
                        break
                _items.append(f"{_fn} -> {_p}" if _p else _fn)
                # 收集图片附件（jpg/png/gif/webp），供 vision 模型多模态接入
                if _p and image_mime_type(_p):
                    _image_abs_paths.append(_p)
            _att_line = "[附件: " + ", ".join(_items) + "]"
            message = (message + "\n" + _att_line).strip() if message else _att_line
        # Auto-set workspace if not already set（存储绝对路径）
        if not self.db.get_topic_meta(tid, "workspace"):
            self.db.set_topic_meta(tid, "workspace", os.path.abspath(ws_dir))
        # 附件清单存进 args，前端刷新后能恢复 chip
        _msg_args = {"attachments": attachments} if attachments else None
        # ── 插话：该话题已有运行中的 agent 循环时，不起第二个并行循环。
        # 消息落库后进入注入队列，运行中的循环会在轮次边界取出、插入上下文继续思考。
        with _cancel_lock:
            _loop_active = _cancel_events.get(tid) is not None
        if _loop_active:
            self.db.add_message(tid, "user", message, args=_msg_args, ts=time.time())
            self.db.update_topic(tid, updated_at=time.time())
            with _inject_lock:
                _inject_queues.setdefault(tid, []).append(message)
            print(f"[agent] message injected into running loop (topic {tid[:8]})")
            self._json(200, {"status": "injected", "response": "", "topic_id": tid})
            return
        self.db.add_message(tid, "user", message, args=_msg_args, ts=time.time())
        # Auto-create task root fragment (first message in topic)
        store = _get_memory_store()
        if store and _memory_enabled:
            existing = store.recall_by_topic(tid, limit=1)
            if not existing:
                ts_str = time.strftime("%Y%m%d%H%M%S")
                store.add(
                    f"开始话题：{topic.get('title', '新任务')}",
                    ts=ts_str,
                    source="task_root",
                    topic_id=tid,
                )
        self.db.update_topic(tid, updated_at=time.time())
        ws_rel = self.db.get_topic_meta(tid, "workspace")
        ws = _resolve_workspace(ws_rel, tid)
        ws_short = ws_rel.replace("\\", "/") if ws_rel else "data/missions/"+tid+"/workspace"
        if len(ws_short) > 60: ws_short = "..." + ws_short[-57:]
        # Normal flow — add message and build history

        store = _get_memory_store()
        # ── Build system message (v4.0 structured blocks) ──
        blocks = []

        # Block 1: 身份与行为规则（重要事项）—— 视角敏感的标题
        from tools.perspective import vocab as _vocab
        matters = _get_matters()
        if matters:
            rule_lines = []
            for i, entry in enumerate(matters, 1):
                rule_lines.append(f"{i}. {entry}")
            rule_lines.append("")
            rule_lines.append(_vocab("matters_manage_hint"))
            blocks.append(_vocab("matters_title") + "\n" + "\n".join(rule_lines))

        # Block 1.1: 互动基调（氛围记忆，表达调节）——中性标题，不随视角切换
        try:
            from tools.important_matters import get_tone as _get_tone
            _tone = _get_tone()
            if _tone and _tone.get("tone"):
                _trust_map = {
                    "high": "高（可直说、可反驳、少客气话）",
                    "mid": "中（正常交流）",
                    "low": "低（谨慎、多解释、少玩笑）",
                }
                _tone_lines = [f"互动基调：{_tone['tone']}"]
                if _tone.get("trust"):
                    _tone_lines.append(f"信任度：{_trust_map.get(_tone['trust'], _tone['trust'])}")
                _tone_lines.append("💡 氛围明显变化时用 interaction_tone_update 更新")
                blocks.append("【当前互动氛围】\n" + "\n".join(_tone_lines))
        except Exception:
            pass


        # Block 1.5: 领域书（开关控制入口）
        try:
            from tools.domain_book_tools import get_active_pages_info, _load_book
            active_pages = get_active_pages_info()
            book = _load_book()
            all_pages = book.get("pages", {})
            active_ids = book.get("active_pages", [])
            if active_pages or all_pages:
                page_lines = []
                # 列出所有页面，标记激活状态（入口透明）
                for pid, pg in all_pages.items():
                    if pid == "default" and not pg.get("content", "").strip():
                        continue
                    status = "✅" if pid in active_ids else "⏹"
                    title = pg.get("title", pid)
                    page_lines.append(f"  {status} {title}（{pid}）")
                # 已激活页面注入完整内容（领域书已是浓缩版本，不再二次压缩/截断）
                # AI 只能选择开关页（book_turn_to），不能压缩内容
                if active_pages:
                    page_lines.append("")
                    page_lines.append(_vocab("book_active_label"))
                    for title, pid, content in active_pages:
                        if not content.strip():
                            continue
                        # 保留原文，包括换行——这是 AI 必须遵循的具体规则
                        page_lines.append(f"  [{title}]\n  {content.strip()}")
                page_lines.append("")
                page_lines.append(_vocab("book_hint"))
                blocks.append(_vocab("book_title") + "\n" + "\n".join(page_lines))
        except Exception as e:
            print(f"[domain_book] inject failed: {e}")

        # Block 2: 当前任务
        task_header = f'任务: {topic.get("title", "新任务")}\nID: {tid[:8]}\n工作区: {ws_short}'
        blocks.append(_vocab("task_title") + "\n" + task_header)

        # Block 2.05: 交互（一行提示，省上下文；需要时 discover_tools('interaction')）
        blocks.append(
            "━━━ 交互 ━━━\n"
            "遇到必须用户拍板的决策：discover_tools('interaction') 后调 ask_user 暂停等回答。能自己决定的不要问。"
        )

        # Block 2.1: 任务DAG锚点 — 当前话题关联的流程图状态
        try:
            from task.dag import DAG
            dag_task_id, dag_task = DAG.get_task_by_topic(tid)
            # If DB has no DAG, try restore from mission folder
            if not dag_task_id:
                restored_id, restored_dag = DAG.load_from_file(tid)
                if restored_id:
                    dag_task_id = restored_id
                    dag_task = restored_dag.get_task()
            if dag_task_id:
                dag = DAG(dag_task_id)
                # Auto-save snapshot to mission folder (keeps file in sync)
                try:
                    dag.save_to_file(tid)
                except Exception:
                    pass
                nodes = dag.get_nodes()
                status_counts = {}
                for n in nodes:
                    s = n["status"]
                    status_counts[s] = status_counts.get(s, 0) + 1
                dag_lines = [f"ID: {dag_task_id[:8]}", f"任务: {dag_task.get('user_request','')[:80]}"]
                dag_lines.append(f"文件: data/missions/{tid}/dag.json")
                dag_lines.append(f"节点: {len(nodes)} | {status_counts}")
                runnable = dag.get_runnable_nodes()
                if runnable:
                    dag_lines.append("可执行: " + ", ".join(f"{n['task'][:30]}({n['id'][:6]})" for n in runnable[:3]))
                dag_lines.append("入口: get_task_dag() 查看 / 点页面标题看可视化 / dag.json 永久备份")
                blocks.append(_vocab("dag_title") + "\n" + "\n".join(dag_lines))
        except Exception:
            pass

        # Block 2.2: 思维导图锚点 — 与 DAG 对齐的读通道（大眼每轮对话感知思维导图状态）
        # 设计原则：只告诉大眼"有思维导图 + 状态摘要 + 工具入口"，不暴露文件路径。
        # 大眼想看详情或改图，一律走 get_mindmap / add_mindmap_node 等工具，不绕路读文件。
        try:
            mm_path = os.path.join(ROOT_DIR, "data", "missions", tid, "mindmap.json")
            if os.path.isfile(mm_path):
                with open(mm_path, "r", encoding="utf-8", errors="replace") as f:
                    mm_data = json.load(f)
                mm_nodes = mm_data.get("nodes") or []
                mm_edges = mm_data.get("edges") or []
                if mm_nodes:
                    mm_lines = [f"【思维导图】 {mm_data.get('title','')[:40]} ({len(mm_nodes)} 节点 / {len(mm_edges)} 连线)"]
                    # 按类型分组计数
                    type_counts = {}
                    for n in mm_nodes:
                        nt = n.get("type", "idea")
                        type_counts[nt] = type_counts.get(nt, 0) + 1
                    mm_lines.append(f"类型分布: {type_counts}")
                    # 列出 decision / question 关键节点（最多 6 个，截 30 字）
                    key_nodes = [n for n in mm_nodes if n.get("type") in ("decision", "question")]
                    if key_nodes:
                        key_str = ", ".join(f"{n.get('text','')[:30]}({n.get('id','')[:6]})" for n in key_nodes[:6])
                        mm_lines.append(f"关键节点: {key_str}")
                    # 列出 task_ref 节点（与 DAG 互链的）
                    task_refs = [n for n in mm_nodes if n.get("type") == "task_ref"]
                    if task_refs:
                        mm_lines.append(f"DAG引用: {len(task_refs)} 个 task_ref 节点")
                    # 最近改动时间（可读格式，省去大眼转时间戳）
                    updated = mm_data.get("updated_at")
                    if updated:
                        import time as _t
                        mm_lines.append(f"最近改动: {_t.strftime('%m-%d %H:%M', _t.localtime(updated))}")
                    # 连线样式 + 布局（让大眼知道当前配置，不用读文件）
                    router = mm_data.get("router", "cubic")
                    layout = mm_data.get("layout", "tree-h")
                    mm_lines.append(f"连线样式: {router} / 布局: {layout}")
                    # 工具入口（不提文件路径，杜绝绕路读文件）
                    mm_lines.append("入口: get_mindmap() 看全图 / add_mindmap_node 加节点 / discover_tools('mindmap_advanced') 高级操作")
                    blocks.append("\n".join(mm_lines))
        except Exception:
            pass

        # Block 2.5: 心智锚点 — 任务自带挂件（AI 无感文件存在）
        try:
            anchor_path = os.path.join(ROOT_DIR, "data", "anchors", f"{tid[:8]}.md")
            if os.path.isfile(anchor_path):
                with open(anchor_path, "r", encoding="utf-8", errors="replace") as f:
                    anchor_text = f.read(800).strip()
                if anchor_text:
                    blocks.append(_vocab("anchor_title") + "\n" + anchor_text + "\n\n" + _vocab("anchor_update_hint"))
                else:
                    blocks.append(_vocab("anchor_title") + "（空）\n" + _vocab("anchor_empty_hint"))
            else:
                blocks.append(_vocab("anchor_title") + "（空）\n" + _vocab("anchor_empty_hint"))
        except Exception:
            pass

        # Block 2.6: 思维链热层 --- 任务切换自动装载
        try:
            from task.thought_chain import ThoughtChain
            active_tid = self.db.get_active_topic_id()
            if active_tid:
                tc = ThoughtChain(active_tid)
                if tc.step_count > 0:
                    hot = tc.format_hot_layer(n=5)
                    blocks.append(_vocab("thought_title") + "\n" + hot)
        except Exception:
            pass

        # Block 3: 现在时间（毫秒精度，每次对话刷新）
        import datetime
        now = datetime.datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        weekday = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
        blocks.append(f"{_vocab('time_title')} {time_str} {weekday}")

        # Block 4: 技能（skills/ 目录，AI 可自创）
        try:
            from skills_scanner import scan_skills
            skills = scan_skills()
            if skills:
                skill_lines = []
                for s in skills:
                    line = f"skills/{s['name']}.md — {s['description']}"
                    trigs = s.get("triggers") or []
                    if trigs:
                        line += f"（何时想起: {'；'.join(trigs)}）"
                    if s.get("status") == "trial":
                        line += "【试用中：匹配场景先按技能实测一次，验证有效后把 frontmatter 的 status 改为 active】"
                    skill_lines.append(line)
                skill_lines.append("")
                skill_lines.append("💡 用 discover_tools('memory_advanced') 获取技能管理工具(查看模板/创建技能)")
                skill_lines.append("   或用 write_file 直接写 skills/新技能.md（支持 YAML 元数据，status: trial 表示试用期）")
                blocks.append(_vocab("skills_title") + "\n" + "\n".join(skill_lines))
        except Exception:
            pass

        # Block 5a: 故事线（story 层常驻显示，最近 N 条 + 当前话题相关优先）
        # 故事是 AI 整理过的连续记忆，是"我是谁、我经历过什么"的自我认知背景
        # 必须每轮都在场，让 AI 有连续思维的感觉——不是"想起来了才显示"
        if store and _memory_enabled:
            try:
                import sqlite3 as _sqlite3
                _db_path = store.db_path
                _conn = _sqlite3.connect(_db_path, timeout=5)
                _conn.row_factory = _sqlite3.Row
                _conn.execute("PRAGMA busy_timeout=5000")
                # 取最近的故事：按时间线倒序，最近 5 条常驻显示
                # 不按当前 topic 过滤——故事不分任务主题（CMN P6 设计），
                # 任务关联只挂在思维链上做溯源；story 走 event_time 时间线
                _rows = _conn.execute(
                    "SELECT id, text, tags, topic_id, event_time, importance, authority_level, "
                    "created_at FROM memory_fragments "
                    "WHERE layer='story' AND dirty=1 "
                    "ORDER BY (event_time IS NULL), substr(event_time,1,10) DESC, created_at DESC LIMIT 5"
                ).fetchall()
                _conn.close()

                if _rows:
                    # 直接取最近 5 条（SQL 已按时间线倒序）
                    _selected = _rows


                    # 按 event_time 排序（最近在前）；无 event_time 的排最后，用 created_at 兜底
                    def _story_et_key(r):
                        et = r["event_time"] or ""
                        return et[:10]  # '2026-07-11 ~ 2026-07-15' 取起始日
                    _selected.sort(key=lambda r: (_story_et_key(r) or "0000-00-00"), reverse=True)

                    story_lines = []
                    for r in _selected:
                        auth = " ★" if r["authority_level"] >= 1 else ""
                        text = r["text"] or ""
                        # 紧凑时间线：取第一句做摘要，限 60 字
                        _first_sent = re.split(r"[。！？!?]", text)[0].strip() if text else ""
                        _sum = _first_sent[:60] + "…" if len(_first_sent) > 60 else _first_sent
                        _et = r["event_time"] or ""
                        _et_disp = _et.replace(" ~ ", "~") if _et else ""
                        story_lines.append(f"📖 {_et_disp} {_sum}{auth}")
                    if story_lines:
                        story_lines.append("")
                        story_lines.append(_vocab("story_tool_hint"))
                        blocks.append(_vocab("story_title") + "\n" + "\n".join(story_lines))
            except Exception as e:
                print(f"[memory] story block failed: {e}")

        # Block 5b: 记忆（embedding 召回 + 权重排序，5 条，回想入口）
        # 零散碎片按相关性触发——故事常驻，记忆按需召回
        if store and _memory_enabled:
            try:
                _msg_clean = message.strip().replace(" ", "")
                _skip_words = {"好的","好","行","ok","嗯","哦","对","是的","继续","可以",
                               "okay","yes","no","不对","没错","知道了","明白","了解",
                               "thanks","谢谢","谢了","cool","666","6","哈哈","hh"}
                _skip = _msg_clean in _skip_words or not message or len(_msg_clean) < 4
                if not _skip:
                    import time as _time
                    recall_ctx = message[:300]
                    all_fragments = store.recall(recall_ctx, top_k=15, threshold=0.35)
                    # 记忆 Block 只显示 core + knowledge，story 已在 Block 5a 常驻
                    non_stories = [f for f in all_fragments if (f.get("layer") or "core") != "story"]

                    if non_stories:
                        now_ts = _time.time()
                        def _score(f):
                            # 综合权重：weight × 时效衰减 × importance × 同话题加权 × 权威加成
                            weight = f.get("weight", 1.0)
                            importance = f.get("importance", 5.0)
                            same_topic = 1.3 if (f.get("topic_id") or "") == tid else 1.0
                            # 时效衰减：30天半衰期
                            ts_str = f.get("ts", "")
                            if ts_str and len(ts_str) >= 8:
                                try:
                                    from datetime import datetime
                                    ft = datetime.strptime(ts_str[:8], "%Y%m%d").timestamp()
                                    days = max(0, (now_ts - ft) / 86400)
                                    recency = 0.5 ** (days / 30)
                                except Exception:
                                    recency = 0.5
                            else:
                                recency = 0.5
                            authority_bonus = 1.5 if f.get("authority_level", 0) >= 1 else 1.0
                            return weight * recency * (importance / 5.0) * same_topic * authority_bonus
                        non_stories.sort(key=_score, reverse=True)
                        # 同 topic 最多 2 条，保证多样性
                        topic_count = {}
                        selected = []
                        for f in non_stories:
                            ftid = f.get("topic_id") or "_none_"
                            if topic_count.get(ftid, 0) >= 2:
                                continue
                            topic_count[ftid] = topic_count.get(ftid, 0) + 1
                            selected.append(f)
                            if len(selected) >= 5:
                                break
                        mem_lines = []
                        for f in selected:
                            ts = f.get("ts", "")
                            auth = " ★" if f.get("authority_level", 0) >= 1 else ""
                            decay = f.get("confidence_decay", 1.0)
                            decay_mark = " ⚠️" if decay < 0.5 else ""
                            # knowledge 层标记 📚（成体系知识，文本可能较长，截到 200 字）
                            layer = f.get("layer") or "core"
                            is_knowledge = layer == "knowledge"
                            layer_mark = " 📚" if is_knowledge else ""
                            text = f['text']
                            if is_knowledge and len(text) > 200:
                                text = text[:200] + "…"
                            mem_lines.append(f"[{ts[:8]} {ts[8:14]}]{layer_mark} {text}{auth}{decay_mark}")
                        if mem_lines:
                            mem_lines.append("")
                            mem_lines.append(_vocab("memory_tool_hint"))
                            mem_lines.append(_vocab("memory_write_hint"))
                            blocks.append(_vocab("memory_title") + "\n" + "\n".join(mem_lines))
            except Exception as e:
                print(f"[memory] recall failed: {e}")

        # Block 5c: 可能关联的对话记录（跨任务召回用户原话）
        # 对话向量索引 user_msg_vectors：用户消息入库时增量 embed，这里按当前消息向量召回
        if store and _memory_enabled:
            try:
                _msg_clean2 = message.strip().replace(" ", "")
                _skip2 = _msg_clean2 in _skip_words or not message or len(_msg_clean2) < 4
                if not _skip2:
                    from memory.msg_vectors import recall_with_context
                    _related = recall_with_context(message[:300], exclude_topic_id=tid, top_k=3)
                    if _related:
                        _dlg_lines = []
                        for _item in _related:
                            _tt = _item.get("topic_title") or (_item.get("topic_id") or "")[:8]
                            _tsv = _item.get("ts") or 0
                            _ts_str = time.strftime("%m-%d %H:%M", time.localtime(_tsv)) if _tsv else ""
                            _txt = (_item.get("text") or "").replace("\n", " ").strip()
                            if len(_txt) > 60:
                                _txt = _txt[:60] + "…"
                            _dlg_lines.append(f"[{_ts_str}][{_tt}] 你说：{_txt}")
                            # 带前后文（"念头"看到对话现场）
                            for _c in (_item.get("context") or [])[-2:]:
                                _ct = (_c.get("text") or "").replace("\n", " ").strip()
                                if not _ct:
                                    continue
                                if len(_ct) > 50:
                                    _ct = _ct[:50] + "…"
                                _cr = "我说" if _c.get("role") == "ai" else "你说"
                                _dlg_lines.append(f"   ↳ {_cr}：{_ct}")
                        if _dlg_lines:
                            _dlg_lines.append("")
                            _dlg_lines.append("💭 这些是历史对话里的用户原话，跨任务相关，附了当时的上下文。翻阅完整对话: read_topic_messages")
                            blocks.append("【关联对话】\n" + "\n".join(_dlg_lines))
            except Exception as e:
                print(f"[memory] related-dialog recall failed: {e}")

        # Block 6: 网络端口
        ips = get_ips(self.port)
        blocks.append(_vocab("network_title") + "\n" + "地址:\n  · " + "\n  · ".join(ips))

        # Block 7: 任务草稿纸 — 任务自带便签
        draft_path = os.path.join(ROOT_DIR, "data", "drafts", f"{tid[:8]}.md")
        if os.path.exists(draft_path):
            try:
                with open(draft_path, "r", encoding="utf-8") as f:
                    draft_content = f.read().strip()
                if draft_content:
                    blocks.append(_vocab("draft_title") + "\n" + draft_content + "\n\n" + _vocab("draft_continue_hint"))
                else:
                    blocks.append(_vocab("draft_title") + "（空）\n" + _vocab("draft_empty_hint"))
            except Exception:
                pass
        else:
            blocks.append(_vocab("draft_title") + "（空）\n" + _vocab("draft_empty_hint"))

        # 注：过程与答案的分离由框架层强制（agent loop 按有无 tool_call 分流到
        # intermediate / response），不再靠 system prompt 让模型自觉。

        # Block: 工具折叠引导
        blocks.append(
            "【工具使用】\n"
            "常驻工具: web_search/bash/read_file/edit_file/remember/current_topic 等 ~40 个，直接调用\n"
            "折叠分组: 先 discover_tools(group) 取 schema，再 execute_advanced_tool(name, args) 执行\n"
            "可用分组:\n"
            "  · task_management (任务管理)\n"
            "  · task_dag_advanced (DAG高级)\n"
            "  · memory_advanced (记忆高级/技能管理)\n"
            "  · mindmap_advanced (思维导图高级)\n"
            "  · domain_book (领域书页面CRUD)\n"
            "  · misc (图片/模板/热重载)"
        )

        # ── Attention focus level filter ──
        # 每个 block 标题映射到注意力等级（1-15），等级越高越重要。
        # AI 通过 set_focus_level(N) 工具收缩上下文，只保留 >= N 的 block。
        # 支持两种视角的标题（self_aware / human）。
        _BLOCK_LEVELS = {
            # Level 15: 永不删除（身份 + 任务）
            "【对我很重要的事】": 15,
            "【我现在在做什么】": 15,
            "【我在做的事】": 15,
            # Level 14: 核心框架（锚点 + DAG）
            "【心智锚点】": 14,
            "【我心里记着的】": 14,
            "【任务流程图】": 14,
            "【我的计划】": 14,
            # Level 13: 行动指南（工具）
            "【工具使用】": 13,
            # Level 12: 思维链
            "【我的思考】": 12,
            "【我的思绪】": 12,
            # Level 11: 能力边界
            "【我的技能】": 11,
            "【我会的】": 11,
            # Level 10: 系统状态
            "【我的系统状态】": 10,
            "【我的状态】": 10,
            # Level 9: 叙事记忆
            "【我的叙事记忆】": 9,
            "【我的故事】": 9,
            # Level 8: 记忆碎片
            "【相关记忆碎片】": 8,
            "【我想起的】": 8,
            # Level 7: 关联对话
            "【关联对话】": 7,
            # Level 6: 草稿
            "【临时缓存区】": 6,
            "【我的草稿本】": 6,
            # Level 5: 工具书
            "【我的工具书】": 5,
            # Level 4: 思维导图
            "【思维导图】": 4,
            # Level 3: 时间
            "【系统时间】": 3,
            "【现在】": 3,
        }
        def _extract_title(block: str) -> str:
            """从 block 文本提取【...】标题。"""
            import re as _re
            m = _re.match(r"【([^】]+)】", block)
            return f"【{m.group(1)}】" if m else ""
        def _apply_focus_filter(blocks: list, focus_level: int, custom_blocks: list = None) -> tuple:
            """按注意力等级过滤 block。返回 (保留的block, 移除的标题列表)。"""
            if focus_level >= 15 and not custom_blocks:
                return blocks, []
            kept, removed = [], []
            for b in blocks:
                title = _extract_title(b)
                if custom_blocks:
                    # AI 自定义的 block 列表
                    if title in custom_blocks:
                        kept.append(b)
                    else:
                        removed.append(title)
                else:
                    level = _BLOCK_LEVELS.get(title, 5)
                    if level >= focus_level:
                        kept.append(b)
                    else:
                        removed.append(title)
            return kept, removed

        # ─ 读取注意力等级设置 ──
        _focus_level = int(self.db.get_topic_meta(tid, "focus_level") or "15")
        _focus_custom_json = self.db.get_topic_meta(tid, "focus_custom_blocks") or "[]"
        try:
            _focus_custom = json.loads(_focus_custom_json)
        except Exception:
            _focus_custom = []
        # ── 自动注意力调节（主动）：任务复杂度评估 ──
        # AI 显式 set_focus_level（focus_explicit=1）优先，自动调节不覆盖
        _focus_auto_note = ""
        _focus_explicit = self.db.get_topic_meta(tid, "focus_explicit") or "0"
        if _focus_explicit != "1" and _focus_level >= 15 and not _focus_custom:
            _auto_focus = _estimate_task_complexity(tid)
            if _auto_focus:
                _focus_level = _auto_focus
                _focus_auto_note = f"（自动调节：任务复杂 → 等级 {_auto_focus}，可 set_focus_level 手动覆盖）"

        # ── Assemble system message ──
        # 结构化分组：6 个逻辑组 + section header + 强分隔线
        # 保留 U 型注意力优化：身份规则+当前任务放头部（强注意力），
        # 工具/记忆/环境放中间（弱注意力），行动依据(DAG+思维链)放尾部（强注意力）。
        _BLOCK_GROUPS = [
            (1, "━━━ 身份与规则 ━━━", [
                ("【对我很重要的事】", 1),
                ("【我在做的事】", 2), ("【我心里记着的】", 2), ("【心智锚点】", 2),
            ]),
            (2, "━━━ 当前任务 ━━━", [
                ("【我现在在做什么】", 1),
                ("【思维导图】", 2),
            ]),
            (3, "━━━ 工具与知识 ━━━", [
                ("【工具使用】", 1),
                ("【我的工具书】", 2),
                ("【我会的】", 3), ("【我的技能】", 3),
            ]),
            (4, "━━━ 记忆与历史 ━━━", [
                ("【我的故事】", 1), ("【我的叙事记忆】", 1),
                ("【我想起的】", 2), ("【相关记忆碎片】", 2),
                ("【关联对话】", 3),
                ("【我的草稿本】", 4), ("【临时缓存区】", 4),
            ]),
            (5, "━━━ 系统环境 ━━━", [
                ("【现在】", 1), ("【系统时间】", 1),
                ("【我的状态】", 2), ("【我的系统状态】", 2),
            ]),
            (6, "━━━ 行动依据 ━━━", [
                ("【我的计划】", 1), ("【任务流程图】", 1),
                ("【我的思绪】", 2), ("【我的思考】", 2),
            ]),
        ]
        def _block_group_key(b):
            """返回 (section_order, block_order)，未知 block 排到最后组最后位"""
            for sec_order, sec_header, block_list in _BLOCK_GROUPS:
                for prefix, blk_order in block_list:
                    if b.startswith(prefix):
                        return (sec_order, blk_order)
            return (99, 99)
        def _compose_system(blocks, focus_level, custom_blocks, auto_note=""):
            """按注意力等级过滤 block 并组装 system prompt。返回 (system_msg, removed_titles)。

            用 sorted 拷贝，不修改外层 blocks —— 上下文压力被动降级时可基于原始 blocks 重新组装。
            """
            blocks = sorted(blocks, key=_block_group_key)
            removed = []
            if focus_level < 15 or custom_blocks:
                blocks, removed = _apply_focus_filter(blocks, focus_level, custom_blocks)
                if removed:
                    _memo = self.db.get_topic_meta(tid, "focus_memo") or ""
                    _restore_at = self.db.get_topic_meta(tid, "focus_restore_at") or "-1"
                    _restore_hint = f"，{_restore_at} 轮后自动恢复" if _restore_at != "-1" else "，需手动恢复"
                    blocks.append(
                        f"━━━ 专注模式 ━━━\n"
                        f"等级 {focus_level}/15{_restore_hint} {auto_note}\n"
                        f"备忘：{_memo}\n"
                        f"已移除：{', '.join(removed)}\n"
                        f"💡 记得完成任务后 set_focus_level(15) 恢复"
                    )
            _BLOCK_SEP = "\n\n" + "─" * 15 + "\n\n"      # 组内 block 间
            _SECTION_SEP = "\n\n" + "═" * 15 + "\n\n"     # 组间
            from itertools import groupby
            _parts = []
            for sec_order, group_blocks in groupby(blocks, key=lambda b: _block_group_key(b)[0]):
                sec_header = next((sh for so, sh, _ in _BLOCK_GROUPS if so == sec_order), None)
                group_list = list(group_blocks)
                if not group_list or not sec_header:
                    continue
                section_text = sec_header + "\n" + group_list[0]
                for b in group_list[1:]:
                    section_text += _BLOCK_SEP + b
                _parts.append(section_text)
            return _SECTION_SEP.join(_parts), removed

        system_msg, _removed_titles = _compose_system(blocks, _focus_level, _focus_custom, _focus_auto_note)
        # ── 规则引擎：事件触发（用户发消息瞬间实时检查，不落库、不推APP）──
        try:
            sys.path.insert(0, os.path.join(ROOT_DIR, "rules_engine"))
            import rule_worker
            _hits = rule_worker.check_rules_for_topic(
                f"http://127.0.0.1:{self.port}", tid)
            if _hits:
                _rule_block = ("━━━ 规则提醒（这是系统最重要的规则，如果你看到请在符合条件后做出处理；处理完本提醒自动消失）━━━\n"
                               + "\n\n".join(_hits))
                # 置顶：放在 system_msg 最前，确保一眼看到，不被其他内容分散注意力
                system_msg = _rule_block + "\n\n" + "═" * 15 + "\n\n" + system_msg
        except Exception as _e:
            print(f"[rules] 事件检查失败: {_e}")
        # Build history — user + AI only, no tool messages
        # 取数用 skip_process=False：process 消息在循环内占位计数（与 fold 侧 anchor 语义一致），
        # 但不加入 AI 上下文（省 token）；这样 compression_tree 的 anchor 才能与折叠时对齐
        history_raw = self.db.get_messages(tid, limit=500, skip_process=False)
        # Detect limit truncation — messages older than the fetch window are lost
        total_db_msgs = self.db.message_count(tid)
        limit_truncated = total_db_msgs > len(history_raw)
        # 全局 anchor 基准：compression_tree 的 anchor 是"话题内非tool消息的全局序号"
        # （从话题第一条非tool消息数起）。组装窗口是最近500条，窗口第一条非tool消息的
        # 全局序号 = 话题总非tool数 - 窗口内非tool数。若不偏移，窗口起点漂移会导致
        # 整个窗口误命中旧折叠区间（复现：上下文只剩摘要+当前消息）。
        _loop_msgs = history_raw[:-1]  # 最后一条是当前用户消息，单独在 _build_messages 处理
        # base 用全窗口非tool数（含最后一条），与 fold 侧 get_messages 窗口一致；
        # 否则组装侧 base 会比 fold 侧大1，导致节点区间整体偏移1条。
        _win_non_tool = sum(1 for m in history_raw if m.get("role") != "tool")
        try:
            _nt_row = self.db._fetchone(
                "SELECT COUNT(*) AS c FROM messages WHERE topic_id=? AND role!='tool'",
                (tid,))
            _total_non_tool = _nt_row["c"] if _nt_row else 0
        except Exception:
            _total_non_tool = 0
        _anchor_base = max(_total_non_tool - _win_non_tool, 0)
        # 加载整理树节点：AI 视角把已整理区间动态替换为摘要（messages 表原文不动，前端显示完整对话）
        try:
            from tools.memory_tools import _ensure_ct_table
            _ensure_ct_table(self.db)
            _ct_rows = self.db._fetchall(
                "SELECT id, anchor_start, anchor_end, depth, summary_text "
                "FROM compression_tree WHERE topic_id = ? ORDER BY anchor_start",
                (tid,))
            ct_nodes = [dict(r) for r in _ct_rows] if _ct_rows else []
        except Exception as _e:
            print(f"[context] 加载整理树失败: {_e}")
            ct_nodes = []
        history_msgs = []
        anchor = _anchor_base  # 非 tool 消息计数（全局序号，与 fold 侧一致）
        for m in _loop_msgs:
            role = m.get("role", "user")
            args = m.get("args") or {}
            if role == "tool":
                continue  # 不计 anchor（fold 侧同样跳过 tool）
            if role == "ai" and args.get("process"):
                anchor += 1  # 占位计数，与 fold 侧语义对齐；不加入上下文
                continue
            text = m["text"]
            msg_id = m.get("id")
            # 命中整理区间？取覆盖当前 anchor 的最内层节点（嵌套整理时子节点优先）
            ct_node = None
            for _n in ct_nodes:
                if _n["anchor_start"] <= anchor <= _n["anchor_end"]:
                    if ct_node is None or _n["anchor_start"] > ct_node["anchor_start"]:
                        ct_node = _n
            if ct_node is not None:
                if anchor == ct_node["anchor_start"]:
                    # 区间首条：注入摘要视图（虚拟消息，不落库）
                    _summary = ct_node.get("summary_text") or "[已整理]"
                    history_msgs.append({
                        "role": "assistant",
                        "content": _summary,
                        "_db_id": None,
                        "_ct_node": ct_node["id"],
                    })
                anchor += 1
                continue  # 区间内其余原文跳过（已被摘要覆盖，不重复占用预算）
            if args.get("hidden"):
                # hidden 消息：折叠摘要由 compression_tree 覆盖（上面已处理）。
                # 此处只兜底 "hidden 但无 ct 记录" 的孤儿：
                #   text 是 [已整理]/[已压缩] 摘要标记 → 保留供 AI 查看
                #   否则（原文）→ 跳过不进上下文，需要时 expand_compressed 按需展开
                if text and (text.startswith("[已整理") or text.startswith("[已压缩")):
                    if role == "ai":
                        history_msgs.append({"role": "assistant", "content": text, "_db_id": msg_id})
                    elif role == "user":
                        history_msgs.append({"role": "user", "content": text, "_db_id": msg_id})
                anchor += 1
                continue
            if role == "ai":
                if not (text or "").strip():
                    anchor += 1
                    continue
                history_msgs.append({"role": "assistant", "content": text, "_db_id": msg_id})
            elif role == "user":
                history_msgs.append({"role": "user", "content": text, "_db_id": msg_id})
            anchor += 1
        # Set workspace context for file/bash/image tools (thread-local)
        tools.file_tools.set_workspace(ws)
        tools.bash.set_workspace(ws)
        tools.image_gen_tool.set_workspace(ws)
        # 用户授权开关：是否允许 AI 跨出工作区访问/写入
        _allow_outside = (self.db.get_topic_meta(tid, "allow_outside") == "1")
        tools.file_tools.set_allow_outside(_allow_outside)
        tools.bash.set_allow_outside(_allow_outside)
        # Cancel event — allows /api/stop-thinking to interrupt streaming
        cancel_evt = threading.Event()
        with _cancel_lock:
            _cancel_events[tid] = cancel_evt
        full_thinking = ""
        full_response = ""
        full_intermediate = ""  # 工具轮的动作叙述（"好嘞去抓官网…"），换行累积；最终答案不进这里
        total_input_tokens = 0
        total_output_tokens = 0
        turn = 0
        msgs = _build_messages(system_msg=system_msg, history=history_msgs, new_msg=message)
        current_msg = msgs[-1]
        old_msgs = _compress_context(msgs[:-1], tid, store, self.db)
        msgs = old_msgs + [current_msg]
        llm_config = get_model_config()
        # ── 看图：图片附件 → 多模态 content + 切 vision 模型 ──
        if _image_abs_paths:
            _mm = build_multimodal_content(message, _image_abs_paths)
            if _mm is not None:
                current_msg["content"] = _mm
                # 当前模型是视觉模型（如 glm-5.3-flash）直接用，否则回退 deepseek vision
                if not is_vision_model(llm_config.model):
                    llm_config = llm_config.clone_with(model="deepseek-v4-flash-vision-exp")
        else:
            # 非 vision 轮次：剥离历史残留的图片 content，避免 v4-pro 报错
            msgs = strip_image_content(msgs, llm_config.model)
        _inject_context_hints(msgs, tid, limit_truncated=limit_truncated)
        # 缓存调试快照：包含 messages + tools + token 拆解
        # 之前只存 msgs，导致工具 schema（可达 13k+ tokens）不可见，调试时对不上 token 数
        _active_tools = _get_enabled_tool_defs(tid)
        _tools_tokens = _estimate_tools_tokens(_active_tools)
        _snapshot = {
            "model": llm_config.model,
            "provider": llm_config.provider,
            "messages": msgs,
            "tools": _active_tools,
            "tool_count": len(_active_tools),
            "estimate": {
                "messages_tokens": _estimate_tokens(msgs),
                "tools_tokens": _tools_tokens,
                "total_estimated": _estimate_tokens(msgs) + _tools_tokens,
            },
        }
        _last_request_body[tid] = json.dumps(_snapshot, ensure_ascii=False, indent=2)
        global _last_hard_tokens
        # 硬性内容实测 = 系统提示 + 工具 schema（每请求刷新，供最小值计算）
        _last_hard_tokens = _estimate_tokens([msgs[0]]) + _tools_tokens
        remaining = TURN_BUDGET
        # ─ 专注模式自动恢复检测 ──
        _focus_restore_at = self.db.get_topic_meta(tid, "focus_restore_at") or "-1"
        _focus_iterations = 0  # agent loop 迭代计数（每轮工具调用后递增）
        if _focus_restore_at != "-1" and _focus_iterations >= int(_focus_restore_at):
            self.db.set_topic_meta(tid, "focus_level", "15")
            self.db.set_topic_meta(tid, "focus_restore_at", "-1")
            self.db.set_topic_meta(tid, "focus_memo", "")
            self.db.set_topic_meta(tid, "focus_custom_blocks", "[]")
            self.db.set_topic_meta(tid, "focus_explicit", "0")
            print(f"[agent] focus auto-restore at iteration 0")
        print(f"[agent] task started, budget {remaining} turns")
        set_working(tid, "thinking", turn=0, remaining=remaining)
        _budget_warned = False  # 每个预算周期只提醒一次
        _budget_exhausted = False
        _auto_downgraded = False  # 上下文压力被动降级标记（每任务最多降一次）
        _turn_retries = 0  # 连接层瞬时故障的轮级重试计数
        _dangling_retries = 0  # 防悬空检测：每请求最多强制续跑 2 次
        while remaining > 0:
            has_tool_calls = False
            turn_text = ""
            turn_tool_calls = []
            turn_error = None
            turn_usage = {}
            turn_finish_reason = "stop"  # 每轮重置，done 事件里刷新

            # ── 剩余10轮时注入一次系统提醒 ──
            _budget_hint_msg = None
            if remaining <= 10 and not _budget_warned:
                _budget_warned = True
                _budget_hint_msg = (
                    f"[系统提示] 你已连续工作较长时间，当前剩余 {remaining} 轮。"
                    f"请自查：是否在有效推进任务？如果任务未完成且方向正确，"
                    f"调用 continue_task 继续（系统会自动续上轮次）；"
                    f"如果已完成，直接给出最终回答。"
                )
                msgs.append({"role": "user", "content": _budget_hint_msg})

            for event in chat_stream_cached(llm_config, msgs, tools=_get_enabled_tool_defs(tid), cancel_event=cancel_evt, verify_ssl=_VERIFY_SSL):
                et = event["type"]
                if et == "thinking_delta":
                    full_thinking += event["delta"]
                    set_working(tid, "thinking", thinking=full_thinking, intermediate=full_intermediate, response=turn_text, turn=turn+1, remaining=remaining)
                elif et == "text_delta":
                    # turn_text = 当前轮模型说的话（工具轮=动作叙述，最终轮=答案）。
                    # 不直接累加进 full_response——是否进最终答案要等 turn 结束看有无 tool_call。
                    turn_text += event["delta"]
                    set_working(tid, "responding", thinking=full_thinking, intermediate=full_intermediate, response=turn_text, turn=turn+1, remaining=remaining)
                elif et == "tool_call":
                    has_tool_calls = True
                    turn_tool_calls.append(event)
                    _record_tool_usage(tid, event.get("tool_name"))
                elif et == "done":
                    turn_finish_reason = event.get("finish_reason") or "stop"
                    usage = event.get("usage", {})
                    if usage:
                        turn_usage = {
                            "input": usage.get("prompt_tokens", 0),
                            "output": usage.get("completion_tokens", 0),
                        }
                        total_input_tokens += turn_usage["input"]
                        total_output_tokens += turn_usage["output"]
                        # 缓存命中 token 数（DeepSeek 在 prompt_tokens_details.cached_tokens）
                        _cache_hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                        _accumulate_cost(tid, turn_usage["input"], turn_usage["output"],
                                         cache_hit=_cache_hit, model=get_model_config().model)
                        global _last_request_tokens
                        _last_request_tokens = turn_usage["input"]
                        if turn_usage["input"] > _round_peak_tokens:
                            _round_peak_tokens = turn_usage["input"]  # 大轮内峰值实时刷新
                        _recent_input_tokens.append(turn_usage["input"])
                        try:
                            import attention_metrics
                            attention_metrics.set_live_usage(turn_usage["input"], list(_recent_input_tokens))
                            attention_metrics.invalidate_cache()  # 实时数据变了，下次查询重算
                        except Exception:
                            pass
                elif et == "error":
                    turn_error = event.get("error", "unknown")

            # If LLM returned an error with no text, abort
            # 但 cancel 导致的 error 不算真错误——走 cancel 处理流程
            if turn_error and not turn_text.strip() and not turn_tool_calls:
                if cancel_evt.is_set():
                    print(f"[agent] cancelled by user (during stream, no output yet)")
                    break  # 跳出 while，走 3312 的 cancel 处理
                # 连接层瞬时故障（网关强断等）：本轮无任何输出，整轮重试安全，
                # 避免单次断连直接杀死整个任务循环（表现为"说到一半断了"）
                if _turn_retries < 2 and is_transient_conn_error(turn_error):
                    _turn_retries += 1
                    print(f"[agent] transient LLM error (retry {_turn_retries}/2), retry in {_turn_retries * 2}s: {turn_error}")
                    time.sleep(_turn_retries * 2)
                    if _budget_hint_msg and msgs and msgs[-1].get("content") == _budget_hint_msg:
                        msgs.pop()  # 重试前移除本轮预算提示，保持 msgs 干净
                    continue
                raise Exception(f"LLM API error: {turn_error}")
            else:
                _turn_retries = 0  # 本轮正常产出，重置重试计数
            # 移除本轮注入的系统预算提示（保持 msgs 干净供下一轮）
            if _budget_hint_msg and msgs and msgs[-1].get("content") == _budget_hint_msg:
                msgs.pop()
            # Check cancel between turns
            if cancel_evt.is_set():
                print(f"[agent] cancelled by user")
                # 保留当前轮未完成的文本（流式中断时 turn_text 里有部分输出）
                if turn_text.strip() and not full_response:
                    full_intermediate = _append_intermediate(full_intermediate, turn_text)
                break
            # ── 插话注入：运行中收到的新用户消息插入上下文，同一循环继续思考 ──
            # 最终轮边界：模型正要收尾时被打断 → 把已说内容记为 assistant，
            # 插话作为 user 追加，continue 让模型带着新输入继续想
            _injs = _drain_injections(tid)
            if _injs and not has_tool_calls:
                if turn_text.strip():
                    msgs.append({"role": "assistant", "content": turn_text})
                for _inj in _injs:
                    msgs.append({"role": "user", "content": _inj})
                print(f"[agent] injected {len(_injs)} user message(s) at final-turn boundary, continuing")
                set_working(tid, "thinking", thinking=full_thinking,
                            intermediate=full_intermediate, response="",
                            turn=turn+1, remaining=remaining)
                continue
            if not has_tool_calls:
                # 最终轮（无 tool_call）：turn_text 就是最终答案
                # ── 防悬空检测：预告但没调用工具 / 生成被截断 → 强制续跑 ──
                _dangling = (
                    turn_finish_reason == "length"
                    or _is_hanging_announcement(turn_text)
                )
                if _dangling and _dangling_retries < 2 and turn_text.strip():
                    _dangling_retries += 1
                    print(f"[agent] dangling announcement (finish={turn_finish_reason}, retry {_dangling_retries}/2), forcing continuation")
                    msgs.append({"role": "assistant", "content": turn_text})
                    msgs.append({"role": "user", "content": "[系统强制] 你上一轮以预告结尾但没有调用任何工具。请立即执行你预告的动作（直接调用工具），不要重复说明、不要重新确认现状。直接做。"})
                    continue
                if turn_text.strip():
                    full_response = turn_text  # 最终答案（覆盖式，只此一轮）
                    msgs.append({"role": "assistant", "content": turn_text})
                break
            # 工具轮：turn_text 是动作叙述（"好嘞去抓官网…"），归 intermediate，不进最终回复
            if turn_text.strip():
                full_intermediate = _append_intermediate(full_intermediate, turn_text)
            # Tool calls were made — execute and continue normally
            assistant_msg = {"role": "assistant", "content": turn_text or None}
            tc_list = []
            for tc in turn_tool_calls:
                tc_list.append({
                    "id": tc["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": tc["tool_name"],
                        "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                    }
                })
            if tc_list:
                assistant_msg["tool_calls"] = tc_list
            msgs.append(assistant_msg)

            # ── Persist AI message BEFORE tool execution ──
            # Tools like organize_context read from DB; messages must be there first
            # args.process=True 标记此条为"工具轮动作叙述"（非最终回复），
            # 加载 AI 上下文和前端历史时通过 skip_process 过滤掉，避免过程碎碎念挤占预算。
            # AI 主动翻阅可用 read_topic_messages 直接 SQL 查到，过程信息折叠但不丢。
            ai_msg_id = self.db.add_message(tid, "ai", turn_text,
                                            args={"tool_calls": tc_list, "turn": turn, "process": True},
                                            thinking=full_thinking, ts=time.time())
            assistant_msg["_db_id"] = ai_msg_id  # for in-memory update after tool result
            # Execute tools and append results
            print(f"[agent] turn {turn+1}: {[tc['tool_name'] for tc in turn_tool_calls]}")
            set_working(tid, "executing", thinking=full_thinking, intermediate=full_intermediate, response="", turn=turn+1, remaining=remaining)

            # Check cancel before tool execution
            if cancel_evt.is_set():
                break

            tool_results = []  # (tc, result_json)
            # ── Streaming output for bash tool: accum chunks into set_working(intermediate) ──
            _tool_stream_lines = []     # list of (stream_type, chunk) pending flush
            _last_push_ts = [0.0]       # list for mutability in closure; throttled flush ~200ms
            _FLUSH_MS = 200

            def _flush_stream_output(force=False):
                """Flush pending chunks into set_working intermediate.
                Throttled to ~FLUSH_MS unless force=True.
                """
                nonlocal full_intermediate
                if not _tool_stream_lines:
                    return
                now = time.time() * 1000
                if not force and (now - _last_push_ts[0]) < _FLUSH_MS:
                    return
                tail_chunks = []
                for st, ch in _tool_stream_lines:
                    clean = ch.replace('\0', '')
                    if clean.endswith('\n'):
                        clean = clean[:-1]
                    if clean:
                        tail_chunks.append(clean)
                _tool_stream_lines.clear()
                if not tail_chunks:
                    _last_push_ts[0] = now
                    return
                full_intermediate = _append_intermediate(full_intermediate, "\n".join(tail_chunks))
                set_working(tid, "executing", thinking=full_thinking,
                            intermediate=full_intermediate, response="",
                            turn=turn+1, remaining=remaining)
                _last_push_ts[0] = now

            def _on_tool_output(tool_id, stream_type, chunk):
                """Bash runtime callback: fire on each line/block emitted.
                begin/end flush immediately (rare events); stdout/stderr accumulate.
                """
                if stream_type in ('begin', 'end'):
                    _tool_stream_lines.append((stream_type, chunk))
                    _flush_stream_output(force=True)
                    return
                _tool_stream_lines.append((stream_type, chunk))
                _flush_stream_output(force=False)

            for tc in turn_tool_calls:
                if cancel_evt.is_set():
                    break
                args = dict(tc["arguments"])
                if tc["tool_name"] in ("remember", "trace_memory",
                                       "edit_memory", "forget", "reinforce",
                                       "build_self_narrative"):
                    args.setdefault("topic_id", tid)
                if tc["tool_name"] == "remember":
                    if "parent_id" not in args:
                        if store:
                            roots = store.get_task_roots(tid, limit=1)
                            if roots:
                                args["parent_id"] = roots[0]["id"]
                            else:
                                args["parent_id"] = store.get_latest_fragment_id(tid)
                # Set runtime context for bash (thread-local, schema unchanged)
                if tc["tool_name"] == "bash":
                    tools.bash.set_runtime_context(on_output=_on_tool_output,
                                                   tool_id=tc["tool_call_id"])
                result = execute_tool(tc["tool_name"], args)
                # Clear context and flush remainder
                if tc["tool_name"] == "bash":
                    tools.bash.set_runtime_context(None, None)
                    _flush_stream_output(force=True)

                # ── continue_task: extend budget, keep running ──
                if tc["tool_name"] == "continue_task":
                    remaining += TURN_BUDGET
                    _budget_warned = False  # 新预算周期，允许再次提醒
                    print(f"[agent] continue_task called, budget extended to {remaining}")
                # ── fold_message: replace message in msgs immediately ──
                if tc["tool_name"] == "fold_message" and isinstance(result, dict) and result.get("folded"):
                    folded_db_id = result.get("db_id")
                    folded_text = result.get("text", "")
                    for m in msgs:
                        if m.get("_db_id") == folded_db_id:
                            m["content"] = folded_text
                            break
                    result_json = json.dumps({"folded": True, "anchor": result.get("anchor", ""),
                                              "msg": f"已整理，原文{len(result.get('summary',''))}字的摘要已替换到对话中。"},
                                             ensure_ascii=False)
                # ── organize_context: replace folded messages in msgs immediately ──
                # ── organize_context: remove folded messages from msgs ──
                elif tc["tool_name"] in ("organize_context", "compress_context") and isinstance(result, dict) and result.get("compressed"):
                    folded_ids = {f.get("db_id") for f in result.get("folded", []) if f.get("db_id")}
                    # Remove folded messages from in-memory context
                    msgs[:] = [m for m in msgs if m.get("_db_id") not in folded_ids]
                    result_json = json.dumps({
                        "compressed": True,
                        "summary_anchor": result.get("summary_anchor", 0),
                        "summary": result.get("summary_text", ""),
                        "folded_count": result.get("folded_count", 0),
                    }, ensure_ascii=False)
                else:
                    result_json = json.dumps(result, ensure_ascii=False)

                # Prune large tool outputs to avoid context pollution
                if len(result_json) > 4000:
                    result_json = result_json[:1000] + f"\n…[截断 {len(result_json)-4000} 字符，工具输出过长已修剪]…\n" + result_json[-500:]
                msgs.append({"role": "tool", "tool_call_id": tc["tool_call_id"],
                             "content": result_json})
                tool_results.append((tc, result_json))
                loop_detector.record_call(tid, tc["tool_name"], args)

            # ── 打转检测：窗口内探索类占比高且零落地类 → 注入提醒 ──
            spin_msg = loop_detector.check_spinning(tid)
            if spin_msg:
                msgs.append({"role": "user", "content": spin_msg})
                print(f"[loop-detector] 打转提醒已注入: {spin_msg[:40]}")

            # Persist tool results
            for tc, result_json in tool_results:
                self.db.add_message(tid, "tool", result_json,
                                    args={"tool_call_id": tc["tool_call_id"],
                                          "name": tc["tool_name"],
                                          "turn": turn},
                                    ts=time.time())

            # ── 插话注入（工具轮边界）：工具结果落库后、下一轮 LLM 调用前，
            # 把运行中收到的新用户消息插入上下文，模型下一轮即可看到并纳入思考 ──
            _injs = _drain_injections(tid)
            if _injs:
                for _inj in _injs:
                    msgs.append({"role": "user", "content": _inj})
                print(f"[agent] injected {len(_injs)} user message(s) after tool turn, continuing")

            # ── Context wall: trim if over budget (dialogue-granular) ──
            # 预算必须计入 tools schema token，否则工具+消息总和可能超限
            # （方案D：之前只算 messages，导致 tools 13k+ tokens 不计入预算）
            loop_msgs_tokens = _estimate_tokens(msgs)
            loop_tools_tokens = _estimate_tools_tokens(_get_enabled_tool_defs(tid))
            loop_total_tokens = loop_msgs_tokens + loop_tools_tokens
            eff_limit = _effective_limit()
            if loop_total_tokens >= eff_limit:
                print(f"[context-wall] 循环触发: msgs={loop_msgs_tokens} + tools={loop_tools_tokens} "
                      f"= {loop_total_tokens} tokens >= {eff_limit} 有效上限({TOKEN_LIMIT}*{ESTIMATE_SAFETY})")
                # 第一优先：自动降级 system prompt（比裁对话消息更优——保留对话完整性）
                # 被动降级仅本次组装生效（_focus_level 是局部变量，不写回 DB，下条消息自动恢复）
                if not _auto_downgraded and _focus_level > 8:
                    _focus_level -= 2
                    _new_system, _removed2 = _compose_system(blocks, _focus_level, _focus_custom, "（上下文压力自动降级）")
                    if _removed2:
                        msgs[0] = {"role": "system", "content": _new_system}
                        _auto_downgraded = True
                        print(f"[context-wall] 自动降级 system → focus {_focus_level}, 移除: {_removed2}")
                        continue  # 重新估算
                    _focus_level += 2  # 降级无效（无 block 可裁），恢复原等级
                # 第二优先：给 tools 留出预算，只对 messages 截断
                msgs_budget = max(eff_limit - loop_tools_tokens, 5000)
                msgs, trimmed = _trim_context_to_budget(
                    msgs, msgs_budget, store=store, topic_id=tid
                )
                if trimmed:
                    print(f"[agent] context wall trimmed {trimmed} messages, "
                          f"remaining {len(msgs)} msgs, "
                          f"~{_estimate_tokens(msgs)} msgs tokens + {loop_tools_tokens} tools tokens")

            # ── 专注模式自动恢复检测（每轮迭代后检查）──
            _focus_iterations += 1
            if _focus_restore_at != "-1" and _focus_iterations >= int(_focus_restore_at):
                self.db.set_topic_meta(tid, "focus_level", "15")
                self.db.set_topic_meta(tid, "focus_restore_at", "-1")
                self.db.set_topic_meta(tid, "focus_memo", "")
                self.db.set_topic_meta(tid, "focus_custom_blocks", "[]")
                self.db.set_topic_meta(tid, "focus_explicit", "0")
                print(f"[agent] focus auto-restore at iteration {_focus_iterations}")
                _focus_restore_at = "-1"

            # ── 预算递减：每个工具轮消耗 1 轮 ──
            remaining -= 1
            turn += 1

        # ── 预算耗尽（AI 未调 continue_task 也未给最终答案）──
        if remaining <= 0 and not full_response and not cancel_evt.is_set():
            _budget_exhausted = True
            print(f"[agent] budget exhausted after {turn} turns")

        # Check if cancelled
        if cancel_evt.is_set():
            with _cancel_lock:
                _cancel_events.pop(tid, None)
            with _inject_lock:
                _inject_queues.pop(tid, None)
            # 组装回复内容：优先用 full_response（最终轮已有内容），
            # 其次用 full_intermediate（工具轮过程叙述），让用户看到 AI 跑到的位置。
            # 附上 [用户已停止] 标记落库，AI 下次加载历史时知道用户中断过。
            cancel_marker = "\n\n[用户已停止]"
            if full_response:
                reply = full_response.rstrip() + cancel_marker
            elif full_intermediate:
                # 只落简短状态提示，不落全量过程叙述（可能几十万字符，进上下文会炸工作记忆）
                reply = "（过程已中断，中间输出见前端 intermediate 面板。）" + cancel_marker
            else:
                reply = "（已中止）" + cancel_marker
            self.db.add_message(tid, "ai", reply,
                                thinking=full_thinking, ts=time.time())
            self.db.update_topic(tid, updated_at=time.time())
            clear_working(tid)
            self._json(200, {
                "response": reply,
                "thinking": full_thinking,
                "intermediate": full_intermediate,
                "topic_id": tid,
                "status": "cancelled",
                "usage": {"input": total_input_tokens, "output": total_output_tokens},
            })
            return

        # ── 预算耗尽且无最终答案：用中间过程兜底，不发空回复 ──
        if _budget_exhausted and not full_response:
            _exhaust_note = "\n\n[系统提示] 本轮工作轮次已用完，任务未结束。发送任意消息可让我继续。"
            # 只落简短提示，不落全量过程叙述（full_intermediate 可能几十万字符，进上下文会炸工作记忆）
            full_response = "（本轮工作轮次已用完，过程叙述见前端 intermediate 面板。）" + _exhaust_note

        print(f"[agent] done after {turn+1} turns")
        # 先释放 cancel 事件：此后到达的新消息会走正常流程起新循环
        # （插话消息已落库，新循环历史自然包含），不会被误判为"运行中"
        with _cancel_lock:
            _cancel_events.pop(tid, None)
        # 收尾竞态：循环刚结束的瞬间到达的插话，本轮已来不及纳入，
        # 在最终回复里明示已收到（消息已落库，下轮历史自然包含）
        _tail_injs = _drain_injections(tid)
        if _tail_injs and full_response:
            full_response += "\n\n（你刚发来的补充我已收到，会结合它继续。）"
        if full_response:
            self.db.add_message(tid, "ai", full_response,
                                thinking=full_thinking, ts=time.time())
        self.db.update_topic(tid, updated_at=time.time())
        clear_working(tid)
        with _inject_lock:
            _inject_queues.pop(tid, None)
        # ── CMN P4: 空闲触发反思回路（2026-08-17 已禁用：自动反思产出的碎片平时不加载，对行为零约束，且每次对话后烧 LLM 调用；行为修正改由重要事项第12条承担，需要时手动调 reflect 工具/API）──
        # def _idle_reflect():
        #     try:
        #         from memory.reflection import trigger_deep_integration_now
        #         trigger_deep_integration_now(get_db())
        #     except Exception as e:
        #         print(f"[memory] idle reflection failed: {e}")
        # threading.Thread(target=_idle_reflect, daemon=True).start()
        # ── 轻量叙事触发（2026-08-30：story 断更修复。不恢复完整空闲反思，
        #    只在积压≥30条 且 距上次≥2h 时，后台跑一次 narrative_consolidation；
        #    该函数输入本身是增量的（只取未整理碎片），单次成本≈1-3次小 LLM 调用）──
        def _lightweight_narrative():
            try:
                if not hasattr(Handler, "_narrative_lock"):
                    return  # 未初始化（create_server 应已初始化）
                now_ts = time.time()
                with Handler._narrative_lock:
                    if now_ts - Handler._last_narrative_ts < 7200:
                        return
                    Handler._last_narrative_ts = now_ts  # 先占位防并发重复触发
                from memory.reflection_loop import get_loop
                loop = get_loop()
                backlog = loop.fragment_store.count_unconsolidated_cores()
                if backlog < 30:
                    return
                n = loop.narrative_consolidation()
                print(f"[memory] lightweight narrative: +{n} stories (backlog was {backlog})")
            except Exception as e:
                print(f"[memory] lightweight narrative failed: {e}")
        threading.Thread(target=_lightweight_narrative, daemon=True).start()
        self._json(200, {
            "response": full_response,
            "thinking": full_thinking,
            "intermediate": full_intermediate,
            "topic_id": tid,
            "usage": {"input": total_input_tokens, "output": total_output_tokens},
        })

# ══════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════
def create_server(port=PORT):
    db = get_db()
    Handler.topics = TopicManager(db)
    Handler.db = db
    Handler.port = port
    # 轻量叙事触发的节流状态（单线程初始化，避免请求线程竞态建锁）
    Handler._narrative_lock = threading.Lock()
    Handler._last_narrative_ts = 0.0
    return http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="大眼X Server")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"HTTP port (default: {PORT})")
    ap.add_argument("--label", default="", help="Instance label")
    args = ap.parse_args()


    # Start reminder worker
    try:
        import reminder_worker
        reminder_worker.init(ROOT_DIR)
        reminder_worker.start_worker(get_db)
        print(f"[大眼X] Reminder worker started")
    except Exception as e:
        print(f"[大眼X] Reminder worker failed: {e}")
    # Start suggestion refresher — 空态 AI 建议池按频率静默刷新
    try:
        threading.Thread(target=_suggestion_refresh_loop, daemon=True).start()
        print(f"[大眼X] Suggestion refresher started")
    except Exception as e:
        print(f"[大眼X] Suggestion refresher failed: {e}")
    # 预热动态工具检索索引（后台线程，不阻塞启动；失败静默，首次请求会懒加载）
    try:
        from tools import tool_router
        threading.Thread(target=tool_router.build_index, daemon=True).start()
        print(f"[大眼X] Tool router index prewarm started")
    except Exception as e:
        print(f"[大眼X] Tool router prewarm failed: {e}")
    # Start rule engine worker — 默认关闭常驻轮询（避免后台写库刷屏 APP）
    # 改为事件触发：用户发消息瞬间在 _handle_chat 里实时检查，只注入 prompt 不落库。
    # 如需后台盯长时任务，可手动开启：RULE_WORKER=1 python server.py
    # try:
    #     sys.path.insert(0, os.path.join(ROOT_DIR, "rules_engine"))
    #     import rule_worker
    #     rule_worker.start_in_thread(interval=120, max_topics=3,
    #                                 base=f"http://127.0.0.1:{args.port}")
    #     print(f"[大眼X] Rule worker started")
    # except Exception as e:
    #     print(f"[大眼X] Rule worker failed: {e}")
    # Bootstrap frozen data (copy config/public from _internal if needed)
    _bootstrap_data()

    # DB schema is created by Database.__init__ (called from get_db())
    db = get_db()

    # ── Restore accumulated cost from DB ──
    global _total_cost_cny, _total_tokens_global, _total_spent_cny, _last_balance_cny
    try:
        saved_cny = db.get_meta("total_cost_cny")
        if saved_cny:
            _total_cost_cny = float(saved_cny)
        saved_tokens = db.get_meta("total_tokens")
        if saved_tokens:
            _total_tokens_global = int(saved_tokens)
        saved_spent = db.get_meta("total_spent_cny")
        if saved_spent:
            _total_spent_cny = float(saved_spent)
        saved_last = db.get_meta("last_balance_cny")
        if saved_last:
            _last_balance_cny = float(saved_last)
        print(f"[大眼X] Cost restored: ¥{_total_cost_cny:.4f}, {_total_tokens_global} tokens")
    except Exception as e:
        print(f"[大眼X] Cost restore failed: {e}")
    # ── 一次性迁移：历史 total_cost 存的是 USD，统一转 CNY（×7）与新精确估价同单位 ──
    try:
        if db.get_meta("cost_unit_cny") != "1":
            # 仅 topics.total_cost 旧存 USD 需转；meta total_cost_cny 本就是 CNY 不动
            db._execute("UPDATE topics SET total_cost = total_cost * 7.0")
            db._commit()
            db.set_meta("cost_unit_cny", "1")
            print("[大眼X] Cost migrated: topic total_cost USD→CNY")
    except Exception as e:
        print(f"[大眼X] Cost migration failed: {e}")
    # ── Load compress config from DB ──
    _load_compress_config(db)

    # Pre-warm balance + rate cache in background
    threading.Thread(target=_fetch_cny_rate, daemon=True).start()
    threading.Thread(target=_query_balance, daemon=True).start()


    server = create_server(args.port)
    print(f"[大眼X] [网络]  http://0.0.0.0:{args.port}")
    for ip in get_ips(args.port):
        print(f"[大眼X]    {ip}")
    # ── Register system_status tool ──
    @register_tool("system_status", "查看大眼的系统状态：运行路径、进程PID、当前工作目录、端口号、操作系统版本。涉及路径/目录归属问题时先调这个。")
    def _system_status():
        import platform
        return {
            "pid": os.getpid(),
            "base_dir": ROOT_DIR,
            "root_dir": ROOT_DIR,
            "cwd": os.getcwd(),
            "port": args.port,
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        }

    @register_tool("reflect", "主动反思最近的对话——扫描所有话题，提取有价值的认知、经验和因果链，存入记忆碎片。反思提示词存在 data/reflection_prompt.txt，可用 write_file 自定义修改（如调整反思深度、关注点、输出格式）。")
    def _reflect():
        try:
            from memory.reflection import trigger_deep_integration_now
            trigger_deep_integration_now(get_db())
            return {"reflected": True, "message": "反思已完成，结果存入记忆碎片"}
        except Exception as e:
            return {"error": f"反思失败: {e}"}

    # ── Register write_draft tool ──
    @register_tool("write_draft",
        "任务草稿纸——任务自带的便签。拆思路、方案选型、debug 根因分析、记临时状态。短句，随时记，不需要传路径，自动跟随当前任务。",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "草稿内容，简短。拆思路、debug根因、记临时状态用。"},
                "mode": {"type": "string", "enum": ["append", "overwrite"], "description": "追加(默认)还是覆盖。追加适合持续记录，覆盖适合重写。"}
            },
            "required": ["content"]
        }
    )
    def _write_draft(content: str, mode: str = "append"):
        try:
            from db import get_db
            db = get_db()
            tid = db.get_active_topic_id()
            if not tid:
                return {"error": "没有活跃任务，无法写草稿"}
            drafts_dir = os.path.join(ROOT_DIR, "data", "drafts")
            os.makedirs(drafts_dir, exist_ok=True)
            draft_path = os.path.join(drafts_dir, f"{tid[:8]}.md")
            if mode == "overwrite" or not os.path.exists(draft_path):
                with open(draft_path, "w", encoding="utf-8") as f:
                    f.write(content)
            else:
                with open(draft_path, "a", encoding="utf-8") as f:
                    f.write("\n" + content)
            return {"written": True}
        except Exception as e:
            return {"error": f"写入草稿失败: {e}"}

    # ── Register update_task_brief tool ──
    @register_tool("update_task_brief",
        "更新任务心智锚点——任务自带的概览便签。写'这是什么任务、做到哪了、下一步'。"
        "任务切换回来时系统会自动展示这个锚点帮你恢复状态。简短，3-6行，用第一人称。",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "任务概览。建议格式：第1行任务名，第2行进度，第3行下一步。3-6行。"}
            },
            "required": ["content"]
        }
    )
    def _update_task_brief(content: str):
        try:
            from db import get_db
            db = get_db()
            tid = db.get_active_topic_id()
            if not tid:
                return {"error": "没有活跃任务"}
            anchors_dir = os.path.join(ROOT_DIR, "data", "anchors")
            os.makedirs(anchors_dir, exist_ok=True)
            anchor_path = os.path.join(anchors_dir, f"{tid[:8]}.md")
            with open(anchor_path, "w", encoding="utf-8") as f:
                f.write(content.strip())
            return {"updated": True}
        except Exception as e:
            return {"error": f"更新锚点失败: {e}"}

    # ── Auto-load MCP servers ──
    try:
        from mcp_client import load_mcp_servers
        mcp_count = load_mcp_servers()
        if mcp_count:
            print(f"[mcp] {mcp_count} MCP servers loaded")
    except Exception:
        pass
    # ── Register hot_reload tool ──
    @register_tool("hot_reload", "写完新技能或工具后调用——重新扫描 skills/ 目录并动态加载 tools/ 下的新 .py 文件，不需要重启服务器。返回加载结果。")
    def _hot_reload():
        import importlib, glob as glob_mod
        results = []
        # Re-scan skills
        try:
            from skills_scanner import scan_skills
            new_skills = scan_skills()
            results.append(f"技能: {len(new_skills)} 个已加载")
        except Exception as e:
            results.append(f"技能扫描失败: {e}")
        # Reload task/*.py core modules first (dag, executor, work_memory...)
        # so tools that `from task.dag import DAG` pick up fresh classes.
        task_dir = os.path.join(BASE_DIR, "task")
        task_loaded = 0
        for fp in sorted(glob_mod.glob(os.path.join(task_dir, "*.py"))):
            mod_name = os.path.splitext(os.path.basename(fp))[0]
            if mod_name.startswith("_"):
                continue
            full_name = f"task.{mod_name}"
            try:
                if full_name in sys.modules:
                    importlib.reload(sys.modules[full_name])
                else:
                    importlib.import_module(full_name)
                task_loaded += 1
            except Exception:
                pass
        if task_loaded:
            results.append(f"任务核心: {task_loaded} 个模块已重载")
        # Reload tools/*.py (reloads existing modules, loads new ones)
        tools_dir = os.path.join(BASE_DIR, "tools")
        loaded = 0
        for fp in sorted(glob_mod.glob(os.path.join(tools_dir, "*.py"))):
            mod_name = os.path.splitext(os.path.basename(fp))[0]
            if mod_name.startswith("_"):
                continue
            # registry.py 不能 reload——它的模块顶层有 _tools = {}，
            # reload 会清空整个工具字典，导致排在其前面的模块工具全部丢失
            if mod_name == "registry":
                continue
            full_name = f"tools.{mod_name}"
            try:
                if full_name in sys.modules:
                    importlib.reload(sys.modules[full_name])
                else:
                    importlib.import_module(full_name)
                loaded += 1
            except Exception:
                pass
        if loaded:
            results.append(f"工具: {loaded} 个模块已加载/重载，共 {len(get_tool_defs())} 个工具")
        else:
            results.append("工具: 无模块可加载")
        # Re-load MCP servers (pick up new mcp_servers.json)
        try:
            from mcp_client import load_mcp_servers, list_mcp_servers
            load_mcp_servers()
            mcp_list = list_mcp_servers()
            if mcp_list:
                total_tools = sum(len(v["tools"]) for v in mcp_list.values())
                results.append(f"MCP: {len(mcp_list)} 服务器, {total_tools} 个工具")
            else:
                results.append("MCP: 无服务器 (创建 mcp_servers.json 添加)")
        except Exception:
            results.append("MCP: 加载失败")
        return {"loaded": True, "results": results}

    print(f"[大眼X] Tool count: {len(get_tool_defs())}")
    print(f"[大眼X] Memory: {'enabled' if _get_memory_store() else 'disabled'}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[大眼X] Shutting down...")
        server.shutdown()

if __name__ == "__main__":
    main()
