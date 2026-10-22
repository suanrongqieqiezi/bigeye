#!/usr/bin/env python
"""
大眼X Server — 全能超级个体。
直接 LLM API 调用 + 工具系统 + 记忆系统。
从模型调用到工具、记忆全部在本进程内完成，不依赖任何外部 Agent 进程。
"""
import json
import task_runtime
import os
import sys
import time
import datetime
import uuid
import re
import math
import base64
import hashlib
import socket
import signal
import subprocess
import threading
import http.server
import urllib.request
import urllib.parse
import urllib.error
import traceback
import queue
import ssl
from collections import deque

# 公网隧道（配置 / 保活 / 状态）。导入失败只影响「公网访问」功能，不能让主服务起不来。
try:
    import tunnel_manager
except Exception as _tm_err:  # pragma: no cover
    tunnel_manager = None
    _TM_IMPORT_ERR = f"{type(_tm_err).__name__}: {_tm_err}"
else:
    _TM_IMPORT_ERR = ""

# 部分环境（企业 VPN / 抓包软件 / 杀软）会向 HTTPS 链路注入自签名证书，
# 导致 urllib 默认证书校验失败。本服务为本地部署，禁用证书校验可接受。
_UNVERIFIED_SSL_CTX = ssl.create_default_context()
_UNVERIFIED_SSL_CTX.check_hostname = False
_UNVERIFIED_SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Path setup ──────────────────────────────────────
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else BASE_DIR
PUBLIC_DIR = os.path.join(BASE_DIR, "public")


# ── 前端指纹（APK 冷启动只比哈希，不重搬前端） ──────────
# 病根：手机端每次打开都是新会话，主文档又带 _t 时间戳 ⇒ 缓存键每次都不同，
# 987K 的 index.html + 子资源全量重下。这里给出一个稳定的 build 指纹，
# 客户端先拉这几百字节，指纹一致就走 WebView 缓存（未命中还有 ETag 304 兜底）。
_FE_FILES = ("index.html", "composer-floating.js", "mindmap.js")
_FE_SHA_CACHE = {}
_FE_MANIFEST_CACHE = {"key": None, "data": None}


def _file_sha1(path, st):
    key = (path, st.st_size, int(st.st_mtime))
    hit = _FE_SHA_CACHE.get(key)
    if hit is not None:
        return hit
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    val = h.hexdigest()
    _FE_SHA_CACHE.clear()          # 只留最近一轮，避免常驻占内存
    _FE_SHA_CACHE[key] = val
    return val


def fe_manifest():
    """前端构建清单：build = 各核心文件内容 sha1 的合并指纹。"""
    sig = []
    entries = {}
    for rel in _FE_FILES:
        fp = os.path.join(PUBLIC_DIR, rel)
        try:
            st = os.stat(fp)
            sig.append((rel, st.st_size, int(st.st_mtime)))
            entries[rel] = {"size": st.st_size, "sha": _file_sha1(fp, st)}
        except OSError:
            sig.append((rel, -1, -1))
            entries[rel] = {"size": 0, "sha": "missing"}
    key = tuple(sig)
    if _FE_MANIFEST_CACHE["key"] == key:
        return _FE_MANIFEST_CACHE["data"]
    h = hashlib.sha1()
    for rel in _FE_FILES:
        h.update(entries[rel]["sha"].encode())
    data = {
        "build": h.hexdigest()[:16],
        "ts": max([s[2] for s in sig] + [0]),
        "files": entries,
    }
    _FE_MANIFEST_CACHE["key"] = key
    _FE_MANIFEST_CACHE["data"] = data
    return data


# ── Ports ───────────────────────────────────────────
PORT = 8765

# ── Imports ─────────────────────────────────────────
sys.path.insert(0, BASE_DIR)
from db import (get_db, Database,
                recycle_items, recycle_take, recycle_clear, recycle_put)  # 回收站（deleted_topics_backup.jsonl）
from llm import LLMConfig, chat_stream, chat_stream_cached, _build_messages, is_transient_conn_error, is_retryable_llm_error, build_multimodal_content, strip_image_content, image_mime_type, is_vision_model
from tools.registry import get_tool_defs, execute_tool, register_tool
import loop_detector  # 目标级打转检测（探索类占比高且零落地 → 注入提醒）
import local_llm  # 本地模型服务探活/按需拉起（Qwen3.8 27B @ llama.cpp）
import custom_providers  # 第三方模型源：注册/归一化/协议探测（模型设置里无限添加）
# 打包成 exe 后 ROOT_DIR ≠ 源码目录，必须把真实路径注入进去
local_llm.configure(config_path=os.path.join(ROOT_DIR, "model_config.json"),
                    data_dir=os.path.join(ROOT_DIR, "data"))
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

import tools.asset_lib  # 资产索引层：库=索引登记制（data/assets/index.json，不搬文件）
import tools.asset_v3   # 资产库 v3：混合仓（笔记/速记/链接实体入库，文件≤10MB拷入，大文件记引用）
import tools.canvas  # 工作区画布布局：画布+窗口（data/missions/{tid}/canvas.json）
import tools.canvas_tools  # AI 画布工具（读布局/摆窗口/收窗口/导图节点）

# Import v4.0 task execution tools
import tools.task_v4_tools
import tools.mindmap_tools  # 思维导图工具（与 DAG 对齐的写通道）
import tools.meta_tools  # 元工具：discover_tools + execute_advanced_tool（工具折叠）
import tools.ask_user  # 阻塞式提问工具（ask_user，折叠分组 interaction）
import tools.focus_tools  # 注意力等级工具（set_focus_level）
import tools.spawn_agent
import tools.order_memory_tools  # OM秩序记忆（秩学习规则，Phase1）  # 子代理工具（spawn_agent）
import tools.rules_engine_tools  # 规则引擎管理工具（rule_list/rule_add/rule_update/rule_delete）
import tools.audit_guard  # 坑指纹核查工具（audit_check：动作前比对事件账本历史坑）
import tools.tool_output  # 有界结果引用：read_tool_output（翻页读超限输出）/ tool_output_stats
import tools.see_image  # 看图：磁盘图片/视频帧 → 多模态模型（折叠分组 vision）。2026-10-16：漏登记导致重启后工具消失

try:
    import tool_outputs as _tool_output_store  # 超限工具输出落盘（改造②）
except Exception:
    _tool_output_store = None


def _unwrap_result_payload(s):
    """剥掉 registry.execute_tool 加的单键 {"result": {...}} 包装，返回 (payload_json, 是否剥过)。

    2026-10-17 定位：execute_tool 统一 `return {"result": result}`，这层壳落盘后
    模型按工具真实字段名调用 read_tool_output(field="stdout"/"content") 会直接报
    「字段不存在，可用: ['result']」——任务 6d895c38e809 里实测踩到（消息 [1946]）。
    剥壳后摘要里的 head 也更可读（少一层嵌套），字段名与工具原始返回一致。

    只在"单键 + result 值是 dict/list"时剥，业务字段本身叫 result 且是标量/字符串
    的不动（那种情况 field="result" 仍然可用）。
    """
    try:
        obj = json.loads(s)
    except Exception:
        return s, False
    depth = 0
    while (isinstance(obj, dict) and len(obj) == 1
           and isinstance(obj.get("result"), (dict, list)) and depth < 3):
        obj = obj["result"]
        depth += 1
    if depth == 0:
        return s, False
    try:
        return json.dumps(obj, ensure_ascii=False), True
    except Exception:
        return s, False


def _save_bounded_ref(result_json, tid, tool_name, raw_result):
    """把超限工具输出落盘成有界引用，返回摘要 dict；**失败返回 None 由调用方回退旧截断**。

    2026-10-15 改造②：原行为是原地砍中间（丢弃部分永久丢失）。
    纪律：落盘异常绝不允许导致工具结果丢失 → 必须回退。
    """
    if _tool_output_store is None:
        return None
    try:
        # 2026-10-17：落盘就剥壳，别把 registry 的 {"result": X} 带进引用库
        _payload, _unwrapped = _unwrap_result_payload(result_json)
        return _tool_output_store.save(
            _payload, topic_id=tid, tool_name=tool_name,
            is_json=isinstance(raw_result, dict) or _unwrapped,
        )
    except Exception:
        try:
            _tool_output_store._log_degrade(
                f"_save_bounded_ref raised for {tool_name} len={len(result_json)}")
        except Exception:
            pass
        return None




def _get_matters(tid=None):
    from tools.important_matters import get_matters
    # 优先用调用方传入的话题（组装系统提示时必须显式传，否则会读到全局活跃指针串台）；
    # 未传时回落本线程任务上下文。
    if tid is None:
        try:
            from tools.task_context import get_current_topic
            tid = get_current_topic()
        except Exception:
            tid = None
    return get_matters(tid)


def _get_matter_items(tid=None):
    """完整条目视图（带缓冲标记），前端区分已生效/缓冲中。"""
    try:
        from tools.mission_overlay import combined_entries
        if tid is None:
            from tools.task_context import get_current_topic
            tid = get_current_topic()
        return [{"content": e["content"], "pending": bool(e.get("_pending"))}
                for e in combined_entries(tid)]
    except Exception:
        return [{"content": c, "pending": False} for c in _get_matters()]


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


def _select_stories(rows, tid):
    """故事链三档分组（2026-09-04 串台修复）：
    ① 本任务故事优先（最多3条，不加标）② 其余按时间补位到5条
    ③ 跨任务/无归属条目加 [别的任务] 标注（无归属按跨任务处理：误标无害，漏标有害）
    边界：跨任务故事不足时用剩余本任务故事补足到5条（避免本任务故事多反而显示变少）
    rows 需按时间线倒序；返回 [(row, other_tag_bool), ...]
    """
    _own_all = [r for r in rows if (r["topic_id"] or "") == tid]
    _own = _own_all[:3]
    _own_ids = {r["id"] for r in _own}
    _selected = list(_own)
    for r in rows:
        if r["id"] not in _own_ids and len(_selected) < 5:
            _selected.append(r)
    if len(_selected) < 5:
        for r in _own_all[3:]:
            if len(_selected) >= 5:
                break
            _selected.append(r)
    return [(r, (r["topic_id"] or "") != tid) for r in _selected]




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
# 工具结果内联上限（字符）。默认 4000，超了落盘成有界引用、只给 head1000+tail500。
# 但"本来就是用来读长文"的工具必须单独放宽：否则 read_topic_messages 一页上万字
# 会被外层再砍成 1500 字 —— AI 以为翻了 10 条，实际只看到 1 条半（2026-10-21 复盘）。
_TOOL_INLINE_DEFAULT = 4000
_TOOL_INLINE_CAP = {"read_topic_messages": 14000}


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

    稳定性原则（2026-09-27 重构）：
    - 标准形态 data/missions/{tid}/workspace 永远按 tid 现算，不依赖 meta 存值；
      存量键由 db.normalize_workspace_metas() 启动时清理。
    - 所有回退必须打印警告，禁止静默漂移到项目根（静默回退导致过嵌套 workspace 副本）。
    """
    std_ws = os.path.join(ROOT_DIR, "data", "missions", topic_id or "", "workspace") if topic_id else None
    if not ws_rel:
        # 无 meta 值：有 tid 就给标准工作区（目录由 create/switch 接口保证存在）
        if std_ws and os.path.isdir(std_ws):
            return std_ws
        print(f"[workspace] ⚠️ 无 meta 且标准目录不存在，回退项目根 tid={topic_id} std={std_ws}")
        return ROOT_DIR
    # Windows: 'C:'（无反斜杠）是当前目录相对路径，补成 'C:\\'
    if os.name == 'nt' and len(ws_rel) == 2 and ws_rel[1] == ':':
        ws_rel = ws_rel + '\\'
    if os.path.isabs(ws_rel):
        abs_path = os.path.abspath(ws_rel)
        if os.path.isdir(abs_path):
            return abs_path
        # 绝对路径不存在（旧机器残留）：按 tid 回标准工作区
        if std_ws and os.path.isdir(std_ws):
            print(f"[workspace] ⚠️ meta 指向不存在的绝对路径，回标准工作区: {ws_rel} -> {std_ws}")
            return std_ws
        # 从路径中提取 data/missions/{id}/workspace 部分再试
        norm = os.path.normpath(ws_rel)
        parts = norm.split(os.sep)
        for i, part in enumerate(parts):
            if part == "data" and i + 1 < len(parts) and parts[i + 1] == "missions":
                rel_from_data = os.sep.join(parts[i:])
                candidate = os.path.join(ROOT_DIR, rel_from_data)
                if os.path.isdir(candidate):
                    print(f"[workspace] ⚠️ meta 绝对路径失效，按路径结构重锚: {ws_rel} -> {candidate}")
                    return candidate
        print(f"[workspace] ⚠️ meta 绝对路径失效且无法重锚，回退项目根: {ws_rel}")
        return abs_path
    rel_path = os.path.abspath(os.path.join(ROOT_DIR, ws_rel))
    if not os.path.isdir(rel_path):
        print(f"[workspace] ⚠️ meta 相对路径解析后不存在: {ws_rel} -> {rel_path}")
    return rel_path
_request_runtime = threading.local()

def _effective_limit():
    """Effective request-scoped budget with safety margin."""
    return int(getattr(_request_runtime, 'budget', TOKEN_LIMIT) * ESTIMATE_SAFETY)
_last_request_tokens = 0  # input tokens of most recent API request
_last_hard_tokens = 0     # 最近一次请求的硬性内容实测 tokens（系统提示+工具schema）

# ── 模型上下文窗口（工作记忆窗口上限）──
# 已知模型上下文窗口（tokens）；未登记模型回退 _DEFAULT_CONTEXT_WINDOW（仅作推定值）
_MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-pro": 1000000,
    "deepseek-v4-flash": 1000000,
    "deepseek-chat": 1000000,
    "deepseek-reasoner": 1000000,
    "glm-5.3-flash": 1000000,
    # 阶跃星辰（官方文档「模型概览」实测值；第三方源同名前缀也走这里）。
    # 注意 step-3.5-flash 用前缀匹配，step-3.5-flash-2603 会命中同一个值。
    "step-5-preview": 1000000,
    "step-3.7-flash": 256000,
    "step-3.5-flash": 256000,
    "step-1o-turbo-vision": 32000,
}
_DEFAULT_CONTEXT_WINDOW = 128000
# 工作记忆容量的绝对上限。未知窗口的模型（聚合站别名 / 私有网关）由用户自己定，
# 只受这个绝对上限约束——不能拿一个"猜出来的 128K"去改写用户的显式设置。
_WM_ABS_MAX = 2000000


def _ctx_window_info(model_id):
    """返回 (推定上下文窗口, 来源)。来源：provider / local / known / unknown。

    推定值只用于估算压缩阈值、任务预算和设置页展示。
    unknown 时**不得**当作可设置上界（等于用启发式替用户拍板）。
    """
    if not model_id:
        return _DEFAULT_CONTEXT_WINDOW, "unknown"
    mid = model_id.lower()
    # 第三方源：同一个模型名在不同聚合站的窗口可能被改写（或本身是别名），
    # 用户在设置页填了就信用户 —— 否则上下文压缩阈值会按错的窗口估算
    try:
        _cfg = get_model_config()
        _cp = custom_providers.runtime_config(ROOT_DIR, _cfg.provider)
        if _cp and _cp.get("ctx_window") and mid == str(_cfg.model or "").lower():
            return int(_cp["ctx_window"]), "provider"
    except Exception:
        pass
    # 本地 llama.cpp 模型：上下文由 llama-server 的 --ctx-size 决定（本机 24K），
    # 不是默认的 128K。必须用真实值，否则应用按 128K 估算上下文、实际只有 24K
    # → 请求超长被 llama-server 拒绝或头部被截断，表现为"忘了前面说的话"。
    try:
        if local_llm.is_local_model(mid):
            return int(local_llm.effective_ctx()), "local"
    except Exception as e:
        print(f"[ctx] local ctx probe failed, fallback to default: {e}")
    if mid in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[mid], "known"
    for k, v in _MODEL_CONTEXT_WINDOWS.items():
        if mid.startswith(k) or k.startswith(mid):
            return v, "known"
    return _DEFAULT_CONTEXT_WINDOW, "unknown"


def _model_context_window(model_id):
    """查模型上下文窗口（推定值）；未知模型前缀匹配，仍未知用默认值。"""
    return _ctx_window_info(model_id)[0]


def _wm_setting_ceiling(model_id=None):
    """工作记忆容量「可设置」的上限。

    已知窗口 → 用真实窗口（小窗口模型不该被设成 200K，那会直接发超长请求）；
    未知窗口 → 用 _WM_ABS_MAX。未知模型上没有可靠事实，就不该拿默认值冒充事实。
    """
    try:
        mid = model_id if model_id is not None else get_model_config().model
        win, src = _ctx_window_info(mid)
        if src == "unknown":
            return _WM_ABS_MAX
        return int(max(min(int(win), _WM_ABS_MAX), _wm_min_token_limit()))
    except Exception:
        return _WM_ABS_MAX


def _effective_token_limit(db=None):
    """实际生效的工作记忆上限 = 库里的设置 → 钳制到 [最小可设值, 可设上限]。

    GET /api/settings/compress 用它回报，保证前端输入框的 value ≤ max。
    可设上限走 _wm_setting_ceiling()：未知窗口的模型不再被 128K 推定值卡住，
    否则用户填 200000 存进去就变成 128000（静默改写，看不出是谁干的）。
    """
    try:
        db = db or get_db()
        raw = int(db.get_meta("compress_token_limit") or TOKEN_LIMIT)
    except Exception:
        raw = TOKEN_LIMIT
    try:
        return int(min(max(raw, _wm_min_token_limit()), _wm_setting_ceiling()))
    except Exception:
        return int(raw)


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


def _runtime_switch(db, meta_key, cfg_name, default=True):
    """运行时开关统一读取：前端设置(db.meta '1'/'0') > 环境变量(config.py) > 代码默认。
    meta 未存过（None/''）时用 config 值；每次现读，改 meta 即热生效。"""
    try:
        import config
        cfg_val = getattr(config, cfg_name, default)
    except Exception:
        cfg_val = default
    raw = db.get_meta(meta_key)
    if raw is None or raw == "":
        return bool(cfg_val)
    return raw == "1"


def _load_compress_config(db=None):
    """Load compress settings from DB into module globals. Idempotent after first load."""
    global PER_MESSAGE_COMPRESS_CHARS, TOKEN_LIMIT, COMPRESS_WARN_PCT, TURN_BUDGET, MSG_COUNT_WARN, MSG_COUNT_WALL
    global _VERIFY_SSL, _DEV_MODE, _compress_config_loaded, _LLM_CACHE_ENABLED
    try:
        db = db or get_db()
        PER_MESSAGE_COMPRESS_CHARS = int(db.get_meta("compress_per_msg_chars") or 500)
        TOKEN_LIMIT = int(db.get_meta("compress_token_limit") or 200000)
        # 钳制到 [最小可设值, 可设上限]（未知窗口模型放行到 _WM_ABS_MAX）
        TOKEN_LIMIT = max(TOKEN_LIMIT, _wm_min_token_limit())
        TOKEN_LIMIT = min(TOKEN_LIMIT, _wm_setting_ceiling())
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
    而是解出图片真实宽高后按 patch 换算（见 _image_tokens_from_data_url）。"""
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
                        total += _image_tokens_from_data_url(
                            (block.get("image_url") or {}).get("url", ""))
            rest = {k: v for k, v in m.items() if k != "content"}
            if rest:
                total += _count_text(json.dumps(rest, ensure_ascii=False))
        else:
            total += _count_text(json.dumps(m, ensure_ascii=False))
    # ×1.5 for unseen overhead: tool definitions, API formatting, special tokens
    return int(total * 1.5)


# ── 单张图片的 token 估算 ──────────────────────────
# 旧实现固定 _EST_IMAGE_TOKENS = 1000，严重低估：一张 1216×2640 的手机截图
# 实测约 4180 视觉 token（按 28px/patch）。低估的后果是压缩逻辑以为图很小、
# 不去裁剪，真实请求直接爆上下文（09-12 实测 HTTP 400:
# request 25868 tokens exceeds the available context size 24576）。
# 现在按图片真实宽高换算，并取偏大值 —— 高估只是多压缩一点，低估会直接失败。
_EST_IMAGE_TOKENS_DEFAULT = 1500   # 头部解析失败时的兜底
_EST_IMAGE_TOKENS_MAX = 16000      # 单图上限（视觉编码器自身也会缩放/切块）


def _image_size_from_bytes(b):
    """从图片字节流解析 (宽, 高)，失败返回 (0, 0)。只读头部，不解码像素。"""
    try:
        if len(b) < 24:
            return 0, 0
        # PNG：IHDR 紧跟签名
        if b[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(b[16:20], "big"),
                    int.from_bytes(b[20:24], "big"))
        # GIF：宽高为小端 16 位
        if b[:6] in (b"GIF87a", b"GIF89a"):
            return (int.from_bytes(b[6:8], "little"),
                    int.from_bytes(b[8:10], "little"))
        # JPEG：遍历段找 SOF（跳过 SOI/APPn 等）
        if b[:2] == b"\xff\xd8":
            i, n = 2, len(b)
            while i + 9 < n:
                if b[i] != 0xFF:
                    i += 1
                    continue
                marker = b[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = int.from_bytes(b[i + 2:i + 4], "big")
                if seg_len < 2:
                    break
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return (int.from_bytes(b[i + 7:i + 9], "big"),
                            int.from_bytes(b[i + 5:i + 7], "big"))
                i += 2 + seg_len
            return 0, 0
        # WebP：VP8X 有显式画布尺寸；VP8 / VP8L 头里也有
        if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            fourcc = b[12:16]
            if fourcc == b"VP8X":
                return (int.from_bytes(b[24:27], "little") + 1,
                        int.from_bytes(b[27:30], "little") + 1)
            if fourcc == b"VP8 ":
                s = b.find(b"\x9d\x01\x2a", 20)
                if s > 0 and s + 7 <= len(b):
                    return (int.from_bytes(b[s + 3:s + 5], "little") & 0x3FFF,
                            int.from_bytes(b[s + 5:s + 7], "little") & 0x3FFF)
            if fourcc == b"VP8L" and len(b) >= 25:
                bits = int.from_bytes(b[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    except Exception:
        pass
    return 0, 0


def _tokens_from_size(w, h):
    """按视觉编码器的 patch 切块换算 token 数。

    Qwen-VL / GLM 系：14px patch + 2×2 merge ≈ 28px 一个 token。
    DeepSeek vision：85 + 每 512² tile × 170。
    取两者较大值（宁可高估），并封顶。
    """
    if not w or not h:
        return _EST_IMAGE_TOKENS_DEFAULT
    qwen = math.ceil(w / 28) * math.ceil(h / 28)
    ds = 85 + math.ceil(w / 512) * math.ceil(h / 512) * 170
    return min(max(qwen, ds), _EST_IMAGE_TOKENS_MAX)


def _image_tokens_from_path(path):
    """从磁盘图片文件估算 token（只看头部）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
        return _tokens_from_size(*_image_size_from_bytes(head))
    except Exception:
        return _EST_IMAGE_TOKENS_DEFAULT


def _image_tokens_from_data_url(url):
    """从 data:image/...;base64,xxx 估算 token（只解前 8KB 头部）。"""
    try:
        s = str(url)
        if ";base64," not in s:
            return _EST_IMAGE_TOKENS_DEFAULT
        b64 = s.split(";base64,", 1)[1]
        chunk = b64[:8192]
        chunk += "=" * (-len(chunk) % 4)
        return _tokens_from_size(*_image_size_from_bytes(base64.b64decode(chunk)))
    except Exception:
        return _EST_IMAGE_TOKENS_DEFAULT


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
        f"  找回: read_topic_messages(topic_id='{str(topic_id or '')[:8]}', query='要找回的那件事')",
        f"  逐页翻: read_topic_messages(topic_id='{str(topic_id or '')[:8]}', more=True)",
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

def _trim_context_to_budget(messages, token_limit, store=None, topic_id=None, on_dropped=None):
    """预算裁剪入口 —— 先把「归档摘要」摘出来，避免被从前往后裁掉。

    `_ct_node` 消息是整理树生成的摘要（[已整理 L1] …），它们本身就位于历史最前面，
    而裁剪是从前往后按对话边界切的，于是每轮都会先砍摘要：
    「刚整理出来的精华，下一轮就被自己裁掉」，整理等于白做（2026-10-21 实测发现）。
    摘要体积很小（两个节点约 1.1k 字 ≈ 600 tok），价值却最高，因此固定保留。

    🔴 但**只钉最近 _CT_PIN_MAX 个**（2026-10-22 复盘）：节点每整理 200 项就多一个，
    无限钉住等于给上下文开后门 —— 实测攒到十几个节点后，墙设 20 却仍放行 55 条，
    token 预算也被这些"永不参与裁剪"的消息架空。旧节点照常参与裁剪，
    真要用还能 expand_compressed 展开（原文一直在库里）。
    """
    history = messages[1:] if len(messages) > 1 else []
    _all_nodes = [m for m in history if m.get("_ct_node")]
    # 取 anchor 最大的若干个（越靠后越新）
    pinned = sorted(_all_nodes, key=lambda m: m.get("_anchor") or 0)[-_CT_PIN_MAX:]
    if not pinned:
        return _trim_context_to_budget_raw(messages, token_limit, store=store,
                                           topic_id=topic_id, on_dropped=on_dropped)
    _pin_ids = {id(m) for m in pinned}
    rest = [m for m in history if id(m) not in _pin_ids]
    trimmed, n = _trim_context_to_budget_raw([messages[0]] + rest, token_limit,
                                             store=store, topic_id=topic_id,
                                             on_dropped=on_dropped)
    return [trimmed[0]] + pinned + trimmed[1:], n


def _trim_context_to_budget_raw(messages, token_limit, store=None, topic_id=None, on_dropped=None):
    """Trim oldest complete dialogues from front until under budget.

    A dialogue = one user message + all following assistant/tool messages
    until the next user message (or end). Never trims mid-dialogue.

    on_dropped(dropped_msgs)：被截掉那批消息的回调。用来把「凭空消失」改成
    「后台整理成摘要节点」——见 _schedule_organize_dropped。回调里抛异常不影响裁剪。

    Returns (trimmed_messages, truncated_count).
    """
    truncated_count = 0

    system_msg = messages[0]
    history_msgs = list(messages[1:])
    _orig_history = list(history_msgs)

    def _emit_dropped(kept):
        if not on_dropped:
            return
        _keep = {id(x) for x in kept}
        dropped = [m for m in _orig_history if id(m) not in _keep]
        if not dropped:
            return
        try:
            on_dropped(dropped)
        except Exception as _e:
            print(f"[context-wall] on_dropped 回调失败（不影响本轮请求）: {_e}")

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
                _emit_dropped(history_msgs)
                return [system_msg] + history_msgs, truncated_count

        # Still over budget — keep only the last assistant exchange + user
        cut_idx = assistant_indices[-1]
        truncated_count = cut_idx - 1
        history_msgs = [history_msgs[0]] + history_msgs[cut_idx:]
        _apply_context_wall(system_msg, history_msgs, truncated_count, store, topic_id)
        _emit_dropped(history_msgs)
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
            _emit_dropped(history_msgs)
            return [system_msg] + history_msgs, truncated_count

    # Still over budget after all dialogues tried — keep only last dialogue
    cut_idx = user_indices[-1]
    truncated_count = cut_idx
    history_msgs = history_msgs[cut_idx:]
    _apply_context_wall(system_msg, history_msgs, truncated_count, store, topic_id)
    _emit_dropped(history_msgs)
    return [system_msg] + history_msgs, truncated_count


# 正在后台整理的区间（防重复调度）。整理本身是"减少未来回读"的优化，
# 失败了也只是回退到"照旧丢弃"，绝不能因此让本轮请求红掉。
_ORGANIZE_INFLIGHT = set()
_ORGANIZE_LOCK = threading.Lock()
_ORGANIZE_MAX_CHARS = 40000   # 一次整理的原文上限，超出按最早的一段截
_CT_PIN_MAX = 6               # 裁剪时固定保留的整理摘要个数（最近的 N 个）
_ORGANIZE_MAX_ITEMS = 200    # 单次整理区间上限（锚点数）。超了只整理最早的一段：
                              # 摘要 LLM 的输入被截到 24000 字，标着「2316 项」的节点
                              # 实际只总结了最早一小段 —— 覆盖区间与摘要内容不符，
                              # 等于静默丢信息（2026-10-22 实测）。


def _schedule_organize_dropped(topic_id, dropped):
    """把「超预算被挤出上下文」的整轮对话交给后台整理，而不是让它凭空消失。

    为什么异步：整理要调一次 LLM（几秒到几十秒）。放在 /api/chat 的发送路径上，
    用户会看到"点了发送，转圈半分钟才开始回复"。这里只登记区间、起个守护线程，
    本轮 AI 仍按原逻辑用 read_topic_messages 回查，下一轮整理树摘要就已就位。

    为什么值得：原文一旦被丢，AI 以后要靠多次工具往返才能捞回来（每次往返重发
    system+工具定义 ≈ 3 万 token）。整理一次 = 一次 LLM 调用，能省掉未来 N 次回读。
    """
    try:
        if not topic_id or not dropped:
            return
        # 已被整理树覆盖的虚拟摘要不能再整理一遍
        anchors = [m.get("_anchor") for m in dropped
                   if isinstance(m.get("_anchor"), int) and not m.get("_ct_node")]
        if len(anchors) < 5:          # 与 MIN_COMPRESSION_ITEMS 对齐
            return
        a0, a1 = min(anchors), max(anchors)
        # 区间闸：超过上限只整理最早的一段。剩下等下一轮被挤出时再整理，
        # 否则「一个节点盖住两千项、摘要只写了前 40 项」比不整理更误导。
        if a1 - a0 + 1 > _ORGANIZE_MAX_ITEMS:
            _cap_a1 = a0 + _ORGANIZE_MAX_ITEMS - 1
            print(f"[auto-organize] {str(topic_id)[:8]} 区间 [{a0},{a1}] 过大（{a1-a0+1} 项），"
                  f"本轮只整理 [{a0},{_cap_a1}]")
            a1 = _cap_a1

        # 体积闸：一次吞 4 万字原文已经够生成高质量摘要，再多只是烧输入 token
        total = 0
        for m in dropped:
            total += len(m.get("content") or "")
            if total > _ORGANIZE_MAX_CHARS:
                print(f"[auto-organize] {str(topic_id)[:8]} 区间过长({total}字)，仍按 [{a0},{a1}] 整理")
                break

        key = (topic_id, a0, a1)
        with _ORGANIZE_LOCK:
            if key in _ORGANIZE_INFLIGHT:
                return
            _ORGANIZE_INFLIGHT.add(key)

        def _run():
            try:
                from tools.memory_tools import compress_context
                r = compress_context(topic_id=topic_id, anchor_start=a0, anchor_end=a1,
                                     purpose="归档：预算超限被挤出上下文的历史轮次",
                                     allow_fallback=False)
                print(f"[auto-organize] {str(topic_id)[:8]} [{a0},{a1}] -> {str(r)[:160]}")
            except Exception as _e:
                print(f"[auto-organize] {str(topic_id)[:8]} [{a0},{a1}] 失败: {_e}")
            finally:
                with _ORGANIZE_LOCK:
                    _ORGANIZE_INFLIGHT.discard(key)

        threading.Thread(target=_run, daemon=True, name="auto-organize").start()
    except Exception as _e:
        print(f"[auto-organize] 调度失败: {_e}")

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
            msgs_with_system, trim_target, store=store, topic_id=topic_id,
            on_dropped=lambda _d: _schedule_organize_dropped(topic_id, _d),
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


_DYN_PREFIX = ("【系统注入·非用户发言】以下是本轮的环境与状态快照"
                "（时间 / 任务图 / 记忆 / 上下文用量），照它判断处境；"
                "不要当成用户说的话，也不要直接复述它。")

# 只保留真正恒定的两组：身份规则 + 工具知识。其余（当前任务/记忆与历史/
# 系统环境/行动依据/上下文状态/专注模式/规则提醒/未知段）一律下沉到末尾。
_SYS_STATIC_SECTIONS = ("━━━ 身份与规则 ━━━", "━━━ 工具与知识 ━━━")


def _split_system_dynamic(system_msg):
    """把 system 里「每轮都会变」的段落剥离出来，交给调用方拼到 messages 末尾。

    🔴 为什么必须拆（2026-10-21 实测，Temp/_zz_cache_real.py）：
       DeepSeek 的上下文缓存是**前缀匹配**，而 tools schema（约 11k tok）排在 system 之后。
       system 里只要有**一个字节**变化 —— 【现在】的毫秒时间戳、【任务流程图】的节点计数、
       「📊 N 条消息在上下文中」的统计行 —— 工具定义就得整体重算。
       实测：只改统计行里一个数字，命中率 99.6% → 26.6%，单次往返多付 11,264 tok。
       按 flash 峰值价约 0.033 元/往返，一次 8 轮往返的任务白白多花 0.26 元；
       预填充耗时同时从 2.0s 涨到 5.4s（缓存命中时几乎不重算）。

       拆法：system 只留恒定的「身份与规则」「工具与知识」，其余全部下沉为
       messages 末尾的一条独立消息（不落库，每轮重建）。末尾离生成最近，
       注意力反而更强，不是降级 —— 只是把"规则"和"当下状态"分成两类放。

    返回 (static_system_msg, dynamic_text)。dynamic_text 为空表示无需拆分（保持原样）。
    """
    if not isinstance(system_msg, dict) or system_msg.get("role") != "system":
        return system_msg, ""
    content = system_msg.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        return system_msg, ""
    _SEP = "\n\n" + "═" * 15 + "\n\n"
    parts = content.split(_SEP)
    keep, moved = [], []
    for p in parts:
        head = p.lstrip().split("\n", 1)[0][:40]
        if any(head.startswith(s) for s in _SYS_STATIC_SECTIONS):
            keep.append(p)
        else:
            moved.append(p)
    # 没有可动的段，或没有可留的段 —— 都保持原样，绝不把 system 掏空
    if not moved or not keep:
        return system_msg, ""
    _static = {**system_msg, "content": _SEP.join(keep)}
    return _static, "\n\n".join(moved)


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
            f"📖 更早的消息不在上下文中。"
            f"找具体内容: read_topic_messages(topic_id='{str(topic_id)[:8]}', query='要找回的那件事')"
            f"（语义命中，不用逐页翻）；顺序翻: 同上 + more=True。看完记得 organize_context。"
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


def get_task_model_config(tid):
    def resolve(provider, model, old):
        urls = {'anthropic': 'https://api.anthropic.com',
                'openai': 'https://api.openai.com/v1',
                'deepseek': 'https://api.deepseek.com',
                'ollama': 'http://localhost:11434/v1',
                'llamacpp': local_llm.base_url()}
        if provider in ('llamacpp', 'ninfer'):
            profile = local_llm.resolve_profile(model)
            if not profile:
                raise ValueError(f'本地模型未配置槽位: {model}')
            cfg = local_llm.get_config()
            target = next(p for p in cfg.get('profiles', []) if p.get('name') == profile)
            return dict(provider=provider, model=model,
                        base_url=f"http://127.0.0.1:{int(target.get('port') or cfg.get('port') or 8080)}/v1",
                        api_key='', protocol='openai')
        cp = _custom_provider(provider)
        if cp:
            return dict(provider=provider, model=model, base_url=cp['base_url'],
                        api_key=cp.get('api_key', ''), protocol=cp.get('protocol', ''),
                        extra_headers=cp.get('extra_headers', {}))
        # Registered fallback configs retain their own credentials and protocol.
        with open(os.path.join(ROOT_DIR, 'model_config.json'), encoding='utf-8') as f:
            _raw = json.load(f)
        for item in _raw.get('fallback_chain', []):
            if item.get('provider') == provider:
                return dict(provider=provider, model=model,
                            base_url=item.get('base_url') or urls.get(provider, ''),
                            api_key=item.get('api_key') or _get_provider_key(old, provider),
                            protocol=item.get('protocol', ''), extra_headers=item.get('extra_headers', {}))
        if provider not in urls and provider != old.provider:
            raise ValueError('Unknown model provider')
        return dict(provider=provider, model=model,
                    base_url=old.base_url if provider == old.provider else urls.get(provider, ''),
                    api_key=_get_provider_key(old, provider))
    return task_runtime.snapshot(get_model_config(), task_runtime.load_selection(get_db(), tid), resolve)


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
            # 第三方源可能用 Anthropic 协议或要求额外请求头 —— 不落盘的话
            # 重启后对话会退回 OpenAI 协议打同一地址，直接 404
            cfg["protocol"] = getattr(_model_config, "protocol", "openai")
            cfg["extra_headers"] = getattr(_model_config, "extra_headers", {}) or {}
            # 保存 fallback_chain（含每个 provider 的 api_key），否则前端输入的 key 不持久化
            cfg["fallback_chain"] = _model_config.fallback_chain
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[config] Failed to save model_config.json: {e}")

# ── Dynamic model list ────────────────────────────

# Cached model lists per provider: {provider: (timestamp, [models], ttl)}
# 成功结果缓存 1 小时；**失败也要缓存**（短 TTL）——否则每次打开模型面板都要
# 重新等一次网络超时（urlopen timeout=10），表现为"点开就卡十几秒"。
_model_list_cache = {}
_MODEL_CACHE_TTL = 3600   # 1 hour（成功）
_MODEL_FAIL_TTL = 60      # 拉取失败时的负缓存（秒）

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
    if cached:
        _ts, _models, _ttl = cached
        if (now - _ts) < _ttl:
            return _models

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
        _model_list_cache[provider] = (now, models, _MODEL_CACHE_TTL)
        return models
    except Exception as e:
        print(f"[models] Failed to fetch from {provider}: {e}")
        # 负缓存：短时间内不再重试，避免每次请求都重新等一遍网络超时
        _model_list_cache[provider] = (time.time(), [], _MODEL_FAIL_TTL)
        return []


def _get_provider_key(cfg, provider: str) -> str:
    """获取指定 provider 的 api_key：主 config 优先 → 自定义源 → fallback_chain。"""
    if cfg.provider == provider:
        return cfg.api_key
    try:
        rt = custom_providers.runtime_config(ROOT_DIR, provider)
        if rt and rt.get("api_key"):
            return rt["api_key"]
    except Exception:
        pass
    for item in cfg.fallback_chain:
        if item.get("provider") == provider:
            return item.get("api_key", "")
    return ""


def _custom_provider(provider: str):
    """取一个第三方源的运行时配置（不存在返回 None）。"""
    try:
        return custom_providers.runtime_config(ROOT_DIR, provider)
    except Exception:
        return None


def _fallback_llm_config(cfg, tried):
    """主模型持续不可用时，按 fallback_chain 取下**一个**备用源的 LLMConfig。

    返回 (new_cfg, label)，没得可用就返回 (None, None)。

    【为什么要有这个】`fallback_chain` 早在 model_config.json 里配着一串备用源，
    但 2026-09-25 之前它**只被用来「切 provider 时取 api_key」**，从来没在对话失败时
    被当过回退 —— 所以"loop 级重连"看着做了、实际不生效：主模型一 503，
    整轮直接抛异常干掉整个循环。

    tried: 已试过的 "provider/model" 集合（调用方维护），避免在多个坏源间来回打转。
    """
    for item in (cfg.fallback_chain or []):
        provider = (item.get("provider") or "").strip()
        model = (item.get("model") or "").strip()
        if not provider or not model:
            continue
        label = f"{provider}/{model}"
        if label in tried:
            continue
        base_url = (item.get("base_url") or "").strip()
        api_key = item.get("api_key") or _get_provider_key(cfg, provider)
        protocol = "openai"
        extra_headers, extra_body = {}, {}
        # 该 provider 若是登记过的第三方源，以源上的登记值为准（协议/额外头/额外体）
        rt = _custom_provider(provider)
        if rt:
            base_url = rt.get("base_url") or base_url
            api_key = rt.get("api_key") or api_key
            protocol = rt.get("protocol") or protocol
            extra_headers = rt.get("extra_headers") or {}
            extra_body = rt.get("extra_body") or {}
        if not base_url:
            continue
        try:
            new_cfg = cfg.clone_with(provider=provider, model=model,
                                     base_url=base_url, api_key=api_key,
                                     protocol=protocol,
                                     extra_headers=extra_headers,
                                     extra_body=extra_body)
        except Exception:
            continue
        return new_cfg, label
    return None, None


# ── 视觉（图片输入）能力判定与回退 ──────────────────

def _vision_capable(cfg) -> bool:
    """当前模型能否**直接**收图。

    - 云端/云端兼容：按模型名判定（is_vision_model 的白名单 + 名字含 vision）
    - 本地（llamacpp/ollama）：不能靠名字猜 —— 直接问服务自己。
      llama.cpp 在 /props 暴露 modalities.vision，只有加载了 mmproj 才会是 true。
      所以本地模型"看起来不支持"其实经常只是没加载视觉投影文件。
    """
    try:
        if is_vision_model(cfg.model):
            return True
    except Exception:
        pass
    # 第三方源：以设置页里的「支持图片输入」开关为准。勾了就直接认，
    # 否则按模型名判（上面已判过）——不能一律回退到 DeepSeek，
    # 那会把用户的图偷偷发到别的厂商。
    cp = _custom_provider(str(getattr(cfg, "provider", "")))
    if cp is not None:
        return bool(cp.get("vision"))
    if str(getattr(cfg, "provider", "")) in ("llamacpp", "ollama"):
        try:
            return bool(local_llm.supports_vision())
        except Exception:
            return False
    return False


def _vision_fallback_config(cfg):
    """找"能收图"的备选模型。返回 (新config, 说明) 或 (None, 原因)。

    关键：必须连 provider / base_url / api_key 一起换。
    历史 bug 只换了 model 名，于是本地 provider 下图片仍被发到
    http://127.0.0.1:8901/v1，而不支持视觉的 llama-server 直接 HTTP 500
    （"image input is not supported ... you may need to provide the mmproj"）。
    """
    key = _get_provider_key(cfg, "deepseek")
    if not key:
        return None, "没有可用的 DeepSeek 密钥"
    try:
        new_cfg = cfg.clone_with(
            provider="deepseek",
            model="deepseek-v4-flash-vision-exp",
            base_url="https://api.deepseek.com",
            api_key=key,
        )
    except Exception as e:
        return None, f"构造回退配置失败: {e}"
    return new_cfg, "DeepSeek 云端视觉模型"


# 已知的多模态模型家族（本地模型拿不到可靠的 /props 时按名字兜底判断）
_LOCAL_VISION_HINTS = (
    "llava", "bakllava", "moondream", "minicpm-v", "minicpmv",
    "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen-vl",
    "internvl", "gemma3", "granite-vision", "smolvlm", "pixtral",
    "llama3.2-vision", "llama4", "glm-4v", "glm-5",
)


def _model_vision_flag(provider, model_id) -> bool:
    """模型列表里给前端的"能否看图"标记（静态判断，不含运行时探测）。"""
    try:
        if is_vision_model(model_id):
            return True
    except Exception:
        pass
    if str(provider) in ("ollama", "llamacpp", "lmstudio", "local"):
        mid = str(model_id or "").lower()
        return any(h in mid for h in _LOCAL_VISION_HINTS)
    return False


def _vision_fallback_enabled() -> bool:
    """当前模型看不了图时，是否允许自动改用云端视觉模型（默认允许）。

    隐私/离线优先的用户可以在 model_config.json 里设 "vision_fallback": false，
    这样带图请求不会把图片发到云端，只提示"当前模型看不了图"，图片仅留路径。
    """
    try:
        with open(os.path.join(ROOT_DIR, "model_config.json"), encoding="utf-8") as f:
            raw = json.load(f) or {}
        return bool(raw.get("vision_fallback", True))
    except Exception:
        return True


# ── 主模型故障处理：同源重试 + 可选自动换源 ──────────────
# 同源重试仅在本轮无任何输出/工具调用时安全；按总恢复窗口限制。
LLM_TURN_RETRY_MAX = 100
LLM_TURN_RETRY_WINDOW = 120  # 从首次可重试故障起，最多恢复 2 分钟

# 需要回传 reasoning_content 的源：{(base_url, model)}。
# 思考模型（LiteLLM 代理的 deepseek-v4.1-flash-1017 等）要求带 tool_calls 的
# assistant 消息原样回传本轮 reasoning_content，否则 HTTP 400。实测补字段即 200。
# 一旦某个源报过这个错，就记住它，后续轮次在组装消息时直接带上，不再白挨 400。
_REASONING_ECHO_MODELS = set()


def _auto_fallback_enabled() -> bool:
    """主模型持续不可用时，是否允许沿 fallback_chain 自动换源（**默认关闭**）。

    换源**有副作用**：备用源的能力与风格可能不同（不支持工具调用、不能看图、
    上下文窗口更小），中途换源可能让长任务跑偏 —— 用户 2026-09-25 明确要求
    「不是所有场景都能换，默认关闭，由我在设置里按任务决定」。
    所以：只认 `model_config.json` 里显式的 `auto_fallback: true`；
    缺键 / 文件读不到 / 值非法 一律当**关闭**（宁可中断并给出可读错误，也不偷偷换模型）。

    关掉后：只做同源重试（`LLM_TURN_RETRY_MAX` 次），再失败就中断并在对话里留下可读说明。

    直接读文件不快照：本函数只在"重试耗尽"这一罕见路径上调用，
    换来的好处是改完立即生效、无需失效缓存。
    """
    try:
        with open(os.path.join(ROOT_DIR, "model_config.json"), encoding="utf-8") as f:
            raw = json.load(f) or {}
        return raw.get("auto_fallback") is True
    except Exception:
        return False


def _prepare_vision_request(llm_config, message, image_abs_paths):
    """决定"带图请求"怎么发。返回 (llm_config, message, image_content, note)。

    - 当前模型能收图（本地已加载 mmproj / 云端视觉模型）
        → 原样用，image_content = 多模态 content 数组
    - 当前模型不能收图且允许云端回退
        → 换到 DeepSeek 云端视觉模型（provider / base_url / api_key **一起换**），
          image_content = 多模态内容，message 里追加一句说明
    - 都不能（没 key 或用户关了回退）
        → image_content = None，图片不外发；message 里说明"只能给路径"

    抽成独立函数是为了能脱离 HTTP/DB 直接单测 —— 这段逻辑出过 bug
    （只换 model 名不换 base_url，导致图片打到不支持视觉的本地服务 → HTTP 500）。
    """
    note = ""
    if _vision_capable(llm_config):
        content = build_multimodal_content(message, image_abs_paths)
        return llm_config, message, content, note

    enabled = _vision_fallback_enabled()
    if enabled:
        fb, why = _vision_fallback_config(llm_config)
    else:
        fb, why = None, "已关闭云端图片回退（vision_fallback = false）"

    if fb is not None:
        old_model = llm_config.model
        note = (f"[图片识别] 当前模型 {old_model} 不支持图片输入，"
                f"本次已自动改用 {why}。")
        message = (message + "\n" + note).strip()
        print(f"[vision] fallback {old_model} -> {why}")
        return fb, message, build_multimodal_content(message, image_abs_paths), note

    note = (f"[图片识别] 当前模型 {llm_config.model} 不支持图片输入，且{why}；"
            f"图片未发送给模型（仅提供文件路径，可用 OCR 工具读取图中文字）。")
    message = (message + "\n" + note).strip()
    print(f"[vision] no fallback: {why}")
    return llm_config, message, None, note


# ── 最近使用的模型（输入框模型面板置顶用）─────────────────
# 存在 meta 全局键里：跨任务、跨设备共享（手机端也看得到同一条最近列表）。
# 只存 provider + model id 两个字段，渲染时再跟当前模型列表对齐 ——
# 源被删/模型下线时自动从最近列表里消失，不会留下点不动的死条目。
_RECENT_MODELS_KEY = "recent_models_v1"
_RECENT_MODELS_MAX = 6


def _load_recent_models(db):
    try:
        raw = db.get_meta(_RECENT_MODELS_KEY)
        arr = json.loads(raw) if raw else []
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def _push_recent_model(db, provider, model):
    """把刚选的模型挪到最近列表最前（去重，最多 6 条）。"""
    arr = [x for x in _load_recent_models(db)
           if not (isinstance(x, dict) and x.get("provider") == provider and x.get("model") == model)]
    arr.insert(0, {"provider": provider, "model": model})
    arr = arr[:_RECENT_MODELS_MAX]
    try:
        db.set_meta(_RECENT_MODELS_KEY, json.dumps(arr, ensure_ascii=False))
    except Exception as e:
        print(f"[models] save recent failed: {e}")
    return arr


def _recent_model_view(db, models):
    """把最近记录映射成完整模型对象（含 name/available/current），过滤已失效的。"""
    by = {}
    for m in models or []:
        by[(m.get("provider"), m.get("id") or m.get("model"))] = m
    out = []
    for r in _load_recent_models(db):
        m = by.get((r.get("provider"), r.get("model")))
        if m and m not in out:
            out.append(m)
        if len(out) >= _RECENT_MODELS_MAX:
            break
    return out


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
            "vision": _model_vision_flag("deepseek", m["id"]),
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
            "vision": _model_vision_flag("deepseek", cfg.model),
            "current": True,
        })

    # Ollama: local models — always probe localhost so user can switch even
    # when current provider is something else.
    _OLLAMA_DEFAULT_URL = "http://localhost:11434/v1"
    ollama_models = _fetch_provider_models("ollama", _OLLAMA_DEFAULT_URL, "")
    _ollama_up = bool(ollama_models)  # 拉到列表即视为服务在跑（只探活，不拉起）
    for m in ollama_models:
        result.append({
            "provider": "ollama",
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "desc": m.get("desc", "本地模型"),
            "available": True,
            "local": True,
            "running": _ollama_up,
            "vision": _model_vision_flag("ollama", m["id"]),
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
            "vision": _model_vision_flag("ollama", cfg.model),
            "current": True,
        })

    # llama.cpp 本地服务（多槽位：Qwen3.8-27B @8901 / Ternary-Bonsai-27B @8080）
    # 每个槽位一条目；激活槽真实探活（running/starting），非激活槽只报配置态。
    # available 恒为 True（前端据此弹窗拉起）。
    try:
        _lc = local_llm.get_config()
        _ls = local_llm.probe()
        _lact = str(_lc.get("active_profile") or "")
        _lprofs = [p for p in (_lc.get("profiles") or []) if isinstance(p, dict)]
        for _p in _lprofs:
            _pname = str(_p.get("name") or "")
            _is_act = (_pname == _lact) or (not _lact and _p is _lprofs[0])
            _lid = str(_p.get("model_id") or _pname)
            _lname = str(_p.get("display") or _pname)
            if _is_act:
                _lctx = _ls.get("live_ctx") or _p.get("ctx_size") or _lc.get("ctx_size")
                _lstate = ("运行中" if _ls.get("running")
                           else ("加载中…" if _ls.get("starting") else "未启动"))
                _lvision = bool(_ls.get("vision"))
                _desc = f"本地GPU推理 · 端口 {_p.get('port')} · 上下文 {_lctx} · {_lstate}"
                _desc += " · 已启用视觉" if _lvision else " · 仅文本"
            else:
                _lctx = _p.get("ctx_size")
                _lvision = bool(str(_p.get("mmproj") or "").strip())
                _cfg_ok = bool(str(_p.get("model_path") or "").strip())
                _desc = f"本地GPU推理 · 端口 {_p.get('port')} · 上下文 {_lctx}"
                _desc += "" if _cfg_ok else " · 未配置路径"
                _desc += " · 切换时自动拉起" if _cfg_ok else ""
            result.append({
                "provider": "llamacpp",
                "id": _lid,
                "name": _lname,
                "desc": _desc,
                "available": True,
                "local": True,
                "profile": _pname,
                "active_profile": _is_act,
                "running": bool(_ls.get("running")) if _is_act else False,
                "starting": bool(_ls.get("starting")) if _is_act else False,
                "ctx_size": _lctx,
                "vision": _lvision,
                "vision_hint": (_ls.get("vision_hint") or "") if _is_act else "",
                "mmproj": (_ls.get("mmproj_path") or "") if _is_act else str(_p.get("mmproj") or ""),
                "current": cfg.provider == "llamacpp" and cfg.model == _lid,
            })
    except Exception as e:
        print(f"[models] local llama.cpp probe failed: {e}")
        result.append({
            "provider": "llamacpp",
            "id": "qwen3.8-27b-iq4xs", "name": "Qwen3.8 27B (Local GPU)",
            "desc": "本地GPU推理~23tok/s,无需联网",
            "available": True, "local": True, "running": False,
            "current": cfg.provider == "llamacpp" and cfg.model == "qwen3.8-27b-iq4xs",
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

    # ── 第三方源（用户在设置页自己添加的任意兼容端点，数量不限）──
    # 放在最后：内置源开箱可用，第三方属于进阶配置。
    # 每个源的模型单独成组（前端用 group 字段显示源的显示名）。
    try:
        for cp in custom_providers.load_all(ROOT_DIR):
            if not cp.get("enabled", True):
                continue
            proto_label = "Anthropic" if cp["protocol"] == "anthropic" else "OpenAI 兼容"
            has_key = bool(cp["api_key"])
            listed_current = False

            def _row(mid, disp=""):
                return {
                    "provider": cp["id"],
                    "id": mid,
                    "name": disp or mid,
                    "desc": f"{cp['name']} · {proto_label}",
                    "available": has_key,
                    "custom": True,
                    "group": cp["name"],
                    "protocol": cp["protocol"],
                    "vision": bool(cp.get("vision")) or _model_vision_flag(cp["id"], mid),
                    "current": cfg.provider == cp["id"] and cfg.model == mid,
                }

            for m in cp["models"]:
                row = _row(m["id"], m.get("name") or "")
                listed_current = listed_current or row["current"]
                result.append(row)
            # 正在用这个源、但模型已不在清单里（用户改过配置/在别处切过）
            # → 补一条，否则"当前模型"卡片有名字、列表里却找不到它
            if cfg.provider == cp["id"] and not listed_current:
                result.append(_row(cfg.model, f"{cfg.model} (当前)"))
    except Exception as e:
        print(f"[models] custom providers failed: {e}")

    return result
def get_ips(port):
    ips = []
    # 公网地址来源：①「设置 → 公网访问」里配的（tunnel_config.json，新的唯一来源）
    #                ② server_config.json 的 public_ip（向后兼容，没启用公网访问时才看）
    # 值可以是纯 IP/域名（自动拼端口）或完整 URL（含 http:// 前缀，用于端口映射场景）
    pub = ""
    try:
        if tunnel_manager is not None:
            tcfg = tunnel_manager.load_config()
            if tcfg.get("enabled"):
                pub = tunnel_manager.public_url(tcfg)
    except Exception:
        pub = ""
    if not pub:
        try:
            with open(os.path.join(ROOT_DIR, "server_config.json"), encoding="utf-8") as f:
                pub = (json.load(f) or {}).get("public_ip") or ""
        except Exception:
            pass
    pub = pub.strip()
    if pub:
        if "://" in pub:
            ips.append(pub)
        else:
            ips.append(f"http://{pub}:{port}")
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


def _peek_injection(tid):
    """该话题是否有待处理的插话（**只看不取走**）。

    [2026-10-08] 供 ask_user 阻塞等待时提前退出用。
    为什么必须要有：插话只在**轮次边界**被 drain（最终轮 / 工具轮之后），
    而 ask_user 是阻塞在工具轮**里面**的 —— 它不返回，轮次就到不了边界。
    于是"用户不点提问卡片、直接发一条新消息"会变成：消息进了队列却永远没人取，
    agent 又永远等卡片 ⇒ 两边一起卡死。
    旧实现靠 ask_user 的 5 分钟超时"顺带"把循环放回边界，才掩盖了这个死锁。
    这里只窥视、**绝不 drain**：插话必须原样留给轮次边界消费，
    否则这里取走了，用户那条消息就凭空消失了。
    """
    if not tid:
        return False
    with _inject_lock:
        return bool(_inject_queues.get(tid))


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


# 终态（回合结束）标记：仅在 /api/topics 里暴露给侧栏绿/红点，
# /api/working 仍返回 idle（否则前端会把它当"处理中"，重建幽灵实时泡）。
_TERMINAL_STATUS = ("done", "failed", "cancelled")
TERMINAL_TTL = 180.0  # 秒：终态点在侧栏保留时长，过期自动清


def _purge_expired_locked(now):
    """清理过期终态（须持锁调用）。"""
    dead = [t for t, w in _current_working.items()
            if w.get("status") in _TERMINAL_STATUS and now - (w.get("ts") or 0) > TERMINAL_TTL]
    for t in dead:
        _current_working.pop(t, None)


def finish_working(tid, status="done"):
    """回合收尾写终态：done=正常结束/用户停止，failed=异常。
    /api/topics 用它点亮侧栏绿/红点；TTL 到期自动清，避免常亮。"""
    with _working_lock:
        prev = _current_working.get(tid) or {}
        _current_working[tid] = {
            "status": status,
            "thinking": "", "intermediate": "", "response": "",
            "tool_calls": [], "turn": prev.get("turn", 0),
            "remaining": None, "ts": time.time(),
        }
    # 终态同样解绑线程上下文，避免 keep-alive 复用线程时残留归属
    try:
        from tools.task_context import clear_if_matches
        clear_if_matches(tid)
    except Exception:
        pass


def get_working_all():
    now = time.time()
    with _working_lock:
        _purge_expired_locked(now)
        return dict(_current_working)

def _loop_active_for(tid):
    """话题是否有正在跑的 agent 循环（比 _current_working 的状态更宽的窗口）。

    _cancel_events 是循环登记的第一件事，循环收尾或异常兜底（_handle_chat 的
    finish_working / pop、以及 do_POST 外层 except）才摘掉；而第一次 set_working
    要等上下文压缩完 —— 那一段可能调 LLM，几十秒。这段时间 /api/working 只能报
    idle，于是"刚发完就重开 / 切走再切回"的窗口里，界面会把"AI 明明在跑"看成空闲
    （运行点不亮、停止按钮变回可发送）。运行点与停止按钮用这个更宽的判据。
    只读 _cancel_events；先算再进 _working_lock，避免两把锁嵌套。
    """
    try:
        with _cancel_lock:
            return tid in _cancel_events
    except Exception:
        return False


def get_working(tid):
    now = time.time()
    _la = _loop_active_for(tid)
    with _working_lock:
        _purge_expired_locked(now)
        w = _current_working.get(tid)
        if w and w.get("status") not in _TERMINAL_STATUS:
            w["loop_active"] = _la
            return w
    return {"status": "idle", "thinking": "", "intermediate": "", "response": "", "tool_calls": [], "turn": 0, "remaining": None, "loop_active": _la}

def clear_working(tid):
    with _working_lock:
        _current_working.pop(tid, None)
    # 任务生命周期结束：解绑线程上下文，避免 keep-alive 复用线程时残留归属
    try:
        from tools.task_context import clear_if_matches
        clear_if_matches(tid)
    except Exception:
        pass


# ── 未命名任务的标题兜底（纯显示层）────────────────────────────
# 场景：我（AI）还没抓出意图命名（title 仍是 '新任务'），但用户已经提问了。
# 此时侧栏/标题栏不该只显示"新任务"——一堆任务全同名，用户分不清谁是谁。
# 用用户第一条真正的提问内容做**显示名**，截断到 24 字。
#
# 铁律：只加 display_title 字段，**绝不改 db.title**。原因：
#   1. rules_engine/rules.json 的 topic_unnamed 规则靠 topic_title == '新任务'
#      触发"此任务未命名，先抓意图"的提醒；改了 title 这条规则就永远不响了。
#   2. 我判断任务身份也看 title，不能被显示层污染。
# 一旦有正式标题，前端优先用 title，display_title 自动失效（无需清理）。
_TITLE_DEFAULT = ("", "新任务", "新话题", "任务", "未命名")
_TITLE_MAX = 24


def is_default_title(title):
    """标题是否仍是"没身份"的默认值。"""
    return str(title or "").strip() in _TITLE_DEFAULT


_ATTACH_RE = re.compile(r"\[附件[^\]]*\]|\[[^\]]*\.(?:png|jpe?g|gif|webp|bmp|mp4|mp3|wav|pdf|docx?|xlsx?|zip|txt)(?:\s*->[^\]]*)?\]\s*", re.I)


def clip_title(text, limit=_TITLE_MAX):
    """压空白 + 剥掉附件标记 + 超长截断（带省略号）。"""
    s = _ATTACH_RE.sub(" ", str(text or ""))
    s = " ".join(s.split())
    if not s:
        return ""
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def pick_title_text(msg_list):
    """从用户消息里挑第一条能当标题的（跳过纯附件/图片/提醒占位）。"""
    for m in (msg_list or []):
        s = clip_title(m)
        if not s:
            continue
        if s.startswith("⏰"):
            continue
        return s
    return ""


def attach_display_title(topics, db=None):
    """给仍是默认标题的任务补一个 display_title（只读、不落库）。

    只查 title 为默认值的那几个任务，命中索引，开销可忽略。
    """
    try:
        need = [t for t in (topics or []) if is_default_title(t.get("title"))]
        if not need:
            return topics
        db = db or get_db()
        m = db.first_user_messages([t.get("id") for t in need])
        for t in need:
            txt = pick_title_text(m.get(t.get("id")))
            if txt:
                t["display_title"] = txt
    except Exception:
        pass
    return topics


def attach_action_needed(topics):
    """给「AI 正在等用户回答」的任务打 action_needed 标记（侧栏黄底黑字标签用）。

    目前只有一种 kind：ask = ask_user 阻塞式提问等待回答。前端 /api/topics 拿到后
    用它**替代**运行点（同一条任务右上角只显示一个状态），所以是独立字段而非
    working_status 的一种取值 —— working_status 会被 /api/working 的 idle 判定干扰。
    """
    try:
        from tools.ask_user import pending_topic_ids
        ids = pending_topic_ids()
        if ids:
            for t in (topics or []):
                if t.get("id") in ids:
                    t["action_needed"] = "ask"
    except Exception:
        pass
    return topics


# ── 公网隧道：由大眼本体接管保活 ─────────────────────
# 链路：公网 <server_addr>:<remote_port> --frpc--> 127.0.0.1:<guard_port>(守门员)
#                                          --> 127.0.0.1:<app_port>(本服务)
#
# 2026-09-20 架构变更：以前靠登录自启的 dayan_supervisor.py 独立保活，但它被
# "批量杀 python 进程"那类清理带走之后就再没东西拉起守门员 —— 而 frpc 还活着、
# 只是一直 connect refused，症状极迷惑（本机一切正常、公网打不开）。
# 现在改由大眼本体接管：下面这个巡检线程缺谁补谁；配置也统一到 tunnel_config.json
# （以前散落在 frpc.toml / tokens.txt / server_config.json 三处）。
#
# 零子进程巡检（socket 探端口 + psutil 枚举）：server 常驻 pythonw 无控制台，
# fork 任何命令行都会在屏幕上闪黑窗。关掉：server_config.json 里 "tunnel_guard": false。
_TUNNEL_WATCH_INTERVAL = 30.0
_tunnel_wd = {"checks": 0, "heals": 0, "disabled": False, "legacy_checked": False}


def _tunnel_log(msg):
    try:
        path = os.path.join(ROOT_DIR, "data", "tunnel_watchdog.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _tunnel_guard_enabled():
    try:
        with open(os.path.join(ROOT_DIR, "server_config.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("tunnel_guard", True) is not False
    except Exception:
        return True


def _tunnel_watchdog_loop():
    """每 30s 巡检公网链路：配置开着就缺谁补谁，配置关着就静默待命。

    只在"真动手"时写日志，不刷屏。零子进程（socket 探端口 + psutil 枚举）。
    """
    time.sleep(8)                       # 稍等启动高峰（此时正忙着预热缓存/拉服务）
    if tunnel_manager is None:
        _tunnel_log(f"tunnel_manager 导入失败，公网保活停用: {_TM_IMPORT_ERR}")
        return
    while True:
        try:
            if _tunnel_wd["disabled"]:
                return
            if not _tunnel_guard_enabled():
                _tunnel_log("server_config.json 里 tunnel_guard=false，隧道保活停用")
                _tunnel_wd["disabled"] = True
                return
            _tunnel_wd["checks"] += 1
            cfg = tunnel_manager.load_config()
            if not cfg.get("enabled"):
                pass                    # 用户没开公网访问：什么都不做
            else:
                # 首次巡检：清退旧架构的独立守护（开机自启那个）—— 它已被大眼取代，
                # 留着会让"谁在管链路"含糊，而且它自己被杀就又没人管了。
                # ⚠️ 只有**真清到了**（n>0）才需要 fresh 重启：那种情况链路还挂在旧目录
                # （可能是 data/missions/... 深目录，凭证被当垃圾清掉就永久断网），必须
                # 停掉重起、让它改用项目内 tunnel/ 的脚本与凭证。
                # n==0 说明链路本来就在 tunnel/，或压根还没起（开机自启场景）——此时
                # fresh 会先去"停"一个不存在的东西并等它超时，实测把首次巡检卡死很久，
                # 直接交给下面的"缺谁补谁"即可。
                if not _tunnel_wd["legacy_checked"]:
                    _tunnel_wd["legacy_checked"] = True
                    n = tunnel_manager.kill_legacy_supervisor()
                    if n:
                        _tunnel_log(f"首次接管：清退旧守护 {n} 个，链路切换到 tunnel/ 目录（会闪断几秒）")
                        tunnel_manager.ensure_running(cfg, fresh=True)
                    else:
                        _tunnel_log("首次巡检：无旧守护，链路沿用 tunnel/ 目录")
                st = tunnel_manager.status(cfg)
                missing = [name for name, key in (("守门员", "guard"), ("frpc", "frpc"))
                           if not st[key]["running"]]
                if missing:
                    _tunnel_wd["heals"] += 1
                    tunnel_manager.ensure_running(cfg)
                    _tunnel_log(f"[第{_tunnel_wd['heals']}次] 缺 {'/'.join(missing)} -> 已拉起")
        except Exception as e:
            _tunnel_log(f"巡检异常: {type(e).__name__}: {e}")
        time.sleep(_TUNNEL_WATCH_INTERVAL)





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


def _accumulate_cost(tid, input_tokens, output_tokens, cache_hit=0, model="", settlement_id=None):
    """精确估价（CNY）累计到 topic + global。total_cost 字段存 CNY。"""
    global _total_cost_cny, _total_tokens_global
    cny = _cost_cny(input_tokens, output_tokens, cache_hit, model)
    try:
        settlement_id = settlement_id or uuid.uuid4().hex
        db = get_db()
        applied = db.settle_topic_usage(tid, settlement_id, cny, input_tokens + output_tokens)
        if not applied:
            return
    except Exception as e:
        print(f"[cost] task settlement failed for {tid}: {e}")
        return
    with _cost_lock:
        _total_cost_cny += cny
        _total_tokens_global += (input_tokens + output_tokens)


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
# 任务级余额缓存：顶栏在任务里显示的是「该任务钉住的源」的余额。
# 不能用单槽缓存 —— 多任务交替切模型时两个源会互相盖掉对方的数。
# TTL 比全局短（30s）：切模型后要很快看到新源的数。
_balance_task_cache = {}
_BALANCE_TASK_TTL = 60
_total_spent_cny = 0.0
_last_balance_cny = 0.0
_balance_provider = None  # 余额基线所属供应商；切换供应商时重置基线不累计消费
_balance_currency = ""    # 基线币种：非人民币源的余额差不能混进 CNY 消费口径


def _merge_balance_auth(pid, sent):
    """把「表单回传的平台凭据」合并进已存值，返回要写盘的 balance_auth。

    这是**唯一的合并口子**：通用 save 与专用 save_balance_auth 都必须走它。
    曾经两条路语义不一致（save 直接透传 = 整体替换），而表单里「用户 ID」是
    回显的 —— 于是用户每点一次「保存」（改备注 / 改模型 / 习惯性确认都算），
    都会用只带 user_id 的载荷把已存的 system_token 整体抹掉。症状：
    "我明明填过、都查到了，今天看凭据又没了要重新填"（2026-09-25 实测复现）。

    语义（与 api_key 同等待遇，理由一样：框里不回显敏感值）：
      None / 非 dict → 不动（返回 None，由 upsert 的"只覆盖非 None"兜住）
      {}            → 显式清空（前端点「清除平台凭据」才发这个）
      非空 dict     → 部分更新：只覆盖确实给到的字段
    带「•」的 system_token 是脱敏回显，不许当新值写进去。
    """
    if not isinstance(sent, dict):
        return None
    if not sent:
        return {}
    old = {}
    if pid:
        try:
            old = dict((custom_providers.get(ROOT_DIR, pid) or {}).get("balance_auth") or {})
        except Exception:
            old = {}
    _t = str(sent.get("system_token") or sent.get("access_token") or "").strip()
    if _t and "•" not in _t:          # 带「•」的是脱敏回显，不是新值
        old["system_token"] = _t
    _u = str(sent.get("user_id") or "").strip()
    if _u:
        old["user_id"] = _u
    # 续期令牌（LeaderAI 这类会轮换登录令牌的站点用）：同样"框里空着 = 不改"
    _r = str(sent.get("refresh_token") or "").strip()
    if _r and "•" not in _r:
        old["refresh_token"] = _r
    return old


def _balance_auth_for(cfg):
    """当前源要不要「平台管理凭据」。

    DMXAPI 这类平台的余额接口不吃模型密钥（sk-…），只认账号后台的
    「系统访问令牌 + 用户 ID」。命中且两样齐全才返回 dict，否则 None ——
    不传就没这回事，其他源的查询路径完全不变。

    先按 provider id 匹配（用户可能直接把 provider 选成 dmxapi），
    再按地址兜底（换了名字但地址同一个站点）。

    判据是「有没有**任何**一份令牌」，不再硬要求 system_token + user_id 两件齐全：
      · LeaderAI 只用主站登录令牌（没有用户 ID 这种东西），按老判据永远返回 None；
      · DMX 缺 user_id 时也应该把凭据交下去 —— 它的适配器会给出
        「还缺用户 ID」的准话，比在这里静默当成"没凭据"有用得多。
    """
    try:
        items = custom_providers.load_all(ROOT_DIR)
    except Exception:
        return None
    pid = str(getattr(cfg, "provider", "") or "")
    base = str(getattr(cfg, "base_url", "") or "").rstrip("/")
    for it in items:
        same_id = bool(pid) and it.get("id") == pid
        same_url = bool(base) and base in (
            str(it.get("api_base") or "").rstrip("/"),
            str(it.get("base_url") or "").rstrip("/"))
        if not (same_id or same_url):
            continue
        a = it.get("balance_auth") or {}
        if a.get("system_token") or a.get("refresh_token") or a.get("user_id"):
            return a
    return None


def _persist_balance_auth(pid, upd):
    """把适配器续期换到的**新令牌**写回源配置。

    为什么必须落盘：LeaderAI 的登录令牌是 JWT、带 exp，主站在用户每次打开网页时
    都会轮换。不写回的话，我们手里那张过期后就再也续不上 —— 症状是顶栏积分
    某天开始静默变成「—」，用户完全不知道发生了什么。
    只写 balance_auth 这一个字段（走 upsert 的部分更新），源的名称/模型/密钥一律不动。
    """
    if not pid or not isinstance(upd, dict) or not upd:
        return
    try:
        cur = custom_providers.get(ROOT_DIR, pid)
        if not cur:
            return
        merged = dict(cur.get("balance_auth") or {})
        merged.update({k: v for k, v in upd.items() if v})
        if merged == (cur.get("balance_auth") or {}):
            return
        custom_providers.upsert(ROOT_DIR, {"id": pid, "balance_auth": merged})
        print(f"[balance] {pid}: 登录令牌已自动续期并保存")
    except Exception as e:
        print(f"[balance] 令牌续期落盘失败: {e}")


def _balance_now(tid=None):
    """实时查询供应商余额（无缓存）。返回 (余额 float|None, 币种, 来源)。

    tid 非空＝查该任务钉住的模型所属源（输入框里切了模型，顶栏金额要跟着走）；
    tid 为空＝维持原语义，查全局默认源。

    统一走 custom_providers.query_balance：内置源（DeepSeek / 智谱 / OpenRouter…）
    与用户自己加的第三方源吃**同一套厂商适配器**，所以切到第三方源后顶栏余额
    也能正常显示，不必为每家再写一段。

    depth 用 "relay" 而不是 "full"：这个函数由后台每 120s 轮询一次，
    full 那层（宽松识别）会对站点打二十几个请求 —— 对自研站点太不礼貌。
    relay 能覆盖 one-api 系（第三方站的绝大多数）且最多 4 个请求。
    """
    try:
        cfg = get_task_model_config(tid) if tid else get_model_config()
        api_key = cfg.api_key
        # 有平台凭据的源（DMX / LeaderAI）即使没填模型密钥也照样能查 ——
        # 它们的余额记在账号上，模型密钥本来就不参与这件事。
        bal_auth = _balance_auth_for(cfg)
        if not api_key and not bal_auth:
            return None, "", ""
        r = custom_providers.query_balance(
            cfg.base_url or "", api_key,
            getattr(cfg, "protocol", "openai") or "openai",
            getattr(cfg, "extra_headers", None),
            timeout=10, depth="relay",
            balance_auth=bal_auth)
        if r.get("auth_update"):
            _persist_balance_auth(getattr(cfg, "provider", ""), r["auth_update"])
        if r.get("ok") and r.get("remaining") is not None:
            return float(r["remaining"]), (r.get("currency") or ""), (r.get("provider") or "")
        if r.get("error"):
            print(f"[balance] {cfg.provider}: {r['error']}｜{(r.get('advice') or '')[:60]}")
    except Exception as e:
        print(f"[balance] query failed: {e}")
    return None, "", ""


def _query_balance(force=False, tid=None):
    """Query balance API, cached.

    tid 为空：全局源，缓存 120s，并维护消费基线（历史行为不变）。
    tid 非空：该任务钉住的源，单独缓存 60s，**不动消费基线** ——
        基线是「某个源的钱变少了＝花了多少」的差量推算，多任务在两个不同源之间
        来回切会把两个池子的余额差算成消费（曾把 ¥10 防抖逻辑搞出垃圾数据）。
    """
    global _total_spent_cny, _last_balance_cny, _balance_cache
    now = time.time()
    if tid:
        with _balance_cache_lock:
            hit = _balance_task_cache.get(tid)
            if not force and hit and now - hit[0] < _BALANCE_TASK_TTL:
                return hit[1]
        balance_data = None
        now_bal, cur_code, src = _balance_now(tid)
        cur_code = (cur_code or "").upper()
        if now_bal is not None:
            balance_data = {
                "is_available": True,
                "balance_infos": [{"total_balance": str(now_bal),
                                   "currency": cur_code or "CNY"}],
                "currency": cur_code or "CNY",
                "provider": src,
            }
        with _balance_cache_lock:
            _balance_task_cache[tid] = (now, balance_data)
        return balance_data
    with _balance_cache_lock:
        if not force and _balance_cache[0] and now - _balance_cache[0] < 120:
            return _balance_cache[1]
    balance_data = None
    now_bal, cur_code, src = _balance_now()
    cur_code = (cur_code or "").upper()
    if now_bal is not None:
        balance_data = {
            "is_available": True,
            "balance_infos": [{"total_balance": str(now_bal),
                               "currency": cur_code or "CNY"}],
            "currency": cur_code or "CNY",
            "provider": src,
        }
    with _balance_cache_lock:
        _balance_cache = (now, balance_data)
    if balance_data and balance_data.get("is_available"):
        now_bal = float(now_bal)
        global _balance_provider, _balance_currency
        cur_provider = get_model_config().provider
        is_cny = cur_code in ("CNY", "RMB")
        if _balance_provider != cur_provider or _balance_currency != cur_code:
            # 供应商/币种切换（或首次启动）：两池资金独立，重置基线，不把差额误算成消费
            _balance_provider, _balance_currency = cur_provider, cur_code
            _last_balance_cny = now_bal
        elif not is_cny:
            # 非人民币源（one-api 系多为 USD）：币种不同，余额差直接并进 CNY 口径
            # 只会把消费统计搞乱，所以这里只跟基线、不累计
            _last_balance_cny = now_bal
        else:
            if _last_balance_cny == 0:
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


def _invalidate_task_balance(tid):
    """把某个任务的余额缓存作废（该任务刚在输入框里换了模型）。
    只清这一个任务的，别人的数不动。
    """
    if not tid:
        return
    with _balance_cache_lock:
        _balance_task_cache.pop(tid, None)


def _invalidate_balance():
    """切源 / 改源 / 删源之后必须清余额缓存。

    缓存是按时间（120s）算的，但余额本身跟 provider 走：不清的话，
    用户切到第三方源后顶栏会一直显示**旧源**的余额（最长 120 秒），
    看起来就像新源的钱被算错了。顺手起个后台线程立刻查一次，
    让顶栏马上换成新源的数（或换成"—"）。

    任务级缓存一并清空：任务钉的那个模型可能就在被改的源上。
    """
    global _balance_cache, _balance_provider, _balance_currency
    with _balance_cache_lock:
        _balance_cache = (0, None)
        _balance_task_cache.clear()
    _balance_provider = None
    _balance_currency = ""
    try:
        threading.Thread(target=_query_balance, kwargs={"force": True}, daemon=True).start()
    except Exception as e:
        print(f"[balance] invalidate query failed: {e}")


# ══════════════════════════════════════════════════════
# 网页画中画（Web PiP）：内嵌可行性预检 + 代理取回 + 外部浏览器
# ══════════════════════════════════════════════════════
_PIP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _pip_host_is_internal(host):
    """字面 IP 是否属于本机/内网/保留段（防 SSRF）。"""
    import ipaddress
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return False  # 不是 IP，交给 DNS 解析后再判
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified)


def _pip_safe_url(url, resolve=True):
    """校验外链：只允许 http/https；resolve=True 时连解析结果也必须是公网地址。

    返回 (ok, reason)。代理端点必须 resolve=True——否则等于把本机 9890、内网服务
    甚至云元数据地址开放给任何能访问前端的人。
    """
    try:
        p = urllib.parse.urlparse(url or "")
    except Exception:
        return False, "地址无法解析"
    if p.scheme not in ("http", "https"):
        return False, "只支持 http/https 链接"
    host = p.hostname
    if not host:
        return False, "缺少主机名"
    if _pip_host_is_internal(host):
        return False, "内网/本机地址不允许走代理"
    if resolve:
        try:
            infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP)
        except Exception:
            return False, "域名解析失败"
        for info in infos:
            if _pip_host_is_internal(info[4][0]):
                return False, "该域名解析到内网地址，已拦截"
    return True, ""


class _PipRedirectHandler(urllib.request.HTTPRedirectHandler):
    """代理取回时逐跳校验重定向目标，避免 302 到 127.0.0.1 绕过 SSRF 检查。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, why = _pip_safe_url(newurl)
        if not ok:
            raise urllib.error.HTTPError(newurl, code, "redirect blocked: %s" % why, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_PIP_OPENER = urllib.request.build_opener(_PipRedirectHandler)


def _pip_frame_policy(url):
    """该站点能否被 iframe 内嵌：读响应头里的 X-Frame-Options / CSP frame-ancestors。

    只做预检，不做拦截判定——预检拿不到响应（被反爬挡了）时返回 reachable=False，
    交给前端照常有 iframe 试一次，避免"预检失败 = 直接不给看"。
    """
    hdrs, final_url, reachable, err = {}, url, False, ""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={
                "User-Agent": _PIP_UA, "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "identity", "Range": "bytes=0-1",
            })
            with _PIP_OPENER.open(req, timeout=8) as r:
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                final_url, reachable = r.geturl(), True
                break
        except urllib.error.HTTPError as e:
            # 403/405 等也算"站点活着"，响应头同样有效
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            final_url, reachable = e.geturl() or url, True
            break
        except Exception as e:
            err = str(e)
            continue
    xfo = (hdrs.get("x-frame-options") or "").strip()
    csp = hdrs.get("content-security-policy") or ""
    blocked, reason = False, ""
    if xfo:
        v = xfo.split(",")[0].strip().upper()
        if v.startswith("DENY") or v.startswith("SAMEORIGIN") or v.startswith("ALLOW-FROM"):
            blocked, reason = True, "X-Frame-Options: %s" % xfo
    m = re.search(r"frame-ancestors([^;]*)", csp, re.I)
    if m:
        fa = m.group(1).strip()
        if fa and "*" not in fa:
            blocked, reason = True, "CSP frame-ancestors %s" % (fa or "'none'")
    return {"ok": True, "reachable": reachable, "blocked": blocked, "reason": reason,
            "final_url": final_url, "error": err}


def _pip_proxy_build(url):
    """取回页面并加工成"可内嵌"的 HTML：注入 <base> 修正相对路径、
    去掉页面自带的 CSP meta、把站内链接改成回到画中画里打开。

    返回 (content_type, bytes) 或抛异常。非 HTML（图片/PDF 等）原样透传。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": _PIP_UA, "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity", "Referer": url,
    })
    with _PIP_OPENER.open(req, timeout=20) as r:
        ctype = (r.headers.get("Content-Type") or "text/html").lower()
        raw = r.read(3 * 1024 * 1024 + 1)
        final_url = r.geturl()
    if len(raw) > 3 * 1024 * 1024:
        raise ValueError("页面超过 3MB，已放弃代理加载")
    if "html" not in ctype:
        return ctype, raw
    # 编码：优先响应头 charset，其次 meta charset，最后按 UTF-8 容错解
    cs = "utf-8"
    m = re.search(r"charset=([\w\-]+)", ctype)
    if m:
        cs = m.group(1)
    else:
        m2 = re.search(rb'charset=["\']?([\w\-]+)', raw[:4096], re.I)
        if m2:
            cs = m2.group(1).decode("ascii", "replace")
    try:
        html = raw.decode(cs, "replace")
    except LookupError:
        html = raw.decode("utf-8", "replace")
    # 页面自带的 CSP meta 会把外链资源再拦一次，移除；iframe 相关头由"我们"决定（本就不转发）
    html = re.sub(r'<meta[^>]+http-equiv=["\']?Content-Security-Policy["\']?[^>]*>', "", html, flags=re.I)
    base_tag = '<base href="%s">' % urllib.parse.urljoin(final_url, "/")
    inject = base_tag + (
        "<script>(function(){"
        "document.addEventListener('click',function(e){"
        "var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;if(!a)return;"
        "var h=a.getAttribute('href')||'';if(!h||h.charAt(0)==='#')return;"
        "try{var u=new URL(h,location.href).href;"
        "if(!/^https?:/i.test(u))return;"
        "e.preventDefault();parent.postMessage({__wp_nav:u},'*');}catch(_){}} ,true);"
        "})();</script>")
    if re.search(r"<head[^>]*>", html, re.I):
        html = re.sub(r"(<head[^>]*>)", lambda mm: mm.group(1) + inject, html, count=1, flags=re.I)
    elif re.search(r"<html[^>]*>", html, re.I):
        html = re.sub(r"(<html[^>]*>)", lambda mm: mm.group(1) + inject, html, count=1, flags=re.I)
    else:
        html = inject + html
    return "text/html; charset=utf-8", html.encode("utf-8", "replace")


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
            # [2026-10-19] GET 接口走 ETag 协商缓存：对话/列表内容没变只回 304（零正文），
            # 手机端切任务、刷新、轮询不再重复搬运整段历史。指纹 = md5(正文)，天然正确；
            # POST 等非 GET 一律不走这条路（请求本来就带副作用语义）。
            if code == 200 and (getattr(self, "command", "") or "").upper() == "GET":
                etag = '"' + hashlib.md5(body).hexdigest()[:24] + '"'
                inm = self.headers.get("If-None-Match") or ""
                self.send_response(304 if etag in inm else 200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, private")
                self.send_header("ETag", etag)
                if etag not in inm:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if etag not in inm:
                    self.wfile.write(body)
                return
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

        # ── 前端指纹清单（APK 冷启动先比哈希，一致就不重搬前端） ──
        elif path == "/api/fe-manifest":
            self._json(200, fe_manifest())


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
                # 任务运行状态同步：把所有运行中的任务状态注入 topics（侧栏运行点用）
                try:
                    _run_map = get_working_all()
                    if _run_map:
                        for t in (topics or []):
                            _ws = _run_map.get(t.get("id"))
                            if isinstance(_ws, dict) and _ws.get("status") and _ws.get("status") != "idle":
                                t["working_status"] = _ws.get("status")
                except Exception:
                    pass
                # 循环一登记就点亮（不等第一次 set_working，见 _loop_active_for）：
                # 前端据此在"刚发起 / 切回来"的窗口里也把运行点与停止按钮立起来
                try:
                    with _cancel_lock:
                        _loop_tids = set(_cancel_events.keys())
                    if _loop_tids:
                        for t in (topics or []):
                            if t.get("id") in _loop_tids:
                                t["loop_active"] = True
                except Exception:
                    pass
                # 未命名任务：用用户第一条提问做**显示名**（只加 display_title，不动 title）
                attach_display_title(topics, self.db)
                # 「需要你动作」同步：AI 正在阻塞提问等回答的任务 → 侧栏右上角黄底黑字标签
                attach_action_needed(topics)
                self._json(200, {"topics": topics or [], "active": active})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 全局搜索：任务标题 + 历史对话正文 ──
        elif path == "/api/search":
            try:
                q = (params.get("q", [""])[0] or "").strip()
                lim_raw = params.get("limit", ["60"])[0]
                try:
                    lim = max(1, min(300, int(lim_raw)))
                except Exception:
                    lim = 60
                hits = self.db.search(q, limit=lim) if q else []
                attach_display_title(hits, self.db)   # 未命名任务：显示名兜底（只加字段）
                self._json(200, {"query": q, "count": len(hits), "hits": hits})
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
                # exclude_thinking=1: 只回 has_thinking 标记，不回思考正文。
                # 单个长任务的 thinking 累计可达几 MB，若随首屏/轮询一起传，
                # "最终答复"要等整包下载完才渲染（用户感知：卡很久然后突然全冒出来）。
                # 正文改由 /api/message-thinking 在前端点开过程泡时按需拉取。
                _excl = str(params.get("exclude_thinking", ["0"])[0]).lower() in ("1", "true", "yes")
                # after_ts: 增量刷新（只取该时刻之后的消息），避免每次全量重传历史
                _after = params.get("after_ts", [None])[0]
                # conclusions=1: 结论视图 —— 只回"人话"（用户消息 + AI 最终回复），
                #   被剔掉的过程（工具调用 + 工具轮叙述）在序列里折成 proc 占位。
                #   实测同一任务：原始 5487 条，结论仅 118 条 / 48KB，过程明细 3.4MB。
                # proc_from/proc_to: 前端点开某个占位时，按 id 区间懒加载那一段的过程明细。
                _concl = str(params.get("conclusions", ["0"])[0]).lower() in ("1", "true", "yes")
                _pfrom = params.get("proc_from", [None])[0]
                _pto = params.get("proc_to", [None])[0]

                if _pfrom not in (None, "") and _pto not in (None, ""):
                    payload = {"messages": self.db.get_proc_range(tid, _pfrom, _pto)}
                elif _concl:
                    msgs = self.db.get_messages(tid, limit=None, offset=0, skip_hidden=False,
                                                skip_process=True, exclude_thinking=_excl,
                                                conclusions_only=True, after_ts=_after)
                    byid = {}
                    for _m in (msgs or []):
                        byid[_m.get("id")] = _m
                    seq = self.db.get_message_sequence(tid)
                    out = []
                    pending = 0
                    prev_id = 0
                    pending_ts = 0
                    pending_step = ""      # 这一段过程里最后一次工具调用的名字
                    for it in seq:
                        if it["c"]:
                            if pending:
                                out.append({"role": "proc", "gap": pending, "from_id": prev_id,
                                            "to_id": it["id"], "timestamp": pending_ts,
                                            "last_step": pending_step})
                                pending = 0
                                pending_step = ""
                            _mm = byid.get(it["id"])
                            if _mm is not None:
                                out.append(_mm)
                            prev_id = it["id"]
                        else:
                            pending += 1
                            pending_ts = it["ts"]
                            if it.get("tn"):
                                pending_step = it["tn"]
                    if pending:
                        # 末尾没有最终答复（任务正在跑或跑完没结论）：这段过程也要露出来
                        out.append({"role": "proc", "gap": pending, "from_id": prev_id,
                                    "to_id": 0, "timestamp": pending_ts,
                                    "last_step": pending_step})
                    if _after not in (None, "", "0"):
                        try:
                            _a = float(_after)
                            if _a > 1e12:
                                _a /= 1000.0
                            _cut = _a * 1000.0      # 占位/结论都按毫秒比，单位与 timestamp 对齐
                            out = [x for x in out if float(x.get("timestamp") or 0) >= _cut]
                        except (TypeError, ValueError):
                            pass
                    if out:
                        _attach_compressed_originals(tid, out)
                    payload = {"messages": out, "concl": True, "total": len(seq),
                               # 前端拿它给尾部轮询播种：这样"刚拉过服务端"的那一次不会被
                               # 紧接着的探针判成"有新消息"而重复拉一遍，同时也不会漏掉
                               # 拉取与探针之间落库的消息（id 对得上就是真没新东西）
                               "last_id": (seq[-1]["id"] if seq else 0)}
                else:
                    msgs = self.db.get_messages(tid, limit=limit, offset=offset, skip_hidden=False,
                                                skip_process=True, exclude_thinking=_excl, after_ts=_after)
                    # Attach original_text for compressed messages so frontend can show real content
                    if msgs:
                        _attach_compressed_originals(tid, msgs)
                    # last_id：前端给尾部轮询播种用（拉到的 id 与探针一致 = 真的没有新东西，
                    # 不会把刚拉的东西再拉一遍；不一致 = 拉取与探针之间落库了新消息，该刷）
                    payload = {"messages": msgs or [],
                               "last_id": self.db.last_message_id(tid) if tid else 0}
                # 前端据 md.working 判断本任务是否还在跑（用于清理陈旧的错误标记），
                # 旧实现没回这个字段，前端一直在读 undefined
                if tid:
                    try:
                        w = get_working(tid)
                        if isinstance(w, dict):
                            payload["working"] = w
                    except Exception:
                        pass
                self._json(200, payload)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 尾部探针（前端每 3 秒问一次）──
        # 目的：让界面"自己会动"。旧实现里除了发消息那条链路，没有任何东西会主动刷新
        # 当前任务的消息区，用户必须切来切去才能看到新内容（原话："他也不动，
        # 非要我切来切去才动一下，我都不知道他有没有做完这个任务"）。
        # 这里只回判据（最后一条 id / 总数 / 最后一步工具名 / 最后结论 id），
        # 不取正文 —— 前端据此决定"要不要刷新"和"末尾那步显示什么"。
        # 走 (topic_id, ts) 索引倒序 seek，实测量级 <5ms，可以放心高频轮询。
        elif path == "/api/topic/tail":
            try:
                tid = params.get("topic_id", [""])[0]
                if not tid:
                    self._json(400, {"error": "topic_id required"})
                else:
                    d = self.db.get_topic_tail(tid)
                    try:
                        w = get_working(tid)
                        if isinstance(w, dict):
                            d["working"] = w
                    except Exception:
                        pass
                    self._json(200, d)
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 按需拉思考正文（过程泡展开时才调，首屏不传）──
        elif path == "/api/message-thinking":
            try:
                tid = params.get("topic_id", [""])[0]
                raw_ids = params.get("ids", [""])[0]
                ids = [int(x) for x in str(raw_ids).split(",") if x.strip().lstrip("-").isdigit()]
                self._json(200, {"topic_id": tid,
                                 "thinking": self.db.get_thinking_by_ids(tid, ids) if tid else {}})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/task-model":
            tid = params.get('topic_id', [''])[0]
            try:
                cfg = get_task_model_config(tid)
                _models = _build_model_list(cfg)
                self._json(200, {'topic_id': tid, 'provider': cfg.provider, 'model': cfg.model,
                                 'models': _models, 'recent': _recent_model_view(self.db, _models),
                                 'usage': task_runtime.usage(tid)})
            except Exception as e:
                self._json(400, {'error': str(e)})
        elif path == "/api/models":
            cfg = get_model_config()
            models = _build_model_list(cfg)
            self._json(200, {
                "models": models,
                # 前端据此提示：当前模型看不了图时，带图会发生什么
                "vision_capable": _vision_capable(cfg),
                "vision_fallback": _vision_fallback_enabled(),
                # 前端设置页用：主模型持续不可用时是否自动换源
                "auto_fallback": _auto_fallback_enabled(),
            })
        # ── Models fallback chain ──
        elif path == "/api/models/fallback-chain":
            cfg = get_model_config()
            self._json(200, {"fallback_chain": cfg.fallback_chain,
                             "current": cfg.to_dict()})

        # ── 第三方模型源（自定义兼容端点，数量不限）──
        elif path == "/api/custom-providers":
            try:
                self._json(200, {
                    "providers": custom_providers.public_list(ROOT_DIR),
                    "builtin_ids": sorted(custom_providers.BUILTIN_IDS),
                    "protocols": list(custom_providers.PROTOCOLS),
                })
            except Exception as e:
                self._json(500, {"error": str(e), "providers": []})

        # ── 本地模型服务状态（llama.cpp / Ollama）──
        # 供前端：①切换前预检 ②拉起后轮询进度 ③设置页状态灯
        elif path == "/api/local-llm/status":
            try:
                force = str(params.get("force", ["0"])[0]).lower() in ("1", "true", "yes")
                self._json(200, local_llm.status(force=force))
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

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
        # ── Cost ──
        elif path == "/api/cost":
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
            # topic_id 非空＝要该任务钉住的源的余额（顶栏在任务里跟着输入框切的模型走）。
            # 不传就退回全局源，老客户端行为不变。
            tid = (params.get("topic_id", [""])[0] or "").strip()
            _force = (params.get("force", [""])[0] or "") in ("1", "true")
            bd = _query_balance(force=_force, tid=tid or None)
            if bd and bd.get("is_available"):
                info = bd.get("balance_infos", [{}])[0]
                self._json(200, {
                    "currency": info.get("currency") or bd.get("currency") or "CNY",
                    "total_balance": info.get("total_balance", "0.00"),
                    "granted_balance": info.get("granted_balance", "0.00"),
                    "topped_up_balance": info.get("topped_up_balance", "0.00"),
                    "provider": bd.get("provider", ""),
                    "topic_id": tid,
                })
            else:
                self._json(200, {"total_balance": "0.00", "currency": "CNY", "error": "unavailable", "topic_id": tid})

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
                # 上报"实际生效值"（已钳制到模型上下文窗口），而不是库里存的原始值。
                # 否则本地模型只有 24K 窗口时会显示存的 80000，超出输入框 max=24576。
                "token_limit": _effective_token_limit(db),
                "warn_threshold_pct": int(db.get_meta("compress_warn_pct") or 60),
                "turn_budget": int(db.get_meta("compress_turn_budget") or 50),
                "msg_count_warn": int(db.get_meta("compress_msg_count_warn") or MSG_COUNT_WARN),
                "msg_count_wall": int(db.get_meta("compress_msg_count_wall") or MSG_COUNT_WALL),
                "verify_ssl": db.get_meta("verify_ssl") != "0",
                "narrative_light_trigger": _runtime_switch(db, "narrative_light_trigger", "NARRATIVE_LIGHT_TRIGGER_ENABLED"),
                "fingerprint_advantage": _runtime_switch(db, "fingerprint_advantage", "FINGERPRINT_ADVANTAGE_ENABLED"),
                "context_window": _model_context_window(get_model_config().model),
                # 可设置上限与窗口来源：前端输入框的 max 用 wm_max，
                # ctx_source=unknown 时说明"窗口未登记，上限由用户定"
                "wm_max": _wm_setting_ceiling(),
                "ctx_source": _ctx_window_info(get_model_config().model)[1],
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
            tid = params.get('topic_id', [''])[0]
            task_live = task_runtime.usage(tid) if tid else {}
            if task_live:
                attention['task_runtime'] = task_live
                if task_live.get('budget'):
                    attention['task_runtime']['pct'] = round(task_live.get('latest', 0) * 100 / task_live['budget'], 2)
            self._json(200, {'topic_id': tid, 'attention': attention})


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
            _m_tid = params.get("tid", [None])[0]
            self._json(200, {"matters": _get_matter_items(_m_tid)})

        # ── 互动基调 ──
        elif path == "/api/tone":
            try:
                from tools.important_matters import get_tone
                self._json(200, {"tone": get_tone()})
            except Exception as e:
                self._json(500, {"error": str(e)})

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
        # 数据源 = data/deleted_topics_backup.jsonl（db.delete_topic 写进去的完整快照）。
        # 2026-09-23 之前这里读的是 data/trash 目录，但没有任何代码往那儿写 → 面板永远空。
        elif path == "/api/recycle/list":
            try:
                self._json(200, {"items": recycle_items()})
            except Exception as e:
                self._json(500, {"error": str(e)})

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
        # ── 工作区（只读：输入框旁「工作区外访问」按钮取状态用；不列目录，故不用 mission-files）──
        elif path == "/api/workspace":
            try:
                _tid = params.get("topic_id", [""])[0]
                _allow = bool(_tid) and (self.db.get_topic_meta(_tid, "allow_outside") == "1")
                _ws_rel = self.db.get_topic_meta(_tid, "workspace") if _tid else ""
                _ws_abs = ""
                if _tid:
                    try:
                        _ws_abs = os.path.abspath(_resolve_workspace(_ws_rel, _tid))
                    except Exception:
                        _ws_abs = ""
                # 产出文件夹（用户为这个任务额外指定的交付目录，与工作区并存）。
                # 目录后来被删/拔盘时仍原样回报（用户在菜单里要能看到"我设过它"），
                # 由前端标"不可用"，运行时另有 isdir 兜底 —— 绝不静默当成没设置。
                _od = (self.db.get_topic_meta(_tid, "output_dir") or "").strip() if _tid else ""
                self._json(200, {"allow_outside": _allow, "workspace": _ws_abs,
                                 "output_dir": _od,
                                 "output_dir_ok": bool(_od) and os.path.isdir(_od)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 目录选择器（输入框「选择本地项目」专用）──
        # 只列目录、不列文件。刻意不复用 /api/mission-files：后者 subdir 不存在时会
        # 静默回退到工作区，在选择器里会表现成"点了 D:\xxx 却跳到工作区"的错目录。
        # path 为空 = 此电脑（Windows 返回所有存在的盘符根）。
        elif path == "/api/dir-picker":
            try:
                import string as _string
                _roots = []
                if os.name == "nt":
                    for _d in _string.ascii_uppercase:
                        _rp = _d + ":\\"
                        try:
                            if os.path.isdir(_rp):
                                _roots.append({"name": _d + ":", "path": _rp})
                        except OSError:
                            pass
                else:
                    _roots = [{"name": "/", "path": "/"}]
                _p = (params.get("path", [""])[0] or "").strip()
                # 从资源管理器"复制路径"常带引号；'C:' 无反斜杠在 Windows 上指该盘当前目录，补成根
                _p = _p.strip('"').strip("'")
                if os.name == "nt" and len(_p) == 2 and _p[1] == ":":
                    _p = _p + "\\"
                if not _p:
                    self._json(200, {"path": "", "parent": "", "crumbs": [],
                                     "dirs": [], "roots": _roots})
                    return
                _p = os.path.abspath(_p)
                if not os.path.isdir(_p):
                    self._json(200, {"path": "", "parent": "", "crumbs": [], "dirs": [],
                                     "roots": _roots,
                                     "error": "目录不存在或无法访问: " + _p})
                    return
                _dirs, _err = [], ""
                try:
                    with os.scandir(_p) as _it:
                        for _e in _it:
                            try:
                                if not _e.is_dir(follow_symlinks=False):
                                    continue
                            except OSError:
                                continue
                            if _e.name in ("$RECYCLE.BIN", "System Volume Information"):
                                continue
                            _dirs.append({"name": _e.name, "path": os.path.join(_p, _e.name)})
                except PermissionError:
                    _err = "没有权限访问这个目录"
                except OSError as _oe:
                    _err = f"无法读取目录: {_oe}"
                _dirs.sort(key=lambda x: x["name"].lower())
                # 面包屑：从盘符根逐级到当前目录，每段都带绝对路径好回跳
                _crumbs, _cur, _guard = [], _p, 0
                while _cur and _guard < 64:
                    _trimmed = _cur.rstrip("\\/")
                    _name = os.path.basename(_trimmed) or (_trimmed[:2] if _trimmed[1:2] == ":" else _trimmed)
                    _crumbs.insert(0, {"name": _name, "path": _cur})
                    _up = os.path.dirname(_trimmed)
                    if not _up or _up == _cur or _up == _trimmed:
                        break
                    _cur = _up
                    _guard += 1
                _parent = os.path.dirname(_p.rstrip("\\/"))
                # 盘符根（'D:\'）的 dirname 是 'D:' 它自己（Windows 的怪癖），
                # 这里必须归一成空 —— 空 = 此电脑；否则前端会画出一个点不动的"上一级"。
                if (not _parent or _parent == _p
                        or os.path.normcase(_parent) == os.path.normcase(_p.rstrip("\\/"))):
                    _parent = ""
                self._json(200, {"path": _p, "parent": _parent, "crumbs": _crumbs,
                                 "dirs": _dirs, "roots": _roots, "error": _err})
            except Exception as e:
                self._json(500, {"error": str(e)})
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
            # ── ⑳ 目录指纹模式（?sig=1）：自动刷新专用 ──────────────────────
            # 稳态轮询每 2.5s 一次，若每次都把整个 files 列表序列化 + 传输，
            # 大目录就是纯浪费。这里只回一个指纹：前端指纹没变 ⇒ 一个字节都不动 DOM。
            # ⚠️ 不带 sig=1 的老调用点行为完全不变（仍返回全量）；前端也兼容"没实施指纹"的
            #    老服务端（响应里直接带 files，就用前端本地指纹顶上）⇒ 这一段可独立上线。
            #
            # 🔴 这里的算法必须与前端 fbArSigLocal() **逐字节一致**，三处口径都要对齐：
            #    ① 累积方式：_h 逐字符累进且**跨条目不重置**。若每项都从初值重算再覆盖，
            #       就等于"只看最后一个条目"——目录里放 a.txt+b.txt 与只放 b.txt 会撞出同一个值。
            #    ② 数值截断：这里用 int() 向零截断，前端必须用 Math.floor，不能用 Math.round
            #       （1.4 两者相同，但 1.6 会分别是 1 和 2，值就错开了）。
            #    ③ 排序：按 name 升序，两端都必须先排序再算。
            #    口径不一致时，前端每轮都判成"变了" ⇒ 自动刷新退化成"每 2.5s 全量拉取 +
            #    全量重排"，比不做还糟（实测踩过：累积写成覆盖导致两端永久对不上）。
            if params.get("sig", ["0"])[0] == "1":
                _h = 2166136261
                try:
                    for _f in sorted(files, key=lambda x: x["name"]):
                        _s = "%s|%d|%d|%d" % (_f["name"], 1 if _f["is_dir"] else 0,
                                               int(_f["size"] or 0), int(_f["mtime"] or 0))
                        for _ch in _s:
                            _h = ((_h ^ ord(_ch)) * 16777619) & 0xFFFFFFFF
                        _h = ((_h ^ len(_s)) * 16777619) & 0xFFFFFFFF
                    # 前缀带项数：少一项/多一项必然改值（哈希碰撞概率可忽略）
                    _sig = "%d-%08x" % (len(files), _h)
                except Exception:
                    _sig = ""
                self._json(200, {"sig": _sig, "n": len(files), "subdir": subdir})
                return
            self._json(200, {"files": files, "workspace": os.path.abspath(ws_path), "subdir": subdir, "mission_dir": mission_dir, "allow_outside": _allow_outside})
        # ── Mission file (download / inline preview) ──
        elif path.startswith("/api/mission-file"):
            tid = params.get("topic_id", [""])[0]
            filename = params.get("file", [""])[0]
            # inline=1：按真实 MIME 内联返回（PDF/图片/音视频/文本能在浏览器里直接显示）；
            # 缺省仍走 octet-stream + attachment，下载队列行为不变。
            _inline = params.get("inline", ["0"])[0] == "1"
            # Resolve from workspace first, fallback to mission dir
            ws_rel = self.db.get_topic_meta(tid, "workspace")
            ws_path = _resolve_workspace(ws_rel, tid)
            filepath = os.path.join(ws_path, filename)
            if not (os.path.exists(filepath) and os.path.isfile(filepath)):
                filepath = os.path.join(ROOT_DIR, "data", "missions", tid, filename)
            if os.path.exists(filepath) and os.path.isfile(filepath):
                self._serve_file(filepath, inline=_inline)
            else:
                self._json(404, {"error": "file not found"})

        # ── 文件所在目录（HTML 预览用）: /api/file-dir?topic_id=&file= ──
        # 返回 {"dir": 绝对目录, "name": 文件名}。路径解析与 /api/mission-file 完全一致
        # （工作区优先 → 回退 mission 目录）。前端拿到绝对目录后再拼
        # /api/fs-raw/<b64dir>/<name> 作为 iframe src —— 目录必须是绝对且正确的，
        # 否则页面里的相对引用解析不到同级 css/js/图片。
        elif path == "/api/file-dir":
            tid = params.get("topic_id", [""])[0]
            filename = params.get("file", [""])[0]
            ws_rel = self.db.get_topic_meta(tid, "workspace")
            ws_path = _resolve_workspace(ws_rel, tid)
            filepath = os.path.join(ws_path, filename)
            if not (os.path.exists(filepath) and os.path.isfile(filepath)):
                filepath = os.path.join(ROOT_DIR, "data", "missions", tid, filename)
            if os.path.exists(filepath) and os.path.isfile(filepath):
                ap = os.path.abspath(filepath)
                seg = base64.urlsafe_b64encode(
                    os.path.dirname(ap).encode("utf-8")).decode("ascii").rstrip("=")
                self._json(200, {"dir": os.path.dirname(ap),
                                 "name": os.path.basename(ap),
                                 "raw": "/api/fs-raw/" + seg + "/" + urllib.parse.quote(os.path.basename(ap))})
            else:
                self._json(404, {"error": "file not found"})


        # 必须走 path-style，不能复用上面的 ?file= 形式（2026-10-16）：
        #   iframe 里页面的相对引用（<link href="a.css">、<img src="img/b.png">）
        #   是按 base URL 的"最后一段"替换解析的
        #   —— base = /api/fs-raw/<b64dir>/index.html
        #      → a.css 解析成 /api/fs-raw/<b64dir>/a.css    ✓ 子目录保住
        #   —— 若 base = /api/mission-file?file=sub/index.html
        #      → a.css 解析成 /api/mission-file?file=a.css  ✗ 目录丢了、参数被整个替换
        #   只用 query 形式时页面必然"只有骨架、没样式没图"，且 file 参数被吃掉。
        # b64dir = 文件所在目录的绝对路径（urlsafe base64，去 padding）；
        # relpath 可为空（空则回目录本身 → 404）。用 ../ 上溯也在同一路径空间里，
        # 能力面与 /api/mission-file 一致（本地单用户应用的本地文件服务）。
        elif path.startswith("/api/fs-raw/"):
            try:
                rest = path[len("/api/fs-raw/"):]
                seg, _, rel = rest.partition("/")
                pad = "=" * (-len(seg) % 4)
                dir_abs = base64.urlsafe_b64decode(seg + pad).decode("utf-8")
                if not os.path.isabs(dir_abs):
                    self._json(400, {"error": "dir must be absolute"})
                    return
                target = os.path.normpath(
                    os.path.join(dir_abs, urllib.parse.unquote(rel)))
                if not (os.path.exists(target) and os.path.isfile(target)):
                    self._json(404, {"error": "file not found"})
                    return
                self._serve_file(target, inline=True)
            except Exception as e:
                self._json(400, {"error": str(e)})


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

        # ── 网页画中画：内嵌可行性预检 ──
        # 对话里点外链时先问一句"这站能不能被 iframe 内嵌"，省得帧里白屏半天
        elif path == "/api/url-check":
            try:
                target = (params.get("url", [""])[0] or "").strip()
                ok, why = _pip_safe_url(target, resolve=False)
                if not ok:
                    self._json(400, {"ok": False, "blocked": False, "error": why})
                    return
                info = _pip_frame_policy(target)
                info["url"] = target
                self._json(200, info)
            except Exception as e:
                self._json(500, {"ok": False, "blocked": False, "error": str(e)})

        # ── 网页画中画：代理取回（给禁止内嵌的站点用）──
        elif path == "/api/web-proxy":
            try:
                target = (params.get("url", [""])[0] or "").strip()
                ok, why = _pip_safe_url(target, resolve=True)
                if not ok:
                    self._json(403, {"error": why})
                    return
                ctype, body = _pip_proxy_build(target)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                # 故意不转发 X-Frame-Options / CSP：内嵌与否由本应用自己决定
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json(502, {"error": "代理加载失败：%s" % e})

        # ── 资产索引层（库=索引，登记制）──
        elif path == "/api/assets":
            self._json(200, {"assets": tools.asset_lib.list_assets(), "lib_dir": tools.asset_lib.get_lib_dir()})
        elif path == "/api/assets/tags":
            self._json(200, {"tags": tools.asset_lib.all_tags()})
        elif path == "/api/assets/settings":
            self._json(200, {"lib_dir": tools.asset_lib.get_lib_dir()})

        # ── 资产库 v3（混合仓：库=能逛的仓库）──
        elif path == "/api/assets/v3/overview":
            try:
                self._json(200, tools.asset_v3.lib_overview())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/v3/browse":
            try:
                sub = (params.get("sub") or ["notes"])[0]
                rel = (params.get("path") or [""])[0]
                self._json(200, tools.asset_v3.browse(sub, rel))
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/v3/read":
            try:
                fp = (params.get("path") or [""])[0]
                self._json(200, {"path": fp, "content": tools.asset_v3.read_text(fp)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path.startswith("/api/assets/v3/file"):
            # 库内文件直读（图片/媒体内联预览，其余下载）：路径必须落在库目录内
            try:
                fp = tools.asset_v3._safe_rel((params.get("path") or [""])[0])
                if os.path.isfile(fp):
                    self._serve_file(fp, inline=params.get("inline", ["0"])[0] == "1")
                else:
                    self._json(404, {"error": "not a file"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path == "/api/assets/preview":
            # 资产缩略图/内联预览（B1 时间流浏览器）：只允许读【已登记在资产索引里】的路径，
            # 相当于 file 接口的"引用版"——登记制下文件在盘上任意位置，不能开放任意路径读，
            # 所以这里以 find_by_path 白名单做闸门：索引里没有的路径一律 403。
            try:
                fp = tools.asset_lib._norm_path((params.get("path") or [""])[0])
                rec = tools.asset_lib.find_by_path(fp)
                if not rec:
                    self._json(403, {"error": "路径未登记，拒绝读取"})
                elif not os.path.isfile(fp):
                    self._json(404, {"error": "文件不存在"})
                else:
                    self._serve_file(fp, inline=True)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif path == "/api/assets/ls":
            # B2 项目内部视图：列出【已登记目录资产】的内部文件树（一层）。
            # 安全闸门：root 必须已登记且为 dir/project；path 相对 root，规范化后必须仍在 root 内。
            try:
                root = tools.asset_lib._norm_path((params.get("root") or [""])[0])
                rec = tools.asset_lib.find_by_path(root)
                rtype = rec.get("type") if isinstance(rec, dict) else None
                if not rec or rtype not in ("dir", "project"):
                    self._json(403, {"error": "root 未登记为目录资产"})
                else:
                    sub = (params.get("path") or [""])[0]
                    cur = os.path.normpath(os.path.join(root, sub)) if sub else root
                    if not (cur == root or cur.startswith(root + os.sep)):
                        self._json(403, {"error": "路径越界"})
                    elif not os.path.isdir(cur):
                        self._json(404, {"error": "目录不存在"})
                    else:
                        items = []
                        try:
                            for e in os.scandir(cur):
                                if e.is_dir():
                                    items.append({"name": e.name, "dir": True, "size": None, "mtime": int(e.stat().st_mtime)})
                                elif e.is_file():
                                    items.append({"name": e.name, "dir": False, "size": e.stat().st_size, "mtime": int(e.stat().st_mtime)})
                        except PermissionError:
                            pass
                        items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
                        rel = os.path.relpath(cur, root).replace("\\", "/") if cur != root else ""
                        self._json(200, {"root": root, "path": rel, "items": items})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 工作区画布布局（画布+窗口，tid 优先显式传参，后默认当前活跃话题）──
        elif path == "/api/canvas":
            tid = (params.get("tid") or [""])[0] or self.db.get_active_topic_id()
            if not tid:
                self._json(400, {"error": "缺 tid 且无活跃话题"})
            else:
                self._json(200, {"tid": tid, "canvas": tools.canvas.load_canvas(tid)})

        # ── 公网访问状态 ──
        # 一次拿全：配置（本机界面给明文，方便复制）+ 链路状态 + 资产是否就位。
        # deep=1 时顺带跑一次公网端到端探活（会真的发一次请求，默认不做）。
        elif path == "/api/tunnel/state":
            if tunnel_manager is None:
                self._json(500, {"ok": False, "error": f"tunnel_manager 未加载: {_TM_IMPORT_ERR}"})
            else:
                try:
                    cfg = tunnel_manager.load_config()
                    deep = (params.get("deep") or ["0"])[0] in ("1", "true", "yes")
                    self._json(200, {
                        "ok": True,
                        "config": tunnel_manager.config_view(cfg, reveal=True),
                        "status": tunnel_manager.status(cfg, deep=deep),
                    })
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        # ── Static files ──
        elif path == "/":
            self._serve_static("/index.html")
        else:
            self._serve_static(path)

    # 内联预览 MIME 表（2026-09-22）：只有"浏览器能自己渲染"的类型才列进来；
    # 表外的一律 application/octet-stream（docx/xlsx/zip/exe 这些交给下载，别让浏览器瞎猜）。
    _INLINE_MIME = {
        ".pdf":  "application/pdf",
        ".png":  "image/png",      ".jpg": "image/jpeg",   ".jpeg": "image/jpeg",
        ".gif":  "image/gif",      ".webp": "image/webp",  ".bmp": "image/bmp",
        ".svg":  "image/svg+xml",  ".ico": "image/x-icon", ".avif": "image/avif",
        ".mp4":  "video/mp4",      ".webm": "video/webm",  ".mov": "video/quicktime",
        ".m4v":  "video/x-m4v",
        ".mp3":  "audio/mpeg",     ".wav": "audio/wav",    ".ogg": "audio/ogg",
        ".m4a":  "audio/mp4",      ".flac": "audio/flac",  ".aac": "audio/aac",
        ".txt":  "text/plain; charset=utf-8",
        ".log":  "text/plain; charset=utf-8",
        ".md":   "text/plain; charset=utf-8",
        ".csv":  "text/csv; charset=utf-8",
        ".tsv":  "text/tab-separated-values; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".xml":  "text/xml; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".htm":  "text/html; charset=utf-8",
        ".js":   "application/javascript; charset=utf-8",
        ".css":  "text/css; charset=utf-8",
    }

    def _serve_file(self, filepath, inline=False):
        """原始文件服务（内联预览 / 下载两种口径）。

        为什么必须有 inline（2026-09-22 实证）：原来这里一律回 application/octet-stream，
        浏览器对 octet-stream 一律按"下载"处理 —— <iframe> 里放 PDF 只会一片空白，
        这就是"文件浏览器预览不了 PDF"的根因，跟前端怎么拼 iframe 无关。

        同时补上 Range 支持：Chrome 的 PDF 阅读器/`<video>` 都会发 Range 请求
        （大文件分段、拖动进度条），不支持就只能整份重下。

        http.server 这个用法是 HTTP/1.0，一个连接一个请求，所以发完必须收干净，
        客户端提前挂断（ConnectionError）要静默吞掉，不能把异常冒到 handler 外面。
        """
        try:
            st = os.stat(filepath)
        except OSError:
            self._json(404, {"error": "file not found"})
            return
        size = st.st_size
        ext = os.path.splitext(filepath)[1].lower()
        ctype = self._INLINE_MIME.get(ext) if inline else None
        if not ctype:
            ctype = "application/octet-stream"
        # 文件名带中文时必须用 RFC 5987 的 filename*=UTF-8''，裸 filename= 走 Latin-1 会炸
        fname = urllib.parse.quote(os.path.basename(filepath))
        disp = ("inline; " if inline else "attachment; ") + "filename*=UTF-8''" + fname

        rng = self.headers.get("Range", "") or ""
        start, end, code = 0, max(size - 1, 0), 200
        if inline and rng.lower().startswith("bytes=") and size > 0:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:                                   # bytes=-N → 末尾 N 字节
                    start = max(0, size - int(m.group(2)))
                    end = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                code = 206
        length = end - start + 1

        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", disp)
        if inline:
            self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            with open(filepath, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (ConnectionError, OSError):
            # 客户端中途断开（切标签/关预览/拖进度条）——文件内容还在磁盘上，静默降级
            pass

    def _serve_static(self, path):
        """Serve a static file from PUBLIC_DIR."""
        # [2026-10-19] URL 解码：浏览器/WebView 发来的是 percent-encoded UTF-8
        # （/大眼.apk -> /%E5%A4%A7%E7%9C%BC.apk）。旧实现直接按编码串拼路径，
        # 中文名文件一律 404（实测 /大眼.apk -> 404，/dayan-1.7.apk -> 200）。
        # 解码放在 normpath 之前，%2e%2e%2f 这类穿越编码才会被下面的前缀校验拦住。
        try:
            raw_path = urllib.parse.unquote(path, errors="surrogatepass")
        except Exception:
            raw_path = path
        # Security: prevent path traversal
        safe = os.path.normpath(raw_path).lstrip("/\\")
        filepath = os.path.join(PUBLIC_DIR, safe)
        _pub_norm = os.path.normpath(PUBLIC_DIR)
        if filepath != _pub_norm and not filepath.startswith(_pub_norm + os.sep):
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
            # [2026-10-18] 缓存分级。病根：首屏总量 7.16MB（mermaid 3.34M / icon.png 1.79M /
            # x6.js 858K / index.html 987K），旧实现一律 no-cache，而远端客户端每次导航都是
            # 全新会话（Cookie 都不保留，guard.log 每次 GET / 都紧跟 COOKIE-SET）⇒ 从不发
            # If-None-Match ⇒ 19 万次请求 0 次 304 ⇒ 每次重搬 7MB；3~5Mbps 下 12~20 秒，
            # 用户体感就是"远端切任务卡住无响应"。分级后首屏只剩 index.html 需要重下。
            #   · vendor/ 第三方库、带 ?v= 的自家 js → 一年 immutable（版本号即缓存键）
            #   · icon.png / favicon → 7 天
            #   · 其余（含 index.html、无版本号的 public/*.js）→ no-cache，改完刷新即生效
            _q = self.path.split("?", 1)[1] if "?" in self.path else ""
            _rel = safe.replace("\\", "/").lower()
            _base = _rel.rsplit("/", 1)[-1]
            if _rel.startswith("vendor/") or re.search(r"(?:^|&)v=", _q):
                _cc = "public, max-age=31536000, immutable"
            elif _base in ("icon.png", "favicon.ico") or _base.startswith("apple-touch-icon"):
                _cc = "public, max-age=604800"
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
                # [2026-10-19] 图片（/generated/ 下都是 gen_时间戳_随机 命名，内容不会原地变）
                # 缓存 7 天：手机端反复看同一张图不再拉网络，只有文件名变了才会重下。
                _cc = "public, max-age=604800"
            else:
                _cc = "no-cache"
            # ETag 协商缓存：指纹 = 大小-mtime；If-None-Match 匹配 -> 304，
            # APK 冷启动不再重拉壳页面，守门员透明转发不影响
            st = os.stat(filepath)
            etag = '"%d-%d"' % (st.st_size, int(st.st_mtime))
            inm = self.headers.get("If-None-Match", "")
            if inm and etag in inm:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", _cc)
                self.end_headers()
                return
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", _cc)
            self.send_header("ETag", etag)
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
        if path == "/api/task-model":
            try:
                tid = data.get('topic_id', '')
                provider, model = data.get('provider', ''), data.get('model', '')
                candidates = _build_model_list(get_model_config())
                if not any(x.get('provider') == provider and (x.get('id') or x.get('model')) == model for x in candidates):
                    raise ValueError('Model is not registered')
                task_runtime.save_selection(self.db, tid, provider, model)
                _push_recent_model(self.db, provider, model)
                # 顶栏金额要跟着这个任务的新模型所属源走：作废该任务的余额缓存。
                # （不传 topic_id 的旧客户端仍然看全局源，不受影响）
                _invalidate_task_balance(tid)
                self._json(200, {'success': True, 'topic_id': tid, 'provider': provider, 'model': model})
            except Exception as e:
                self._json(400, {'error': str(e)})
            return
        if path == "/api/chat":
            try:
                self._handle_chat(data)
            except Exception as e:
                import traceback
                traceback.print_exc()
                # 清理 working state，否则前端永久显示"处理中…"
                tid = data.get("topic_id", "")
                if tid:
                    finish_working(tid, "failed")
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
                finish_working(tid, "done")
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
                # 标准工作区不写 meta：_resolve_workspace 按 tid 现算，避免绝对路径失效残留
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
                # 标准工作区不写 meta：_resolve_workspace 按 tid 现算，避免绝对路径失效残留
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
                    "llamacpp": local_llm.base_url(),
                }
                # 本地服务没启动就别静默切换 —— 否则切过去之后第一条消息
                # 才在 ~20 秒重试后抛 WinError。这里返回专门 code，
                # 前端据此弹窗问"要不要现在拉起"。
                if provider == "llamacpp":
                    try:
                        # 多槽位：先按 model_id 找到槽位并切换（激活槽决定拉起哪个模型）。
                        # 槽位真变了才停旧槽 —— 16G 卡装不下两个 27B，切槽必须释放显存。
                        _prof = local_llm.resolve_profile(model_id)
                        if _prof:
                            _cfg0 = local_llm.get_config()
                            _old_prof = str(_cfg0.get("active_profile") or "")
                            _old_port = int(_cfg0.get("port") or 0)
                            _ar = local_llm.activate(_prof)
                            if not _ar.get("ok"):
                                self._json(200, {"success": False,
                                                 "error": _ar.get("error") or "槽位切换失败"})
                                return
                            if _prof != _old_prof and _old_port:
                                _new_port = next((int(p.get("port") or 0)
                                                  for p in (_cfg0.get("profiles") or [])
                                                  if isinstance(p, dict)
                                                  and str(p.get("name") or "") == _prof), 0)
                                if _new_port != _old_port:
                                    _stop_r = local_llm.stop(_old_port)
                                    print(f"[switch-model] stopped old slot {_old_prof}@{_old_port}: "
                                          f"killed={len(_stop_r.get('killed', []))}")
                        _st = local_llm.probe(force=True)
                        if not _st.get("running"):
                            self._json(200, {
                                "success": False,
                                "code": "local_not_running",
                                "error": f"本地推理服务未启动（{_st.get('base_url', '')}）",
                                "starting": bool(_st.get("starting")),
                                "status": _st,
                            })
                            return
                    except Exception as e:
                        print(f"[switch-model] local probe failed: {e}")
                base_url = provider_urls.get(provider, "")
                # 第三方源：地址/协议/额外请求头都取自设置页里的登记，
                # 不能走上面那张写死的字典（否则自定义模型一切就断线）
                cp = _custom_provider(provider)
                protocol, extra_headers, custom_key = "openai", {}, ""
                if cp:
                    base_url = cp["base_url"]
                    protocol = cp["protocol"]
                    extra_headers = cp["extra_headers"] or {}
                    custom_key = cp["api_key"] or ""
                elif not base_url:
                    self._json(200, {"success": False,
                                     "error": f"未知模型源 {provider}（可能已被删除）"})
                    return
                with _config_lock:
                    old_cfg = get_model_config()
                    # 切换 provider 时，从 fallback_chain 取对应 provider 的 api_key
                    # 否则切换到 deepseek 后 api_key 还是 ollama 的，导致 401
                    new_key = custom_key or _get_provider_key(old_cfg, provider)
                    _model_config = old_cfg.clone_with(
                        provider=provider, model=model_id,
                        base_url=base_url, api_key=new_key,
                        protocol=protocol, extra_headers=extra_headers)
                _save_model_config()
                # 换源了：余额缓存必须作废，否则顶栏还是上一个源的数字
                _invalidate_balance()
                # 上下文窗口随模型变化（本地模型按 --ctx-size，不是默认 128K）
                try:
                    _load_compress_config()
                except Exception:
                    pass
            self._json(200, {"success": True})

        # ── 本地模型服务：启动 ──
        elif path == "/api/local-llm/start":
            try:
                r = local_llm.start()
                self._json(200 if r.get("ok") else 500, r)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        # ── 本地模型服务：停止 ──
        elif path == "/api/local-llm/stop":
            try:
                self._json(200, local_llm.stop())
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        # ── 图片回退开关：当前模型看不了图时是否允许改用云端视觉模型 ──
        # 关掉后带图请求不会把图片发到云端（本地/离线优先场景）
        elif path == "/api/vision-fallback":
            try:
                p = os.path.join(ROOT_DIR, "model_config.json")
                raw = {}
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        raw = json.load(f) or {}
                if "enabled" in data:
                    raw["vision_fallback"] = bool(data["enabled"])
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(raw, f, ensure_ascii=False, indent=4)
                self._json(200, {"ok": True, "enabled": bool(raw.get("vision_fallback", True))})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        # ── 自动换源开关：主模型持续不可用时是否沿 fallback_chain 换源 ──
        # 换源会改变模型（能力/风格/上下文窗口都可能不同），不是所有场景都能换，
        # 所以做成开关；**默认关闭**（缺键=关），关掉后只做同源重试，再失败即中断。
        elif path == "/api/model-fallback":
            try:
                p = os.path.join(ROOT_DIR, "model_config.json")
                raw = {}
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        raw = json.load(f) or {}
                if "enabled" in data:
                    raw["auto_fallback"] = bool(data["enabled"])
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(raw, f, ensure_ascii=False, indent=4)
                self._json(200, {"ok": True, "enabled": raw.get("auto_fallback") is True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        # ── 本地模型服务：配置（含上下文长度，改后需重启生效）──
        elif path == "/api/local-llm/config":
            try:
                updates = {}
                for k in ("ctx_size", "port", "threads", "gpu_layers", "start_timeout",
                          "exe", "model_path", "model_id", "extra_args", "mmproj"):
                    if k in data:
                        updates[k] = data[k]
                if "ctx_size" in updates:
                    try:
                        ctx = int(updates["ctx_size"])
                    except Exception:
                        self._json(400, {"ok": False, "error": "上下文长度必须是整数"})
                        return
                    if not (512 <= ctx <= 1048576):
                        self._json(400, {"ok": False, "error": "上下文长度需在 512 ~ 1048576 之间"})
                        return
                    updates["ctx_size"] = ctx
                r = local_llm.save_config(updates)
                # 新的上下文上限要立刻参与压缩阈值钳制
                try:
                    _load_compress_config()
                except Exception:
                    pass
                self._json(200 if r.get("ok") else 500, r)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        # ── 第三方模型源：查余额 / 额度 ──
        elif path == "/api/custom-providers/balance":
            try:
                pid = str(data.get("id") or "").strip()
                saved = custom_providers.get(ROOT_DIR, pid) if pid else None
                base_url = str(data.get("api_base") or data.get("base_url") or "").strip()
                api_key = str(data.get("api_key") or "")
                protocol = str(data.get("protocol") or "").lower()
                if saved:
                    base_url = base_url or saved.get("api_base") or saved.get("base_url") or ""
                    protocol = protocol or saved.get("protocol") or "openai"
                    # 前端列表里只有脱敏 key：脱敏或空都回退到文件里的真 key，
                    # 否则「查余额」永远返回鉴权失败
                    if not api_key or "•" in api_key:
                        api_key = saved.get("api_key") or ""
                elif data.get("provider"):
                    # 内置 provider 也走同一套适配器（只需 host 对上，路径由适配器补）
                    builtin_urls = {
                        "deepseek": "https://api.deepseek.com",
                        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
                        "openrouter": "https://openrouter.ai/api/v1",
                    }
                    prov = str(data["provider"])
                    cfg = get_model_config()
                    base_url = base_url or builtin_urls.get(prov) or (
                        cfg.base_url if cfg.provider == prov else "")
                    protocol = protocol or "openai"
                    if not api_key or "•" in api_key:
                        api_key = _get_provider_key(cfg, prov)
                if not base_url:
                    self._json(400, {"ok": False, "error": "缺少 Base URL"})
                    return
                depth = str(data.get("depth") or "full").lower()
                if depth not in ("vendor", "relay", "full"):
                    depth = "full"
                # 平台管理凭据（DMXAPI 这类）：以表单里刚填的为准，没填就用已保存的。
                # 与 api_key 同等待遇 —— 前端只回显脱敏值，带「•」或空的都回退到
                # 文件里的真值，否则"查余额"永远吃到 401。
                _sent = data.get("balance_auth") if isinstance(data.get("balance_auth"), dict) else {}
                bal_auth = dict((saved or {}).get("balance_auth") or {})
                _tok = str(_sent.get("system_token") or _sent.get("access_token") or "").strip()
                if _tok and "•" not in _tok:
                    bal_auth["system_token"] = _tok
                _uid = str(_sent.get("user_id") or "").strip()
                if _uid:
                    bal_auth["user_id"] = _uid
                _rt = str(_sent.get("refresh_token") or "").strip()
                if _rt and "•" not in _rt:
                    bal_auth["refresh_token"] = _rt
                if not bal_auth.get("system_token") and not bal_auth.get("refresh_token") \
                        and data.get("provider"):
                    # 从「内置模型配置」那一栏发起查询时前端不带 id，凭据只能按
                    # 当前正在用的源去认领（DMXAPI / LeaderAI 常被当成内置 provider 直接选）
                    bal_auth = _balance_auth_for(get_model_config()) or bal_auth
                r = custom_providers.query_balance(
                    base_url, api_key, protocol or "openai",
                    data.get("headers") or None,
                    timeout=int(data.get("timeout") or 10),
                    depth=depth,
                    balance_auth=bal_auth or None)
                # 续期换到的新令牌：落盘但**不回给前端**（令牌没必要进 DOM/日志）
                _upd = r.pop("auth_update", None)
                if _upd:
                    _persist_balance_auth(pid, _upd)
                r["manual"] = (saved or {}).get("balance_manual") or {}
                self._json(200, r)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e), "tried": []})

        # ── 第三方模型源：测试连接 / 拉模型（不落盘，供「测试连接」按钮）──
        elif path == "/api/custom-providers/probe":
            try:
                raw_key = str(data.get("api_key") or "")
                # 已保存的源在前端只显示脱敏 key：这时必须用文件里的真 key 去探，
                # 否则"测试连接"永远失败，用户以为是地址填错了
                if data.get("id") and ("•" in raw_key or not raw_key):
                    saved = custom_providers.get(ROOT_DIR, data.get("id"))
                    if saved:
                        raw_key = saved.get("api_key") or ""
                r = custom_providers.probe(
                    data.get("base_url", ""),
                    raw_key,
                    data.get("protocol", "auto"),
                    data.get("headers") or None,
                    timeout=int(data.get("timeout") or 15),
                    probe_chat=bool(data.get("probe_chat", True)),
                    model=data.get("model", ""),
                )
                self._json(200, r)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e), "tried": []})

        # ── 第三方模型源：新增 / 修改 / 删除 ──
        elif path == "/api/custom-providers":
            try:
                action = str(data.get("action") or "save").lower()
                if action == "delete":
                    pid = data.get("id", "")
                    cur = get_model_config()
                    was_current = bool(cur and cur.provider == pid)
                    ok = custom_providers.remove(ROOT_DIR, pid)
                    if was_current:
                        # 删掉的正是当前在用的源：必须回落，否则下一条消息
                        # 会打到已经失效的地址上（用户只会看到一堆连接错误）
                        with _config_lock:
                            _model_config = cur.clone_with(
                                provider="deepseek", model="deepseek-v4-flash",
                                base_url="https://api.deepseek.com",
                                api_key=_get_provider_key(cur, "deepseek"),
                                protocol="openai", extra_headers={})
                        _save_model_config()
                        _invalidate_balance()   # 已回落，余额基线也要跟着换
                        try:
                            _load_compress_config()
                        except Exception:
                            pass
                    self._json(200, {"success": ok,
                                     "switched_back": was_current,
                                     "error": "" if ok else "未找到该模型源"})
                    return
                if action == "save_balance_auth":
                    # ── 只保存「平台管理凭据」的轻量动作（2026-09-24）──
                    # 用户实测：填了 DMXAPI 的系统令牌、点「查询余额」查到了，
                    # 但没点「保存」→ 关面板/重启后凭据就没了，而"查询成功"让人
                    # 以为已经配好。所以让查询成功时顺手把凭据落盘。
                    #
                    # 为什么单开一个动作而不是复用 save：save 的必填项是 Base URL，
                    # 且会把 name/vision/enabled/ctx_window 一并写回（缺省值会把源
                    # 的其它字段冲掉）。走 upsert 只传 {id, balance_auth}，
                    # upsert 只覆盖非 None 字段 → 精确命中这一个字段，别的一律不动。
                    pid = str(data.get("id") or "").strip()
                    if not pid:
                        self._json(400, {"success": False, "error": "缺少源 id"})
                        return
                    if not custom_providers.get(ROOT_DIR, pid):
                        self._json(404, {"success": False, "error": "未找到该模型源"})
                        return
                    sent = data.get("balance_auth")
                    if sent is None:
                        # 没带值 = 不动（与 save 里的语义一致）
                        self._json(200, {"success": True, "changed": False})
                        return
                    if not isinstance(sent, dict):
                        self._json(400, {"success": False, "error": "balance_auth 需要是对象"})
                        return
                    # {} = 显式清空；非空 = 部分更新。合并逻辑与通用 save 共用
                    # _merge_balance_auth()（那个口子上面有完整来龙去脉）。
                    new_auth = _merge_balance_auth(pid, sent)
                    saved_item, _created, dup = custom_providers.upsert(
                        ROOT_DIR, {"id": pid, "balance_auth": new_auth})
                    if dup:
                        self._json(409, {"success": False, "error": dup})
                        return
                    pub = next((x for x in custom_providers.public_list(ROOT_DIR)
                                if x["id"] == pid), None)
                    # 凭据变了 → 顶栏那条 120s 余额缓存必须作废，否则继续显示
                    # 旧凭据下的结论（"API Key 读不到余额"）
                    _invalidate_balance()
                    self._json(200, {
                        "success": True,
                        "changed": True,
                        "balance_auth": (pub or {}).get("balance_auth") or {},
                    })
                    return
                base_url = str(data.get("api_base") or data.get("base_url") or "").strip()
                if not base_url:
                    self._json(400, {"success": False, "error": "请填写 Base URL"})
                    return
                key = str(data.get("api_key") or "")
                if data.get("clear_key"):
                    # 显式清空：前端输入框不回显密钥，"空"默认理解为"不改"，
                    # 想删就得靠这个标记（否则删不掉）
                    key = ""
                elif data.get("id") and ("•" in key or not key):
                    old = custom_providers.get(ROOT_DIR, data.get("id")) or {}
                    key = old.get("api_key") or ""
                protocol = str(data.get("protocol") or "openai").lower()
                resolved = None
                # 保存前把地址"校正确认"一遍：用户把 https://站点/openai 少写一层 /v1
                # 时，归一化看不出来，只有真发一次请求才知道哪个根是通的。
                # 不校正的话就会出现"测试通过、一发消息 404"这种最气人的状态。
                if not data.get("api_base"):
                    _first = (data.get("models") or [None])[0]
                    _pmodel = _first.get("id", "") if isinstance(_first, dict) else ""
                    try:
                        resolved = custom_providers.probe(
                            base_url, key,
                            protocol if protocol in custom_providers.PROTOCOLS else "auto",
                            data.get("headers") or None,
                            timeout=8, probe_chat=True, model=_pmodel)
                        if resolved and resolved.get("ok") and resolved.get("api_base"):
                            base_url = resolved["api_base"]
                            if protocol not in custom_providers.PROTOCOLS or protocol == "auto":
                                protocol = resolved.get("protocol") or "openai"
                    except Exception as e:
                        print(f"[custom-providers] resolve base failed, keep raw: {e}")
                item = {
                    "id": (str(data.get("id") or "").strip() or None),
                    "name": str(data.get("name") or "").strip(),
                    "base_url": base_url,
                    "protocol": protocol,
                    "api_key": key,
                    "headers": data.get("headers") or {},
                    "models": data.get("models"),   # None = 保留原有清单
                    "vision": bool(data.get("vision", False)),
                    "enabled": bool(data.get("enabled", True)),
                    "ctx_window": int(data.get("ctx_window") or 0),
                    "note": str(data.get("note") or ""),
                    # None = 保留原有的手填余额；传 {} 才是"清空手填"
                    "balance_manual": data.get("balance_manual"),
                    # 平台凭据：必须走合并口子，**不能透传**。
                    # 这里以前是 data.get("balance_auth") 直接透传 = 整体替换，
                    # 而表单里「用户 ID」是回显的 → 用户每点一次「保存」都会用
                    # {user_id} 把已存的 system_token 整体抹掉（2026-09-25 复现）。
                    "balance_auth": _merge_balance_auth(
                        str(data.get("id") or "").strip(), data.get("balance_auth")),
                }
                saved, created, dup_err = custom_providers.upsert(ROOT_DIR, item)
                if dup_err:
                    # 同站点同密钥重复 / 备注重名：不落盘，让前端弹出来
                    self._json(409, {"success": False, "error": dup_err,
                                     "kind": "duplicate"})
                    return
                # 改的正好是"当前正在用的源" → 立刻同步到内存配置，
                # 不然要等重启才生效（用户会觉得"改了没用"）
                cur = get_model_config()
                if cur and cur.provider == saved["id"]:
                    with _config_lock:
                        _model_config = cur.clone_with(
                            base_url=saved["api_base"] or base_url,
                            api_key=saved["api_key"] or cur.api_key,
                            protocol=saved["protocol"],
                            extra_headers=saved["headers"] or {})
                    _save_model_config()
                    # 改的正是当前在用的源（地址/密钥/协议可能都变了）→ 余额重查
                    _invalidate_balance()
                pub = next((p for p in custom_providers.public_list(ROOT_DIR)
                            if p["id"] == saved["id"]), None)
                self._json(200, {
                    "success": True, "created": created,
                    "provider": pub,
                    "base_url": saved["base_url"],
                    "api_base": saved["api_base"],
                    "protocol": saved["protocol"],
                    # 校正结果回报给前端：地址被改过 / 校正失败都能提示用户
                    "resolved_ok": bool(resolved and resolved.get("ok")),
                    "resolve_error": ("" if (resolved is None or resolved.get("ok"))
                                      else resolved.get("error", "")),
                })
            except Exception as e:
                self._json(500, {"success": False, "error": str(e)})

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
                # 钳制到 [最小可设值, 可设上限]（未知窗口模型放行到 _WM_ABS_MAX）
                tl = int(data["token_limit"])
                tl = max(tl, _wm_min_token_limit())
                tl = min(tl, _wm_setting_ceiling())
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
            if "narrative_light_trigger" in data:
                db.set_meta("narrative_light_trigger", "1" if data["narrative_light_trigger"] else "0")
            if "fingerprint_advantage" in data:
                db.set_meta("fingerprint_advantage", "1" if data["fingerprint_advantage"] else "0")
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

        # ── Reveal file in system explorer（右键菜单"在文件资源管理器中显示"/"复制路径"）──
        # body: {topic_id, file: 工作区相对路径或绝对路径, open: bool}
        # open=True → 打开资源管理器并定位文件；open=False → 仅解析并返回绝对路径
        elif path == "/api/open-explorer":
            try:
                tid = data.get("topic_id", "")
                filename = data.get("file", "")
                want_open = data.get("open", True)
                ws_rel = self.db.get_topic_meta(tid, "workspace")
                ws_path = _resolve_workspace(ws_rel, tid)
                # filename 为相对路径时拼工作区；绝对路径（工作区外浏览）时 os.path.join 直接返回其本身
                filepath = os.path.join(ws_path, filename) if filename else ws_path
                if filename and not os.path.exists(filepath):
                    filepath = os.path.join(ROOT_DIR, "data", "missions", tid, filename)
                filepath = os.path.abspath(os.path.normpath(filepath))
                if not os.path.exists(filepath):
                    self._json(404, {"error": "file not found"})
                    return
                if want_open:
                    # ⚠️ 这里曾有一句冗余的 `import subprocess`。Python 会把 subprocess 判定为
                    # do_POST 的**局部名**，于是同一函数内其它地方用 subprocess 时（如
                    # /api/app/restart 的 Popen）直接 UnboundLocalError —— 无论执行顺序如何。
                    # "一键完全重启"按钮因此从未成功过（前端只看到 ok:false）。顶层已 import，
                    # 这里直接用全局的即可，切莫再写局部 import 遮蔽它。
                    if os.name == "nt":
                        if os.path.isdir(filepath):
                            subprocess.Popen(["explorer", filepath])
                        else:
                            # /select, 定位到具体文件并选中
                            subprocess.Popen(["explorer", "/select,", filepath])
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", filepath])
                    else:
                        subprocess.Popen(["xdg-open", filepath if os.path.isdir(filepath) else os.path.dirname(filepath)])
                self._json(200, {"success": True, "path": filepath})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 网页画中画：用系统默认浏览器打开（面板兜底出口）──
        elif path == "/api/open-external":
            try:
                target = (data.get("url") or "").strip()
                # 这里不解析 DNS：只是把地址交给本机浏览器，用户自己点的链接
                ok, why = _pip_safe_url(target, resolve=False)
                if not ok:
                    self._json(400, {"success": False, "error": why})
                    return
                if os.name == "nt":
                    os.startfile(target)  # noqa: S606 打开网址，不执行文件
                elif sys.platform == "darwin":
                    import subprocess as _sp
                    _sp.Popen(["open", target])
                else:
                    import webbrowser
                    webbrowser.open(target)
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"success": False, "error": str(e)})

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
                    # 产出文件夹：只认"真的存在且是目录"，避免存进一个错路径后
                    # 任务里再也看不到它、也删不掉（前端拿不到这个键）。
                    # 传空字符串 = 清除；键不传 = 不动。
                    if "output_dir" in data:
                        _od = (data.get("output_dir") or "").strip().strip('"').strip("'")
                        if _od:
                            if os.name == 'nt' and len(_od) == 2 and _od[1] == ':':
                                _od = _od + '\\'
                            _od = os.path.abspath(_od)
                            if not os.path.isdir(_od):
                                self._json(400, {"error": f"目录不存在或无法访问: {_od}"})
                                return
                            db.set_topic_meta(tid, "output_dir", _od)
                        else:
                            db.delete_topic_meta(tid, "output_dir")
                self._json(200, {"success": True})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Recycle: restore ──
        # dir_name = /api/recycle/list 给的键。取出快照 → 倒回 topics/messages → 从回收站移除。
        elif path == "/api/recycle/restore":
            try:
                key = (data.get("dir_name") or data.get("key") or "").strip()
                if not key:
                    self._json(400, {"error": "缺少 dir_name"})
                    return
                rec = recycle_take(key)
                if rec is None:
                    self._json(404, {"error": "回收站里找不到这条记录（可能已被恢复或清空）"})
                    return
                tid = self.db.restore_topic_snapshot(rec.get("topic"), rec.get("messages"))
                if not tid:
                    # 快照本身没内容 → 把记录放回去，别让用户白点一次就丢了
                    try:
                        recycle_put(rec)
                    except Exception:
                        pass
                    self._json(500, {"error": "快照内容为空，无法恢复"})
                    return
                n_msg = len(rec.get("messages") or [])
                print(f"[recycle] restored topic {tid[:8]} ({n_msg} msgs)")
                self._json(200, {"success": True, "topic_id": tid, "msg_count": n_msg})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── Recycle: clear ──
        elif path == "/api/recycle/clear":
            try:
                n = recycle_clear()
                print(f"[recycle] cleared {n} snapshot(s)")
                self._json(200, {"success": True, "cleared": n})
            except Exception as e:
                self._json(500, {"error": str(e)})


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
                # ── JitRL 借鉴：坑指纹优势标注（2026-10-06 点亮空壳开关）──
                # 前端策略页开关 fingerprint_advantage：开=收尾 LLM 打分写回事件账本 advantage；
                # 关=直接跳过，不发起任何 LLM 调用（零额外 token）。异步线程跑，不拖慢收尾响应。
                try:
                    from tools.task_advantage import score_task, is_enabled
                    if is_enabled():
                        threading.Thread(target=score_task, args=(task_id,),
                                         daemon=True, name="task-advantage").start()
                except Exception as _adv_e:
                    print(f"[task_advantage] 收尾钩子异常(不影响收尾): {_adv_e}")
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

        # ── 资产索引层（登记/更新/删除/设库位）──
        elif path == "/api/assets/add":
            try:
                rec = tools.asset_lib.add_asset(data)
                self._json(200, {"asset": rec})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/update":
            try:
                rec = tools.asset_lib.update_asset(str(data.get("id") or ""), data)
                if not rec:
                    self._json(404, {"error": "not found"})
                else:
                    self._json(200, {"asset": rec})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/delete":
            ok = tools.asset_lib.delete_asset(str(data.get("id") or ""))
            self._json(200 if ok else 404, {"ok": ok})
        elif path == "/api/assets/settings":
            try:
                lib_dir, copied = tools.asset_lib.set_lib_dir(str(data.get("lib_dir") or ""))
                self._json(200, {"lib_dir": lib_dir, "index_copied": copied})
            except Exception as e:
                self._json(400, {"error": str(e)})

        # ── 资产库 v3（写入/收纳/删除）──
        elif path == "/api/assets/v3/write":
            try:
                rec = tools.asset_v3.save_text(str(data.get("path") or ""), str(data.get("content") or ""))
                self._json(200, rec)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/v3/new_note":
            try:
                rec = tools.asset_v3.new_note(str(data.get("title") or "新笔记"), str(data.get("content") or ""), data.get("tags"), str(data.get("source_task") or ""))
                self._json(200, rec)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/v3/new_snippet":
            try:
                rec = tools.asset_v3.new_snippet(str(data.get("text") or ""), data.get("tags"), str(data.get("source_task") or ""))
                self._json(200, rec)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/v3/new_link":
            try:
                rec = tools.asset_v3.new_link(str(data.get("url") or ""), str(data.get("note") or ""), data.get("tags"), str(data.get("source_task") or ""))
                self._json(200, rec)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/v3/import_file":
            try:
                rec = tools.asset_v3.import_file(str(data.get("src") or ""), data.get("tags"), str(data.get("source_task") or ""))
                self._json(200, rec)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/assets/v3/delete":
            try:
                rec = tools.asset_v3.delete_entry(str(data.get("path") or ""))
                self._json(200, rec)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 工作区画布布局（POST：整包回存/加窗/改窗/删窗）──
        elif path == "/api/canvas/save":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                canvas = tools.canvas.save_canvas(tid, data.get("canvas") or {"windows": []})
                self._json(200, {"tid": tid, "canvas": canvas})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/window/add":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                win = tools.canvas.add_window(tid, data.get("window") or {})
                self._json(200, {"tid": tid, "window": win})
            except (ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/window/update":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                win = tools.canvas.update_window(tid, str(data.get("id") or ""), data.get("patch") or {})
                self._json(200, {"tid": tid, "window": win})
            except (ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/window/delete":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                tools.canvas.remove_window(tid, str(data.get("id") or ""))
                self._json(200, {"tid": tid, "ok": True})
            except KeyError as e:
                self._json(404, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})

        # ── 工作区标签（POST：新建/切换/改名/关闭/布局元数据）──
        elif path == "/api/canvas/tab/add":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                tab = tools.canvas.add_tab(tid, data.get("tab") or {}, data.get("index"))
                self._json(200, {"tid": tid, "tab": tab,
                                 "canvas": tools.canvas.load_canvas(tid)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/tab/active":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                pane = tools.canvas.set_active_tab(tid, str(data.get("id") or ""))
                self._json(200, {"tid": tid, "pane": pane})
            except KeyError as e:
                self._json(404, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/tab/rename":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                tab = tools.canvas.rename_tab(tid, str(data.get("id") or ""), data.get("title"))
                self._json(200, {"tid": tid, "tab": tab})
            except KeyError as e:
                self._json(404, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/tab/remove":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                r = tools.canvas.remove_tab(tid, str(data.get("id") or ""))
                r["tid"] = tid
                self._json(200, r)
            except KeyError as e:
                self._json(404, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/layout":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                r = tools.canvas.set_layout_meta(tid, data.get("ratio"), data.get("assetWidth"))
                r["tid"] = tid
                self._json(200, r)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/pane/split":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                lay = tools.canvas.split_pane(tid, str(data.get("id") or ""),
                                              str(data.get("side") or "right"),
                                              data.get("ratio"))
                self._json(200, {"tid": tid, "layout": lay,
                                 "canvas": tools.canvas.load_canvas(tid)})
            except KeyError as e:
                self._json(404, {"error": str(e)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif path == "/api/canvas/pane/merge":
            tid = str(data.get("tid") or self.db.get_active_topic_id() or "")
            try:
                try:
                    fi = int(data.get("from", 1))
                except (TypeError, ValueError):
                    fi = 1
                lay = tools.canvas.merge_panes(tid, fi)
                self._json(200, {"tid": tid, "layout": lay,
                                 "canvas": tools.canvas.load_canvas(tid)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})


        # ── 完全重启应用（设置 → 系统信息 → 按钮）──
        # server 是桌面壳的子进程，自己没法"杀爹再重生"：真正动刀的是脱离子进程
        # _app_restart.py —— 先回本响应，助手延迟 1.2s 后杀桌面壳整棵树 + 旧 server，
        # 等端口释放再重新拉起 desktop.py，前后端一起重载。
        elif path == "/api/app/restart":
            busy = [tid for tid, w in get_working_all().items()
                    if isinstance(w, dict)
                    and w.get("status") not in ("idle",) + _TERMINAL_STATUS]
            if busy:
                self._json(409, {"ok": False,
                                 "error": "有任务正在运行（话题 %s），等它结束再重启" % ", ".join(busy[:3])})
                return
            try:
                helper = os.path.join(ROOT_DIR, "_app_restart.py")
                if not os.path.isfile(helper):
                    self._json(500, {"ok": False, "error": "缺少 _app_restart.py"})
                    return
                port_now = self.server.server_address[1]
                subprocess.Popen(
                    [sys.executable, helper, str(os.getpid()), str(port_now)],
                    cwd=ROOT_DIR,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        # ── 公网访问（设置 → 公网访问）──
        # save   只存配置+生成 frpc.toml/tokens.txt
        # apply  存配置并**重启链路**（改端口/换服务器后必须走这个）
        # start  等同 apply 但强制 enabled=true；stop 停链路并落盘 enabled=false
        # test   公网端到端探活；regenerate 换一套访问凭证/加密密钥
        elif path.startswith("/api/tunnel/"):
            if tunnel_manager is None:
                self._json(500, {"ok": False, "error": f"tunnel_manager 未加载: {_TM_IMPORT_ERR}"})
                return
            tm = tunnel_manager
            act = path[len("/api/tunnel/"):].strip("/")
            try:
                if act == "save":
                    cfg, err = tm.save_config(data)
                    if err:
                        self._json(400, {"ok": False, "error": err})
                        return
                    tm.write_artifacts(cfg)
                    self._json(200, {"ok": True, "config": tm.config_view(cfg, reveal=True)})

                elif act in ("apply", "start"):
                    nxt = dict(data or {})
                    nxt["enabled"] = True
                    cfg, err = tm.save_config(nxt)
                    if err:
                        self._json(400, {"ok": False, "error": err})
                        return
                    tm.ensure_running(cfg, fresh=True)
                    st = tm.status(cfg)
                    self._json(200, {"ok": True, "status": st,
                                     "config": tm.config_view(cfg, reveal=True)})

                elif act == "stop":
                    cfg, err = tm.save_config({"enabled": False})
                    if err:
                        self._json(400, {"ok": False, "error": err})
                        return
                    stopped = tm.stop_link(cfg)
                    self._json(200, {"ok": True, "stopped": stopped, "status": tm.status(cfg)})

                elif act == "test":
                    self._json(200, {"ok": True, "result": tm.test_public()})

                elif act == "regenerate":
                    cfg = tm.regenerate_credentials()
                    cfg, err = tm.save_config(cfg)
                    if err:
                        self._json(400, {"ok": False, "error": err})
                        return
                    tm.write_artifacts(cfg, force=True)
                    if cfg.get("enabled"):
                        # 守门员每次现读 tokens.txt，但可能还跑在旧目录读着旧文件 ——
                        # 停掉让它按新路径/新凭证重起；frpc 不动，隧道不闪断。
                        tm.stop_guard()
                        tm.ensure_running(cfg)
                    self._json(200, {"ok": True, "config": tm.config_view(cfg, reveal=True)})

                else:
                    self._json(404, {"error": f"unknown tunnel action: {act}"})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

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
    # ── 同文本幽灵重发判定窗（秒）────────────────────────────────
    # 2026-09-22 实证：手机端发出的长句在 2544s（42分钟）后被重放（链路超时断连 → 浏览器重发），
    # 越过原 1800s 窗，二次入库 + 白跑一整轮 agent。而用户"有意重问"的长句几乎不可能一字不差。
    # 短消息（"继续"/"嗯？"/"好了吗"）用户确实会隔几十分钟重新说一次 → 保持短窗不误伤。
    _GHOST_WIN_LONG = 6 * 3600   # 长消息：网络层重放窗口
    _GHOST_WIN_SHORT = 1800      # 短消息：保持原 30 分钟窗
    _GHOST_LONG_LEN = 10         # 去空白后 ≥ 此长度算"长消息"

    def _ghost_window(self, message):
        t = (message or "").strip()
        return self._GHOST_WIN_LONG if len(t) >= self._GHOST_LONG_LEN else self._GHOST_WIN_SHORT

    def _reply_after(self, tid, ts):
        """取 ts 之后**最早**一条有正文的最终 AI 回复，返回 (文本, 是否存在)。
        取最早而不是最新：这个文本是给"被重放的那次发送"当回执用的，必须紧挨着它；
        取最新的会把后面别的问题的答案当成本次回执（会串答案）。"""
        try:
            r = self.db._fetchall(
                "SELECT text FROM messages WHERE topic_id=? AND role='ai' AND ts>? "
                "AND (args IS NULL OR args NOT LIKE '%\"process\"%') "
                "AND length(trim(text)) > 0 "
                "ORDER BY ts ASC, id ASC LIMIT 1", (tid, ts))
            if r:
                return (r[0]["text"] or "", True)
        except Exception:
            pass
        return ("", False)

    def _find_user_msg_by_cmid(self, tid, cmid):
        """按客户端幂等键（cmid，存在 args JSON 里）查已入库的用户消息，返回 (msg_id, ts)。"""
        if not cmid:
            return (None, None)
        needle = json.dumps({"cmid": cmid}, ensure_ascii=False)[1:-1]  # '"cmid": "abc123"'
        try:
            rows = self.db._fetchall(
                "SELECT id, ts FROM messages WHERE topic_id=? AND role='user' AND ts>? "
                "AND args LIKE ? ORDER BY id DESC LIMIT 1",
                (tid, time.time() - 7 * 86400, "%" + needle + "%"))
        except Exception:
            return (None, None)
        if rows:
            return (rows[0]["id"], rows[0]["ts"])
        return (None, None)

    def _recent_same_text_gap(self, tid, message, window=None):
        """在最近用户消息（回看20条）中找同文本，命中返回 (间隔秒数, 该消息ts)，否则 (None, None)。
        window 由 _ghost_window() 决定（长/短消息不同窗）。
        2026-09-03 补强：原实现只比对最后一条用户消息，幽灵重发前若插过一句别的话即漏网。
        2026-09-23 补强：窗内命中有多条时取**最早**那条（不再一命中就返回最新那条），
        让调用方的"其后是否有最终回复"覆盖整段重复——否则重放第三次时（第二、三次都没回复）
        会因为"最新那条之后没有回复"而重新漏网（实测 T3 就漏了）。"""
        if window is None:
            window = self._ghost_window(message)
        try:
            rows = self.db._fetchall(
                "SELECT text, ts FROM messages WHERE topic_id=? AND role='user' "
                "ORDER BY ts DESC, id DESC LIMIT 20", (tid,))
        except Exception:
            return (None, None)
        msg_norm = (message or "").strip()
        if not msg_norm:
            return (None, None)
        now = time.time()
        hit = None
        for row in rows:
            if ((row["text"] or "").strip()) != msg_norm:
                continue
            ts = row["ts"] or 0
            ts_s = ts / 1000.0 if ts > 1e14 else ts
            gap_s = now - ts_s
            if 0 <= gap_s <= window:
                hit = (gap_s, ts)  # 取窗内**最早**那条（继续往回找），
                continue           # 让调用方的"其后有没有回复"覆盖整段重复记录
            break  # 同文本但已超窗：视为有意重发，不再往更老找
        return hit if hit is not None else (None, None)

    def _log_guard(self, msg):
        """守卫拦截日志落盘：pythonw 下 print 全部丢失，文件留证便于排查重复消息。"""
        try:
            with open(os.path.join(ROOT_DIR, "data", "guard.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except Exception:
            pass

    def _dup_check(self, tid, message, window=None, quiet=False):
        """幽灵重复守卫：消息文本与库内最近一条用户消息相同且在时间窗内——
        ≤10秒：判双击/重复提交，直接拦截；
        2026-09-07 实证：幽灵重发不止发生在运行中(897s)，也出现在回答完整结束后(725s≈12分钟)。
        窗内且其后已有最终 AI 回复：判定幽灵重发，拦截并返回已持久化的回复
        （首次请求其实已到服务端入库，只是响应没走回来，直接返还省一轮 AI 运行）。
        2026-09-22 实证：手机端发长句 2544s(42分钟)后重放（链路 600s 超时断连 → 浏览器重开 app 时
        重发同一条 POST），越过原 1800s 统一窗 → 二次入库 + 白跑一轮 agent。
        故窗长改为按文本长度取（见 _ghost_window）：长句 6 小时，短句仍 30 分钟。
        无最终回复时不在此拦：agent 运行中由注入分支按同文本拦截（防二次入库），
        agent 已死则放行保留补救路。
        返回 None 放行；返回 dict 则拦截（作为 200 JSON 响应）。

        quiet=True：**只判定、不落日志** —— 供"带 cmid 时的放行前预检"使用。
        （本函数命中时会写"…已拦截"日志；预检若照写，凡是真放行的都会在 guard.log
          里留下一行假的"已拦截"，把证据本身搞脏。见 2026-10-07 那次实测。）
        """
        if window is None:
            window = self._ghost_window(message)
        gap_s, last_ts = self._recent_same_text_gap(tid, message, window)
        if gap_s is None or gap_s > window:
            return None  # 超窗：视为真实新消息（用户有意重复提问）
        reply_text, has_reply = self._reply_after(tid, last_ts)
        if gap_s <= 10 or has_reply:
            tag = "重复提交" if gap_s <= 10 else "幽灵重发"
            if not quiet:
                self._log_guard(f"[chat] {tag}已拦截 (窗{int(window)}s 间隔{int(gap_s)}s, topic {tid[:8]}, 首句{message[:30]!r})")
            return {"status": "duplicate", "response": reply_text,
                    "usage": {"total_tokens": 1}, "topic_id": tid}
        return None  # 尚无最终回复：agent 可能在跑（注入分支会拦同文本）或已死（放行补救）

    def _handle_chat(self, data):
        """Chat: LLM → tool loop → track cost → JSON response."""
        global _round_peak_tokens
        _chat_t0 = time.time()  # 本次任务起始时刻：落最终回复时算"耗时"，供消息底部 meta 显示
        _round_peak_tokens = 0  # 用户新消息 = 新大轮开始，重置工作记忆峰值
        message = data.get("message", "").strip()
        tid = data.get("topic_id", "")
        attachments = data.get("attachments", []) or []
        # 客户端幂等键：同一次"发送"的稳定 id（前端每次点击发送生成一个，网络层重放时不变）
        _cmid = str(data.get("client_msg_id") or "").strip()
        _image_abs_paths = []  # 图片附件绝对路径（用于 vision 多模态）
        if not message and not attachments:
            self._json(400, {"error": "message required"})
            return
        topic = self.topics.get_or_create(tid)
        tid = topic["id"]
        _task_config = get_task_model_config(tid)
        _configured_budget = int(self.db.get_topic_meta(tid, 'context_budget') or self.db.get_meta('compress_token_limit') or TOKEN_LIMIT)
        _cp = _custom_provider(_task_config.provider) or {}
        # 预算上限同样走 _wm_setting_ceiling：原来直接查 _MODEL_CONTEXT_WINDOWS，
        # 未登记模型（聚合站别名 / 私有网关）一律被 128K 截断，和用户设的工作记忆容量对不上。
        _task_budget = min(_configured_budget,
                           int(_cp.get('ctx_window') or _wm_setting_ceiling(_task_config.model)))
        _request_runtime.budget = _task_budget
        task_runtime.begin(tid, _task_config.model, _task_budget)
        # Sync active topic so AI tools (current_topic etc.) see the right context
        self.db.set_active_topic_id(tid)
        # Ensure mission directory and workspace exist（标准形态不再写 meta，_resolve_workspace 按 tid 现算）
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
        # Auto-set workspace if not already set —— 已废弃：标准形态不写 meta（_resolve_workspace 按 tid 现算），
        # 避免反复写回机器绑定绝对路径（旧机器残留导致的漂移根因之一）
        # 附件清单存进 args，前端刷新后能恢复 chip；顺带存客户端幂等键 cmid
        _msg_args = {}
        if attachments:
            _msg_args["attachments"] = attachments
        if _cmid:
            _msg_args["cmid"] = _cmid
        _msg_args = _msg_args or None

        # ―― 客户端幂等键（cmid）：同一次发送被网络层重放时，不看时间窗、精确拦截 ――
        # 场景（2026-09-22 实证）：链路在 agent 跑完前超时断连（守卫 600s 超时 → ERR upstream /api/chat: timed out），
        # 手机端浏览器在 app 重开时把这条 POST 原样重放（Chromium 对"已复用连接在收到任何响应字节前被关闭"的请求会重发）。
        # 文本守卫只能靠时间窗猜（且会漏 42 分钟这种长间隔），cmid 是精确命中，与间隔无关。
        if _cmid:
            _hit_id, _hit_ts = self._find_user_msg_by_cmid(tid, _cmid)
            if _hit_id:
                _rep_txt, _rep_ok = self._reply_after(tid, _hit_ts)
                self._log_guard(f"[chat] cmid 重放已拦截 (cmid={_cmid[:12]} msg#{_hit_id} topic {tid[:8]} 首句{message[:30]!r})")
                self._json(200, {"status": "duplicate", "response": _rep_txt if _rep_ok else "",
                                 "usage": {"total_tokens": 1}, "topic_id": tid})
                return

        # ―― 幽灵重复守卫：窗口内同文本重复提交直接拦截，防二次入库 ――
        # 🔴 2026-10-07 修正：**带 cmid 时跳过文本守卫**（文本守卫只留给**不带 cmid 的旧客户端**兜底）。
        #   上面那段已经按 cmid 精确判过：带 cmid 还能走到这里
        #   = 这个 cmid 库里查无此条 = 这条消息确实没入库过 = **绝不是重放**。
        #   此时再按"同文本 + 时间窗"去猜，就会把"用户看完报错说明后原样再发一遍"当成重放吞掉：
        #     18:23:52 发消息 → 429 → 落 ⚠️ 中断说明（role=ai，算"其后已有回复"）
        #     18:24:41 用户照提示原样再发同一句 → 判"幽灵重发"（窗21600s 间隔48s）
        #              → 不入库、不跑 agent、只把旧 ⚠️ 文本当回执返回
        #              → 前端因回执与已有 AI 消息同文而跳过渲染 ⇒ 界面彻底静默
        #   （guard.log 2026-10-07 18:24:41 实证；2026-09-30 连吞三条"继续"同因。）
        if _cmid:
            # 仅在"文本守卫本会命中"时留痕：日后回看"这次为什么没被拦"有据可查，平时不刷日志。
            # quiet=True：预检不得写"已拦截"日志，否则真放行的也会在 guard.log 里留下假拦截。
            if self._dup_check(tid, message, quiet=True) is not None:
                self._log_guard(
                    f"[chat] 文本守卫本会命中但已放行（带新cmid={_cmid[:12]} "
                    f"topic {tid[:8]} 首句{message[:30]!r}）")
        else:
            _dup = self._dup_check(tid, message)
            if _dup is not None:
                self._json(200, _dup)
                return
        # ── 插话：该话题已有运行中的 agent 循环时，不起第二个并行循环。
        # 消息落库后进入注入队列，运行中的循环会在轮次边界取出、插入上下文继续思考。
        with _cancel_lock:
            _loop_active = _cancel_events.get(tid) is not None
        if _loop_active:
            # agent 运行中收到的同文本必是幽灵重发/重复提交（差异文本才是真插话）：
            # 不二次入库、不注入，返回空回复让前端转入恢复轮询等最终结果。
            # 2026-09-03 实证：22:51 消息运行15分钟，23:06 幽灵重发(间隔897s)曾越过 ≤600s 窗被当真插话，
            # 二次入库+二次作答白烧一轮 token——回看窗已扩到30分钟：agent 没跑完时收到的同文本
            # (≤30分钟)必是幽灵重发，一律拦；超30分钟视为有意重问，放行插话。
            # 窗长按文本长度取（长句 6h / 短句 30min），长句重放不再被当"真插话"二次入库
            _inj_gap, _inj_ts = self._recent_same_text_gap(tid, message, self._ghost_window(message))
            if _inj_gap is not None:
                self._log_guard(f"[chat] 运行中同文本重发已拦截 (间隔{int(_inj_gap)}s, topic {tid[:8]}, 首句{message[:30]!r})")
                self._json(200, {"status": "duplicate", "response": "", "topic_id": tid})
                return
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
        # 展示用短路径：显示实际生效的工作区（相对项目根），而非 meta 原值
        try:
            ws_short = os.path.relpath(ws, ROOT_DIR).replace("\\", "/")
        except ValueError:
            ws_short = ws
        if len(ws_short) > 60: ws_short = "..." + ws_short[-57:]
        # 产出文件夹：用户为这个任务额外指定的交付目录（工作区不变，只是多一个可读写目录）。
        # 目录已不存在（拔盘/被删）时本轮按"没设置"处理 —— 但 meta 值保留，
        # 前端仍能看到"我设过它"，不至于静默丢失。
        _output_dir = (self.db.get_topic_meta(tid, "output_dir") or "").strip()
        if _output_dir and not os.path.isdir(_output_dir):
            print(f"[workspace] ⚠️ 产出文件夹不可用，本轮忽略: {_output_dir}")
            _output_dir = ""
        # Normal flow — add message and build history

        store = _get_memory_store()
        # ── Build system message (v4.0 structured blocks) ──
        blocks = []

        # Block 1: 身份与行为规则（重要事项）—— 视角敏感的标题
        from tools.perspective import vocab as _vocab
        matters = _get_matters(tid)
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
            from tools.domain_book_tools import get_active_pages_info, _load_book, resolve_active_pages

            active_pages = get_active_pages_info(tid)

            book = _load_book()

            all_pages = book.get("pages", {})

            active_ids, _book_src = resolve_active_pages(book, tid)
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
        # ── 路径句柄（2026-10-22）──
        # 让模型手拼长绝对路径必然出错（层级/中文名/反斜杠各有各的错法），所以给两个
        # 短句柄由执行端展开：工作区/ 、产出/ 。出口侧工具返回的路径也是句柄形式，
        # 模型"看到什么写什么"即可闭环。详见 tools/file_tools._ref_path。
        _path_hint = '\n路径写法: 路径参数优先用句柄 —— "工作区/<相对路径>"'
        if _output_dir:
            _path_hint += '，产出文件夹用 "产出/<相对路径>"'
        _path_hint += '；bash 里用 $BIGEYE_WS' + (' / $BIGEYE_OUT' if _output_dir else '') \
                    + '。别手拼整条绝对路径（层级和中文名最容易错）。'
        task_header += _path_hint
        if _output_dir:
            _od_show = _output_dir if len(_output_dir) <= 90 else "..." + _output_dir[-87:]
            task_header += (
                "\n产出文件夹: " + _od_show
                + "\n  · 这是用户为本任务指定的交付目录（在工作区之外，同样可读写）。成品、交付物优先写这里，"
                "引用它用 \"产出/<文件名>\" 即可，不用抄下面这行绝对路径。"
            )
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
            active_tid = tid
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
                # 取最近 8 条（留余量给分组），Python 端三档分组：
                # ① 本任务故事优先（最多3条，不加标） ②其余按时间补位到5条
                # ③ 跨任务/无归属条目行尾加 [别的任务] 标注（2026-09-04 串台修复）
                # 无归属按跨任务处理：误标无害，漏标有害
                _rows = _conn.execute(
                    "SELECT id, text, tags, topic_id, event_time, importance, authority_level, "
                    "created_at FROM memory_fragments "
                    "WHERE layer='story' AND dirty=1 "
                    "ORDER BY (event_time IS NULL), substr(event_time,1,10) DESC, created_at DESC LIMIT 8"
                ).fetchall()
                _conn.close()

                if _rows:
                    # 按 event_time 排序（最近在前）；无 event_time 的排最后，用 created_at 兜底
                    def _story_et_key(r):
                        et = r["event_time"] or ""
                        return et[:10]  # '2026-07-11 ~ 2026-07-15' 取起始日
                    _rows.sort(key=lambda r: (_story_et_key(r) or "0000-00-00"), reverse=True)

                    _selected = _select_stories(_rows, tid)  # [(row, other_tag), ...]

                    story_lines = []
                    for r, _is_other in _selected:
                        auth = " ★" if r["authority_level"] >= 1 else ""
                        # 跨任务/无归属标注（防串台误联想）
                        _other = " [别的任务]" if _is_other else ""
                        text = r["text"] or ""
                        # 紧凑时间线：取第一句做摘要，限 60 字
                        _first_sent = re.split(r"[。！？!?]", text)[0].strip() if text else ""
                        _sum = _first_sent[:60] + "…" if len(_first_sent) > 60 else _first_sent
                        _et = r["event_time"] or ""
                        _et_disp = _et.replace(" ~ ", "~") if _et else ""
                        story_lines.append(f"📖 {_et_disp} {_sum}{auth}{_other}")
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
                    all_fragments = store.recall(recall_ctx, top_k=15, threshold=0.35,
                                                 current_topic=tid)
                    # 记忆 Block 只显示 core + knowledge，story 已在 Block 5a 常驻
                    non_stories = [f for f in all_fragments if (f.get("layer") or "core") != "story"]

                    if non_stories:
                        now_ts = _time.time()
                        # 2026-10-15 21:30 修复：删除注入层二次重排，直接沿用 store.recall 的返回顺序
                        # 依据（同一候选池、仅换排序函数、153 条正样本）:
                        #   旧 freshness 13.7% < 现役 _score 44.4% < 召回原序 68.6% < 纯 relevance 76.5%
                        # store 层已完成 relevance 排序 + MMR 去重；注入层再乘 recency^0.10 等因子
                        # 会跨数量级扰动，把"老而准"的记忆按下去。故此处不再重排。
                        # 保留：同 topic≤2 多样性裁剪 + 权威知识保底（下方逻辑不变）
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
                        # 权威知识保底：行为准则类 knowledge 不赌语义召回（间接表达召回不了），
                        # 按 weight*importance 取前2补进注入，已在 selected 的跳过
                        try:
                            _guar = store.recall_guaranteed(
                                exclude_ids={f.get("id") for f in selected}, cap=2)
                            if _guar:
                                selected.extend(_guar)
                        except Exception as _ge:
                            print(f"[memory] guaranteed recall failed: {_ge}")
                        # ── 旁路注入日志（2026-10-15）──────────────────────
                        # 规则：绝不往注入正文里加 #id —— 正文是模型读到的 prompt，
                        # 加可见标记本身就是一次注入干预（弃答 v1/v2 已证净伤害）。
                        # 所以另落一行日志，零 prompt 改动。
                        # 这是将来训排序器的唯一数据来源：推了谁 → 后来被引用没 → 任务成没成。
                        try:
                            import json as _json, os as _os
                            _push_rec = {
                                "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                                "epoch": now_ts,
                                "topic_id": tid,
                                "query": (message or "")[:200],
                                "n_candidates": len(non_stories),
                                "selected": [
                                    {
                                        "id": f.get("id"),
                                        "layer": f.get("layer") or "core",
                                        "topic_id": f.get("topic_id"),
                                        "relevance": round(float(f.get("relevance")
                                                              or f.get("_relevance") or 0.0), 4),
                                        "weight": f.get("weight"),
                                        "importance": f.get("importance"),
                                        "authority_level": f.get("authority_level", 0),
                                        "causal": bool(f.get("_causal_propagation")),
                                        "text_head": (f.get("text") or "")[:60],
                                    }
                                    for f in selected
                                ],
                            }
                            _push_log = _os.path.join(
                                _os.path.dirname(_os.path.abspath(__file__)),
                                "data", "memory_push_log.jsonl")
                            with open(_push_log, "a", encoding="utf-8") as _lf:
                                _lf.write(_json.dumps(_push_rec, ensure_ascii=False) + "\n")
                        except Exception as _le:
                            print(f"[memory] push log failed: {_le}")

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
        # 取数窗口：按话题**全量**取，而不是固定 500 行（2026-10-21 修）。
        # 旧实现 limit=500 是"最近 500 条原始行"，而原始行里工具返回/过程叙述占绝大多数：
        # 实测「蓝牙控制器」7507 行中 500 行只含 **7 条真人对话**（全量 360 条），于是
        #   (a) 模型只看得到最近 7 轮，更早内容静默消失，且这条路径不经过任何预算判定；
        #   (b) _compress_context 的条数墙（MSG_COUNT_WALL=50）永远不触发
        #       ⇒ 后台整理（丢弃→摘要）永不启动，整理树覆盖率停在 7–10%。
        # 现在把"留多少"交还给预算与条数墙：超墙 → _trim_context_to_budget 按对话边界
        # 从前裁剪 → on_dropped 后台整理成摘要节点。20000 行仅作极端话题的安全上限。
        total_db_msgs = self.db.message_count(tid)
        _FETCH_ROW_CAP = 20000
        _fetch_limit = total_db_msgs if total_db_msgs <= _FETCH_ROW_CAP else _FETCH_ROW_CAP
        history_raw = self.db.get_messages(tid, limit=_fetch_limit, skip_process=False)
        # Detect limit truncation — messages older than the fetch window are lost
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
                        "_anchor": anchor,
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
                        history_msgs.append({"role": "assistant", "content": text,
                                             "_db_id": msg_id, "_anchor": anchor})
                    elif role == "user":
                        history_msgs.append({"role": "user", "content": text,
                                             "_db_id": msg_id, "_anchor": anchor})
                anchor += 1
                continue
            if role == "ai":
                if not (text or "").strip():
                    anchor += 1
                    continue
                # _anchor = 话题内全局非 tool 序号，与 compression_tree 的锚点同一套坐标系。
                # 只在内存里用（发送前 llm.py 会剥掉所有 _ 开头字段），用于裁剪时精确标定
                # "被丢掉的是哪一段"，好让后台整理写出锚点正确的节点。
                history_msgs.append({"role": "assistant", "content": text,
                                     "_db_id": msg_id, "_anchor": anchor})
            elif role == "user":
                history_msgs.append({"role": "user", "content": text,
                                     "_db_id": msg_id, "_anchor": anchor})
            anchor += 1
        # Set workspace context for file/bash/image tools (thread-local)
        tools.file_tools.set_workspace(ws)
        tools.bash.set_workspace(ws)
        tools.image_gen_tool.set_workspace(ws)
        # 用户授权开关：是否允许 AI 跨出工作区访问/写入
        _allow_outside = (self.db.get_topic_meta(tid, "allow_outside") == "1")
        tools.file_tools.set_allow_outside(_allow_outside)
        tools.bash.set_allow_outside(_allow_outside)
        # 产出文件夹：不是拆围墙，是在墙上多开一扇门 —— 只放开这一个目录，
        # 其余越权判定仍按 allow_outside。bash 走 file_tools._check_boundary，自动生效。
        try:
            tools.file_tools.set_extra_roots([_output_dir] if _output_dir else [])
        except Exception as _od_err:
            print(f"[workspace] ⚠️ set_extra_roots 失败（老工具模块？）: {_od_err}")
        # Cancel event — allows /api/stop-thinking to interrupt streaming
        cancel_evt = threading.Event()
        with _cancel_lock:
            _cancel_events[tid] = cancel_evt
        full_thinking = ""
        # 【2026-10-17】full_thinking 是"整段 agent 会话"的累积值，只用于实时思考泡
        # 显示（重置会让用户看到的思考内容突然变短/跳段，见 §20 的坑）。
        # 落库必须用 **本轮** 的增量 _turn_thinking —— 旧实现在每条工具轮消息上都写
        # 全量累积值，实测单条达 88,512 字符（任务 6d895c38e809，02:52-02:53 一分钟
        # 内 30+ 条逐条递增），十几轮下来同一条思考链被重复存了几十遍。
        _turn_thinking = ""
        full_response = ""
        full_intermediate = ""  # 工具轮的动作叙述（"好嘞去抓官网…"），换行累积；最终答案不进这里
        total_input_tokens = 0
        total_output_tokens = 0

        _est_input = 0  # 源不返回 usage 时的兜底估算累计（每轮实际发出的 messages，见下方累加点）

        def _task_stats_args():
            """本次任务的耗时与 Token 合计 —— 写进最终 AI 消息的 args，前端按开关决定是否展示。
            只挂在"最终回复"上（工具轮过程叙述不带），与任务级统计语义一致。

            Token 优先用服务端真实 usage；拿不到时（实测部分聚合站的流式
            usage 恒为 {}，stream_options.include_usage 也不生效）退回本地估算，
            并置 est=True 让前端加 "~" 前缀 —— 宁可标注成估算值，也不要给用户一个恒为 0 的数。
            """
            _in, _out = int(total_input_tokens), int(total_output_tokens)
            _est = False
            if _in + _out <= 0:
                _est = True
                _in = int(_est_input)
                _out = int((len(full_response) + len(full_intermediate) + len(full_thinking)) / 1.6)
            return {"elapsed": round(max(0.0, time.time() - _chat_t0), 1),
                    "tokens": _in + _out,
                    "est": _est}

        turn = 0
        # 系统注入的动态状态（时间/任务图/记忆/上下文用量）：每轮变，只能追在 messages 末尾，
        # 不能进 system —— 否则前缀缓存全废。用可变容器让 loop 内降级时也能更新。
        _dyn_state = {"text": "", "injected": False}

        def _send_view(_msgs):
            """实际发给模型的 messages：首次发送前把动态状态追到末尾。

            ⚠ 只追加**一次**：若每次工具往返都重追，模型会看到
            「tool 结果 → 又一条 user 消息」，容易误判成用户插话而中断当前动作。
            首次追加后它就是历史的一部分（位置在末尾，裁剪不会先切它），
            后续轮次自然带着，前缀仍然连续。
            """
            if _dyn_state.get("injected"):
                return _msgs
            _t = _dyn_state.get("text") or ""
            if not _t:
                return _msgs
            _msgs.append({"role": "user", "content": _DYN_PREFIX + "\n\n" + _t})
            _dyn_state["injected"] = True
            return _msgs

        msgs = _build_messages(system_msg=system_msg, history=history_msgs, new_msg=message)
        current_msg = msgs[-1]
        old_msgs = _compress_context(msgs[:-1], tid, store, self.db)
        msgs = old_msgs + [current_msg]
        llm_config = _task_config
        # ── 看图：图片附件 → 多模态 content + 选对"能收图"的模型 ──
        # 见 _prepare_vision_request：老实现只换 model 名不换 provider/base_url，
        # 于是本地 provider 下图片仍被发到不支持视觉的本地服务 → HTTP 500。
        if _image_abs_paths:
            llm_config, message, _mm, _note = _prepare_vision_request(
                llm_config, message, _image_abs_paths)
            if _mm is not None:
                current_msg["content"] = _mm
            else:
                current_msg["content"] = message
                msgs = strip_image_content(msgs, llm_config.model, vision_ok=False)
        else:
            # 非看图轮次：剥离历史残留的图片 content，避免纯文本模型报错
            msgs = strip_image_content(msgs, llm_config.model,
                                       vision_ok=_vision_capable(llm_config))
        _inject_context_hints(msgs, tid, limit_truncated=limit_truncated)
        # 🔴 system 必须是恒定前缀，否则 tools schema（约 11k tok）每轮重算，
        # 缓存命中率从 99.6% 掉到 26.6%。动态段下沉到末尾单独一条（见 _split_system_dynamic）。
        msgs[0], _dyn_state["text"] = _split_system_dynamic(msgs[0])
        if _dyn_state["text"]:
            print(f"[cache] system 已拆：恒定 {len(msgs[0]['content'])} 字 / "
                  f"动态 {len(_dyn_state['text'])} 字（下沉到末尾，保住 tools 前缀缓存）")
        # 缓存调试快照：包含 messages + tools + token 拆解
        # 之前只存 msgs，导致工具 schema（可达 13k+ tokens）不可见，调试时对不上 token 数
        _active_tools = _get_enabled_tool_defs(tid)
        _tools_tokens = _estimate_tools_tokens(_active_tools)
        _snapshot = {
            "model": llm_config.model,
            "provider": llm_config.provider,
            "messages": _send_view(msgs),
            "tools": _active_tools,
            "tool_count": len(_active_tools),
            "estimate": {
                "messages_tokens": _estimate_tokens(_send_view(msgs)),
                "tools_tokens": _tools_tokens,
                "total_estimated": _estimate_tokens(_send_view(msgs)) + _tools_tokens,
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
        # ── 任务上下文通道：把本线程绑定到当前任务（工具层据此读归属，治串台）──
        try:
            from tools.task_context import set_current_topic
            set_current_topic(tid)
        except Exception as _ctx_err:
            print(f"[agent] set_current_topic failed: {_ctx_err}")
        print(f"[agent] task started, budget {remaining} turns")
        set_working(tid, "thinking", turn=0, remaining=remaining)
        _budget_warned = False  # 每个预算周期只提醒一次
        _budget_exhausted = False
        _auto_downgraded = False  # 上下文压力被动降级标记（每任务最多降一次）
        _turn_retries = 0  # 连接层瞬时故障的轮级重试计数
        _dangling_retries = 0  # 防悬空检测：每请求最多强制续跑 2 次
        _resume_retries = 0  # 流式中断续写（A 方案）：每请求最多续写 2 次
        _empty_final_retries = 0  # 空收尾（无工具无正文）：每请求最多逼问结论 2 次
        _carry = ""  # 流式中断续写：承接被提前掐断的那半段正文
        _orig_model = llm_config.model       # 本请求最初选的主模型（换源后仍用于提示文案）
        _fallback_tried = set()              # 已换过的备用源 "provider/model"
        _fell_back = []                      # 换源记录，最终报错时告诉用户试过哪些
        while remaining > 0:
            _usage_settlement_id = uuid.uuid4().hex
            _turn_thinking = ""  # 本轮思考（落库口径；实时泡仍用 full_thinking）
            # ── 每轮自检运行时绑定（防串任务）──
            # 工具层的"我在哪个任务"来自本线程的绑定（tools/task_context）。
            # 一旦绑定丢了，工具层会静默回落到全局活跃任务 = 用户此刻点开的任务，
            # 于是本循环会替**别的任务**干活、消息却写回自己的话题里。
            # 2026-09-24 实证：19:26/19:27 另一任务调用 hot_reload（会 reload
            # tools/*.py 里的 task_context，模块级 threading.local 被重建），
            # 18:57 起就在跑的进销存循环当场丢绑定 → 19:41 起跑了 1h50m 的视频任务。
            # 绑定已改为跨 reload 稳定（tools/registry.stable_local）；这里是第二道
            # 保险：任何原因导致归属不对，最迟下一轮恢复。只在"不对"时才动工作区，
            # 免得把用户中途切的工作区/越权开关覆盖回去。
            try:
                from tools.task_context import explicit_topic as _explicit_topic
                from tools.task_context import set_current_topic as _set_ctx
                if _explicit_topic() != tid:
                    _set_ctx(tid)
                    tools.file_tools.set_workspace(ws)
                    tools.bash.set_workspace(ws)
                    tools.image_gen_tool.set_workspace(ws)
                    tools.file_tools.set_allow_outside(_allow_outside)
                    tools.bash.set_allow_outside(_allow_outside)
                    try:
                        tools.file_tools.set_extra_roots([_output_dir] if _output_dir else [])
                    except Exception:
                        pass
                    print(f"[agent] task context re-bound -> {tid[:8]} (turn {remaining})")
            except Exception as _ctx_rebind_err:
                print(f"[agent] task context self-check failed: {_ctx_rebind_err}")
            has_tool_calls = False
            # 续写时以被掐断的前半段为起点；否则最终答案只剩后半截
            turn_text = _carry
            _carry = ""
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

            # 兜底估算累加点：不管源给不给 usage 都累计，只有真拿不到真实值时才被采用。
            # 多轮任务必须逐轮累加（每轮 msgs 都在长），只取最后一轮会明显低估；
            # 工具 schema 每轮都随请求发出去，也算进 input —— 工具定义约 13k tok，漏掉会少算一半。
            # 已识别的严格思考源：后续每轮直接回传，不再先挨一次400。
            if (llm_config.base_url, llm_config.model) in _REASONING_ECHO_MODELS:
                for _m in msgs:
                    if _m.get("role") == "assistant" and _m.get("tool_calls"):
                        _m.setdefault("reasoning_content", _m.get("_thinking") or "")
            _tool_defs = _get_enabled_tool_defs(tid)
            try:
                _est_input += _estimate_tokens(_send_view(msgs)) + _estimate_tools_tokens(_tool_defs)
            except Exception:
                pass

            for event in chat_stream_cached(llm_config, _send_view(msgs), tools=_tool_defs, cancel_event=cancel_evt, verify_ssl=_VERIFY_SSL):
                et = event["type"]
                if et == "thinking_delta":
                    full_thinking += event["delta"]
                    _turn_thinking += event["delta"]
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
                                         cache_hit=_cache_hit, model=llm_config.model, settlement_id=_usage_settlement_id)
                        global _last_request_tokens
                        _last_request_tokens = turn_usage["input"]
                        if turn_usage["input"] > _round_peak_tokens:
                            _round_peak_tokens = turn_usage["input"]  # 大轮内峰值实时刷新
                        _recent_input_tokens.append(turn_usage["input"])
                        task_runtime.record(tid, turn_usage['input'], llm_config.model, _task_budget)
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
                # ① 同模型重试。本轮无任何输出，整轮重试安全。
                # 2026-09-25 起判据从 is_transient_conn_error（**只认网络层文案**）放宽到
                # is_retryable_llm_error（另含 HTTP 5xx / 429 / "无可用渠道" / upstream）。
                # 实证：DMXAPI 会间歇返回 503 "无可用渠道（distributor）"，约 1 分钟后自愈；
                # 旧判据把它当"非瞬时"→ 当场 raise → 整个循环死掉，用户只看到"断了"且无提示。
                # 次数上限 LLM_TURN_RETRY_MAX = 10（用户要求拉长）：重试是零副作用的，
                # 换源才有副作用，所以"能等就等"。
                # ── reasoning_content 回传修复（2026-10-17）──
                # 症状：HTTP 400 LITELLM_ERROR "The `reasoning_content` in the
                # thinking mode must be passed back to the API."，本轮无任何输出
                # 就直接中断（实测 10-17 23:11/23:12 B站任务与 CC Switch 任务同时中招）。
                # 根因：思考模型要求带 tool_calls 的 assistant 消息原样回传本轮
                # reasoning_content，大眼只发了 content/tool_calls → 上游拒收。
                # 实测证据（同一模型同一请求三次）：不回传 400/400/400，回传（哪怕空串）
                # 200/200/200 —— 确定性问题，不是上游抖动，所以"等几秒再重试"没用，
                # 必须先把消息修好再重发。修好后记住这个源，后续轮次组装时直接带上。
                _te = str(turn_error).lower()
                if _turn_retries == 0:
                    _turn_retry_started = time.monotonic()
                _retry_elapsed = time.monotonic() - _turn_retry_started
                _retry_window_open = _retry_elapsed < LLM_TURN_RETRY_WINDOW
                if ("reasoning_content" in _te and "passed back" in _te
                        and _retry_window_open
                        and any(m.get("role") == "assistant" and m.get("tool_calls")
                                and "reasoning_content" not in m for m in msgs)):
                    _patched = 0
                    for _m in msgs:
                        if _m.get("role") == "assistant" and _m.get("tool_calls") \
                                and "reasoning_content" not in _m:
                            _m["reasoning_content"] = _m.get("_thinking") or ""
                            _patched += 1
                    try:
                        _REASONING_ECHO_MODELS.add((llm_config.base_url, llm_config.model))
                    except Exception:
                        pass
                    _turn_retries += 1
                    print(f"[agent] reasoning_content missing -> patched {_patched} "
                          f"assistant msg(s), retry immediately ({_turn_retries}/{LLM_TURN_RETRY_MAX})")
                    full_intermediate = _append_intermediate(
                        full_intermediate,
                        "（上游要求回传思考内容（reasoning_content）→ 已自动补齐并重发）")
                    set_working(tid, "thinking", thinking=full_thinking,
                                intermediate=full_intermediate, response="",
                                turn=turn + 1, remaining=remaining)
                    if _budget_hint_msg and msgs and msgs[-1].get("content") == _budget_hint_msg:
                        msgs.pop()
                    continue
                if (_retry_window_open and _turn_retries < LLM_TURN_RETRY_MAX
                        and is_retryable_llm_error(turn_error)):
                    _turn_retries += 1
                    _wait = min(_turn_retries * 2, 8)
                    print(f"[agent] retryable LLM error (retry {_turn_retries}/{LLM_TURN_RETRY_MAX}), retry in {_wait}s: {turn_error}")
                    # 让用户看得见"在重试"而不是干等（过程泡里出这行）
                    full_intermediate = _append_intermediate(
                        full_intermediate,
                        f"（模型侧 {str(turn_error)[:100]} → {_wait}s 后重试 {_turn_retries}/{LLM_TURN_RETRY_MAX}）")
                    set_working(tid, "thinking", thinking=full_thinking,
                                intermediate=full_intermediate, response="",
                                turn=turn + 1, remaining=remaining)
                    if _budget_hint_msg and msgs and msgs[-1].get("content") == _budget_hint_msg:
                        msgs.pop()  # 重试前移除本轮预算提示，保持 msgs 干净
                    if cancel_evt.wait(_wait):
                        break  # 退避期间允许用户取消
                    continue
                # ② 主模型持续不可用 → 按 fallback_chain 换源，把这一轮跑完（不再杀 loop）。
                #    fallback_chain 早就在配置里，但此前只用于「切 provider 时取 key」，
                #    从没被当运行时回退 —— 这就是"loop 级重连看着有、实际不生效"的另一半。
                #    **受用户开关控制**（设置 → 模型 →「模型不可用时自动切换备用源」）：
                #    换源有副作用（备用源可能不支持工具调用/看图、风格与上下文窗口不同），
                #    不是所有场景都能换，所以关掉后只重试、失败即中断并留下错误说明。
                _auto_fb = _auto_fallback_enabled()
                _fb_cfg, _fb_label = (None, None)
                if _auto_fb:
                    _fb_cfg, _fb_label = _fallback_llm_config(llm_config, _fallback_tried)
                if _fb_cfg is not None:
                    _fallback_tried.add(_fb_label)
                    _fell_back.append(_fb_label)
                    print(f"[agent] main model '{_orig_model}' unusable -> fallback to {_fb_label}")
                    llm_config = _fb_cfg
                    _turn_retries = 0  # 新源重新给满重试额度
                    _turn_retry_started = time.monotonic()  # 新源独立计时 2 分钟
                    if _budget_hint_msg and msgs and msgs[-1].get("content") == _budget_hint_msg:
                        msgs.pop()
                    msgs.append({"role": "user", "content":
                                 f"[系统提示] 主模型 {_orig_model} 持续不可用（{turn_error}），"
                                 f"本轮已临时切换到 {_fb_label} 继续。请继续完成当前任务。"})
                    # 换源也要看得见（否则用户只发现"回答风格变了"却不知为何）
                    full_intermediate = _append_intermediate(
                        full_intermediate,
                        f"（主模型 {_orig_model} 持续不可用 → 本轮已临时切到 {_fb_label} 继续）")
                    set_working(tid, "thinking", thinking=full_thinking,
                                intermediate=full_intermediate, response="",
                                turn=turn + 1, remaining=remaining)
                    continue
                # ③ 所有源都不可用。必须在这里**落一条可见的错误说明**：
                #    do_POST 兜底里的 self._json(500) 因为响应头已流式发出必然失败并被
                #    except 静默吞掉 → 用户只会看到"突然断了、什么都不知道"（本次的真实症状）。
                _fb_note_lines = [
                    "⚠️ 本轮中断：模型侧持续不可用，已停止。",
                    "",
                    f"· 主模型：{_orig_model}",
                    f"· 错误：{turn_error}",
                    f"· 已尝试：当前源实际重试 {_turn_retries} 次（恢复窗口 {min(_retry_elapsed, LLM_TURN_RETRY_WINDOW):.0f}/{LLM_TURN_RETRY_WINDOW} 秒）",
                ]
                if _fell_back:
                    _fb_note_lines.append(
                        f"· 备用源：已试 {'、'.join(_fell_back)}，均不可用")
                elif not _auto_fb:
                    _fb_note_lines.append(
                        "· 备用源：未尝试（「模型不可用时自动切换备用源」已关闭）")
                else:
                    _fb_note_lines.append(
                        "· 备用源：未尝试（fallback_chain 里没有已配好的可用源）")
                _fb_note_lines.append("")
                # 一类错一类话：网关"结算流水在途"不是"模型下线"，重发就能过。
                # 混在"去换模型"里，用户只会白跑一趟设置页（2026-10-20 实测）。
                _te_note = str(turn_error or "").lower()
                if "settlement" in _te_note or "reconciliation" in _te_note:
                    _fb_note_lines.append(
                        "建议：这是网关侧的结算流水在途（不是余额不足，也不是模型下线），"
                        "通常几十秒内自己就消失 —— 直接再发一条消息即可；"
                        "急用可临时到 设置 → 模型 换一个源。")
                else:
                    _fb_note_lines.append(
                        "建议：到 设置 → 模型 里换一个当前可用的模型，再发一条消息让我继续。")
                if not _auto_fb:
                    _fb_note_lines.append(
                        "（也可在同一页开启「模型不可用时自动切换备用源」，让这类故障自动恢复。）")
                _fb_note = "\n".join(_fb_note_lines)
                try:
                    self.db.add_message(tid, "ai", _fb_note, args=_task_stats_args(),
                                        thinking=_turn_thinking, ts=time.time())
                    self.db.update_topic(tid, updated_at=time.time())
                except Exception as _persist_err:
                    print(f"[agent] persist error note failed: {_persist_err}")
                raise Exception(f"LLM API error: {turn_error}")
            else:
                _turn_retries = 0  # 本轮正常产出，重置重试计数
                _turn_retry_started = None  # 下一个模型轮次重新开始恢复窗口
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
            # ── 流式中断续写（A 方案）────────────────────────────────
            # 触发：① 流读到一半抛异常（瞬时网络故障）但本轮已有部分输出
            #          旧逻辑只对“零输出”重试，有输出就直接 raise 干掉整个 loop；
            #       ② 连接被静默掐断，llm 层已把收尾标成 interrupted。
            # 做法：把已产出正文交给 _carry 承接（下一轮 turn_text 以它为起点，
            #      最终答案不会丢前半段），把这段+续写指令塞进上下文，continue 续跑。
            # 上限 2 次，防“断了→续→又断”死循环。
            # 只有“干净收尾”才重置续写计数：带部分输出的中断算不干净，
            # 否则连续中断会把预算每次刷回 0，上限形同虚设（实测 4 次调用不停）。
            if turn_error is None and turn_finish_reason != "interrupted":
                _resume_retries = 0
            _interrupted_turn = (
                turn_finish_reason == "interrupted"
                or (turn_error is not None and is_transient_conn_error(turn_error))
            )
            if (not has_tool_calls and _interrupted_turn
                    and (turn_text.strip() or full_thinking.strip())
                    and _resume_retries < 2 and not cancel_evt.is_set()):
                _resume_retries += 1
                print(f"[agent] stream interrupted ({turn_error or turn_finish_reason}), "
                      f"resume {_resume_retries}/2, kept {len(turn_text)} chars")
                if turn_text.strip():
                    _carry = turn_text  # 前半段已写内容随到下一轮，不丢
                    msgs.append({"role": "assistant", "content": turn_text})
                    _resume_hint = ("[系统提示] 你上一条回复因网络中断被截断，只写到这里。"
                                    "请从中断处接着写：不要重复已写过的内容、不要重新开头、"
                                    "不要解释中断原因，直接继续输出剩余部分。"
                                    "如果上一条其实已经写完，只回复一个句号「。」。")
                else:
                    _resume_hint = ("[系统提示] 你上一轮的思考因网络中断被截断，还没产出正文。"
                                    "请跳过冗长推理，直接给出简洁的最终回答。")
                msgs.append({"role": "user", "content": _resume_hint})
                set_working(tid, "responding", thinking=full_thinking,
                            intermediate=full_intermediate, response=turn_text,
                            turn=turn+1, remaining=remaining)
                time.sleep(_resume_retries * 2)
                continue
            if not has_tool_calls:
                # 最终轮（无 tool_call）：turn_text 就是最终答案
                # ── 防悬空检测：预告但没调用工具 / 生成被截断 → 强制续跑 ──
                # 2026-09-08 修复：思考阶段截断（GLM 深度思考烧满 max_tokens，
                # 正文为空、finish=length）原本被 turn_text.strip() 条件排除，
                # 导致循环直接 break、full_response 为空 → 前端"AI 无回复"、
                # thinking 不落库。现对"纯思考被截断"也强制续跑。
                _thinking_truncated = (
                    turn_finish_reason == "length"
                    and not turn_text.strip()
                    and full_thinking.strip()
                )
                _dangling = (
                    turn_finish_reason == "length"
                    or _is_hanging_announcement(turn_text)
                )
                if _dangling and _dangling_retries < 2 and (turn_text.strip() or _thinking_truncated):
                    _dangling_retries += 1
                    print(f"[agent] dangling announcement (finish={turn_finish_reason}, retry {_dangling_retries}/2), forcing continuation")
                    if turn_text.strip():
                        msgs.append({"role": "assistant", "content": turn_text})
                        msgs.append({"role": "user", "content": "[系统强制] 你上一轮以预告结尾但没有调用任何工具。请立即执行你预告的动作（直接调用工具），不要重复说明、不要重新确认现状。直接做。"})
                    else:
                        msgs.append({"role": "user", "content": "[系统提示] 你上一轮的思考输出被截断（token 预算耗尽），尚未产生任何正文。请跳过冗长推理，直接给出简洁的最终回答，不要再展开长思考。"})
                    continue
                if turn_text.strip():
                    full_response = turn_text  # 最终答案（覆盖式，只此一轮）
                    msgs.append({"role": "assistant", "content": turn_text})
                    break
                # ── 空收尾：既没调工具、也没写一个字 ──────────────────────
                # 旧实现到这里直接 break ⇒ 一条 AI 消息都不落库，用户只看到末尾占位写着
                # "未给出结论"（2026-10-19 实测：64 轮工具后模型回了空，日志
                # "[agent] done after 64 turns"，DB 最后一条是 remember 的工具结果）。
                # 这是"跑了很久却什么都拿不到"里最贵的一种：过程全部算完、结论一个字没有，
                # 而且**连失败都不告诉用户**。先强制再要一次结论（模型常把 update_task_brief /
                # remember 这类状态维护工具当成了收尾），两次都空才落一条看得见的兜底。
                if _empty_final_retries < 2 and not cancel_evt.is_set():
                    _empty_final_retries += 1
                    print(f"[agent] empty final turn (no text, no tool calls), "
                          f"forcing conclusion {_empty_final_retries}/2")
                    msgs.append({"role": "user", "content": (
                        "[系统提示] 你上一轮既没有调用工具，也没有输出任何正文。"
                        "现在请直接用正文向用户汇报：这一轮做了什么、结论是什么、"
                        "还需要用户确认或决定什么。不要调用工具，不要重新开头。")})
                    set_working(tid, "responding", thinking=full_thinking,
                                intermediate=full_intermediate, response="",
                                turn=turn + 1, remaining=remaining)
                    continue
                full_response = (
                    "（模型这一轮没有返回正文，任务停在了上面「最后一步」之后 —— "
                    "过程都在最后一个过程泡里，结论没出来。"
                    "直接回一句「继续」，我接着把结论说完。）")
                break
            # 工具轮：turn_text 是动作叙述（"好嘞去抓官网…"），归 intermediate，不进最终回复
            if turn_text.strip():
                full_intermediate = _append_intermediate(full_intermediate, turn_text)
            # Tool calls were made — execute and continue normally
            assistant_msg = {"role": "assistant", "content": turn_text or None,
                             "_thinking": _turn_thinking}
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
                                            thinking=_turn_thinking, ts=time.time())
            assistant_msg["_db_id"] = ai_msg_id  # for in-memory update after tool result
            # Execute tools and append results
            print(f"[agent] turn {turn+1}: {[tc['tool_name'] for tc in turn_tool_calls]}")
            set_working(tid, "executing", thinking=full_thinking, intermediate=full_intermediate, response="", turn=turn+1, remaining=remaining)

            # Check cancel before tool execution
            if cancel_evt.is_set():
                break

            tool_results = []  # (tc, result_json)
            _sv_pending = []   # self_vision：本轮 see_image(mode=self) 要注入的 (tool_name, 图片路径)
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
                # 2026-10-15 改造②：超限不再原地丢弃，改为落盘成有界引用（r-xxxx）。
                # 落盘失败**必须回退旧行为**——绝不允许因落盘异常导致工具结果丢失。
                _inline_cap = _TOOL_INLINE_CAP.get(tc["tool_name"], _TOOL_INLINE_DEFAULT)
                if len(result_json) > _inline_cap:
                    _ref = _save_bounded_ref(result_json, tid, tc["tool_name"], result)
                    if _ref is not None:
                        result_json = json.dumps(_ref, ensure_ascii=False)
                    else:
                        _keep = max(1000, _inline_cap - 3000)
                        result_json = (result_json[:_keep]
                                       + f"\n…[截断 {len(result_json) - _inline_cap} 字符，工具输出过长已修剪]…\n"
                                       + result_json[-500:])
                msgs.append({"role": "tool", "tool_call_id": tc["tool_call_id"],
                             "content": result_json})
                tool_results.append((tc, result_json))
                loop_detector.record_call(tid, tc["tool_name"], args)
                # self_vision：see_image(mode=self) 的图片，等本轮工具结果全部回填后
                # 再作为一条 user 图片消息注入（OpenAI 协议 tool 消息不收图，
                # 且 tool 消息必须紧跟 assistant.tool_calls，不能插在中间）。
                # 折叠工具经 execute_advanced_tool 返回时会被包成 {"result": {...}}（可能多层），逐层剥开再认标记
                _sv_r = result
                for _ in range(4):
                    if isinstance(_sv_r, dict) and "__self_vision__" not in _sv_r and isinstance(_sv_r.get("result"), dict):
                        _sv_r = _sv_r["result"]
                    else:
                        break
                if isinstance(_sv_r, dict) and _sv_r.get("__self_vision__"):
                    _sv_pending.extend((tc["tool_name"], p) for p in _sv_r.get("images") or [])

            # ── self_vision：注入本轮 see_image 收集的图片（仅存内存，不落库）──
            if _sv_pending and not _vision_capable(llm_config):
                # 当前模型收不了图：不注入（否则云端 400 / 本地 500），明说而不是假装看到
                msgs.append({"role": "user", "content":
                    f"[self_vision] 当前模型 {llm_config.model} 被判定不能收图，"
                    f"{len(_sv_pending)} 张图未注入。请改用 see_image(mode='proxy')。"})
                _sv_pending.clear()
            if _sv_pending:
                try:
                    _sv_content = build_multimodal_content(
                        "[see_image 自视] 以下是你刚请求查看的图片："
                        + "、".join(os.path.basename(p) for _, p in _sv_pending),
                        [p for _, p in _sv_pending])
                    if _sv_content:
                        msgs.append({"role": "user", "content": _sv_content})
                        print(f"[self_vision] 注入 {len(_sv_content) - 1} 张图")
                except Exception as _sv_e:
                    print(f"[self_vision] 注入失败: {_sv_e}")
                _sv_pending.clear()

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
                        # 降级重组会把动态段又塞回 system —— 重新拆一次，否则缓存当轮失效
                        msgs[0], _dyn_state["text"] = _split_system_dynamic(msgs[0])
                        _auto_downgraded = True
                        print(f"[context-wall] 自动降级 system → focus {_focus_level}, 移除: {_removed2}")
                        continue  # 重新估算
                    _focus_level += 2  # 降级无效（无 block 可裁），恢复原等级
                # 第二优先：给 tools 留出预算，只对 messages 截断
                msgs_budget = max(eff_limit - loop_tools_tokens, 5000)
                msgs, trimmed = _trim_context_to_budget(
                    msgs, msgs_budget, store=store, topic_id=tid,
                    on_dropped=lambda _d: _schedule_organize_dropped(tid, _d),
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
            self.db.add_message(tid, "ai", reply, args=_task_stats_args(),
                                thinking=full_thinking, ts=time.time())
            self.db.update_topic(tid, updated_at=time.time())
            finish_working(tid, "done")
            _stats = _task_stats_args()
            self._json(200, {
                "response": reply,
                "thinking": full_thinking,
                "intermediate": full_intermediate,
                "topic_id": tid,
                "status": "cancelled",
                "elapsed": _stats["elapsed"],
                "tokens": _stats["tokens"],
                "tokens_est": _stats["est"],
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
            self.db.add_message(tid, "ai", full_response, args=_task_stats_args(),
                                thinking=full_thinking, ts=time.time())
        self.db.update_topic(tid, updated_at=time.time())
        finish_working(tid, "done")
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
                # 2026-09-04 开关门控：false=关闭自动触发，回落手动 reflect，不影响基础能力
                # 2026-09-05 运行时开关：前端策略页可切换，优先级 前端设置(meta) > 环境变量(config) > 默认 true
                from db import get_db
                if not _runtime_switch(get_db(), "narrative_light_trigger", "NARRATIVE_LIGHT_TRIGGER_ENABLED"):
                    return
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
        _stats = _task_stats_args()
        self._json(200, {
            "response": full_response,
            "thinking": full_thinking,
            "intermediate": full_intermediate,
            "topic_id": tid,
            "elapsed": _stats["elapsed"],
            "tokens": _stats["tokens"],
            "tokens_est": _stats["est"],
            "usage": {"input": total_input_tokens, "output": total_output_tokens},
        })

# ══════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════
def create_server(port=PORT, host="127.0.0.1"):
    db = get_db()
    Handler.topics = TopicManager(db)
    Handler.db = db
    Handler.port = port
    # 轻量叙事触发的节流状态（单线程初始化，避免请求线程竞态建锁）
    Handler._narrative_lock = threading.Lock()
    Handler._last_narrative_ts = 0.0
    # 默认只绑回环：局域网访问需显式 --host 0.0.0.0（暴露时配合守门员/防火墙）
    return http.server.ThreadingHTTPServer((host, port), Handler)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="大眼X Server")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"HTTP port (default: {PORT})")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address (default: 127.0.0.1; use 0.0.0.0 to expose to LAN)")
    ap.add_argument("--label", default="", help="Instance label")
    ap.add_argument("--no-tunnel", action="store_true",
                    help="不启动公网隧道保活巡检（跑临时/调试实例时用，避免与正式实例抢链路）")
    args = ap.parse_args()


    # Start reminder worker
    try:
        import reminder_worker
        reminder_worker.init(ROOT_DIR)
        reminder_worker.start_worker(get_db)
        print(f"[大眼X] Reminder worker started")
    except Exception as e:
        print(f"[大眼X] Reminder worker failed: {e}")
    # 任务写缓冲自愈：上一次运行残留的未合并暂存（崩溃/被掐断）按规则合并
    try:
        from tools.mission_overlay import merge_orphans_on_startup
        rep = merge_orphans_on_startup()
        if rep.get("scanned"):
            print(f"[大眼X] 任务写缓冲自愈：扫到 {rep['scanned']} 份残留，合并 {rep['merged']} 条，孤儿 {len(rep['orphaned'])} 条")
            for d in rep.get("details", []):
                print(f"  [overlay] {d}")
    except Exception as e:
        print(f"[大眼X] 写缓冲自愈失败: {e}")
    # 超限工具输出引用库：启动清理过期引用（TTL 24h）+ 建表
    # 2026-10-15 改造②。失败不影响启动（落盘时会自动重建）
    try:
        import tool_outputs as _tos
        _tos.init_db()
        _purged = _tos.cleanup()
        _st = _tos.stats()
        print(f"[toolrefs] 有界引用库就绪：存量 {_st.get('count', 0)} 条，"
              f"启动清理 {_purged} 条过期，{_st.get('stored_chars', 0)} 字符")
    except Exception as e:
        print(f"[大眼X] 有界引用库初始化失败（超限输出将回退旧截断）: {e}")
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

    # ── workspace 路径治理（幂等启动迁移，2026-09-27）──
    # 标准形态不存 meta 改运行时现算；旧机器失效绝对路径回标准工作区。
    try:
        _ws_norm = db.normalize_workspace_metas()
        if _ws_norm["fixed"]:
            print(f"[workspace] 启动治理: 规范化 {len(_ws_norm['fixed'])} 条, 保留 {_ws_norm['kept']} 条特殊工作区, 跳过 {len(_ws_norm['skipped'])} 条无法自动修复")
    except Exception as _ws_err:
        print(f"[workspace] 启动治理失败（不影响启动）: {_ws_err}")

    # ── Restore accumulated cost from DB ──
    global _total_cost_cny, _total_tokens_global, _total_spent_cny, _last_balance_cny
    try:
        saved_cny = db.get_meta("total_cost_cny")
        if saved_cny:
            _total_cost_cny = float(saved_cny)
        saved_tokens = db.get_meta("total_tokens")
        if saved_tokens:
            _total_tokens_global = int(float(saved_tokens))  # 历史脏数据可能是 '2762013694.0' 这类浮点串
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
    # 公网隧道保活：配置开着就缺谁补谁（守门员 / frpc），见 _tunnel_watchdog_loop
    if not args.no_tunnel:
        threading.Thread(target=_tunnel_watchdog_loop, daemon=True).start()
    else:
        print("[大眼X] --no-tunnel: 公网保活巡检未启动")


    server = create_server(args.port, args.host)
    print(f"[大眼X] [网络]  http://{args.host}:{args.port}")
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
            from tools.task_context import get_current_topic
            db = get_db()
            # ⚠️ 归属必须走任务上下文通道（本线程绑定），不要直接读全局活跃任务：
            # 全局是"用户此刻点开的任务"，与"我这条循环属于谁"是两回事。
            # 2026-09-24 实证：进销存循环把草稿写进了视频任务的 drafts/fe65d696.md。
            tid = get_current_topic() or db.get_active_topic_id()
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
            from tools.task_context import get_current_topic
            db = get_db()
            # ⚠️ 同 write_draft：锚点归属走任务上下文通道，别用全局活跃任务
            # （2026-09-24 实证：进销存循环把锚点写成了视频任务的 anchors/fe65d696.md）
            tid = get_current_topic() or db.get_active_topic_id()
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
        task_errors = []
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
            except Exception as _e:
                # ⚠️ 绝不静默：reload 失败会把模块留在"半新半旧"状态，
                # 调用者却以为成功了 —— 这就是"改了没效果"的经典成因。
                task_errors.append(f"{mod_name}: {type(_e).__name__}: {_e}")
        if task_loaded:
            results.append(f"任务核心: {task_loaded} 个模块已重载")
        if task_errors:
            results.append("⚠️ 任务模块重载失败 %d 个 —— %s" % (len(task_errors), "; ".join(task_errors[:5])))
        # Reload tools/*.py (reloads existing modules, loads new ones)
        tools_dir = os.path.join(BASE_DIR, "tools")
        loaded = 0
        tool_errors = []
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
            except Exception as _e:
                # ⚠️ 同 task 侧：静默吞错 = 假成功。工具文件写错时这里必须报出来。
                tool_errors.append(f"{mod_name}: {type(_e).__name__}: {_e}")
        if loaded:
            results.append(f"工具: {loaded} 个模块已加载/重载，共 {len(get_tool_defs())} 个工具")
        if tool_errors:
            results.append("⚠️ 工具模块重载失败 %d 个 —— %s" % (len(tool_errors), "; ".join(tool_errors[:5])))
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
