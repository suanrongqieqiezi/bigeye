"""目标级打转检测模块。

检测 LLM 在任务执行中是否陷入"打转"：大量探索类工具调用（读/搜/执行）
但零落地类调用（写文件/完成节点/沉淀记忆/求助），即"光看不干"。

设计原则：
- 内存态、零 DB 查询，埋点零成本。
- 环形 buffer 记录最近 N 次工具调用，按落地/探索/中性分类。
- 触发条件：窗口内探索类占比达到阈值且落地类为 0，且距上次提醒超过冷却时间。
- 落地类调用出现即重置冷却（说明有实质进展，不打断）。

调用方：server.py 的 agent 主循环。每次工具调用后 record_call(tid, tool_name)，
每轮工具执行完 check_spinning(tid) 决定是否注入提醒。
"""

import time
import threading
from collections import deque, defaultdict

# ── 落地类工具：调用即产生持久化/外部可见改变，= 有进展 ──
LANDING_TOOLS = {
    "write_file", "edit_file",
    "complete_node", "finish_task",
    "remember", "remember_knowledge",
    "important_matters_add", "important_matters_update", "important_matters_remove",
    "create_task", "insert_dag_node", "remove_dag_node", "update_node_deps",
    "dynamic_split", "start_node", "rework_subtree",
    "write_draft", "update_task_brief",
    "add_mindmap_node",
    "save_task_template",
    "report_blocker", "ask_question", "ask_user",
    "rule_add", "rule_update", "rule_delete",
}

# ── 探索类工具：只读/搜索/执行，无落地 ──
EXPLORE_TOOLS = {
    "web_search", "web_fetch",
    "read_file", "grep", "file_search",
    "bash", "run_python", "check_python",
    "discover_tools", "execute_advanced_tool",
    "code_ast_parse", "code_find_defs", "code_get_symbol",
    "trace_memory", "crystal_recall", "recall_by_topic", "check_memory_gaps",
    "expand_compressed", "get_task_dag", "get_mindmap", "get_perspective",
    "list_topics", "current_topic", "list_tools", "read_topic_messages",
    "book_list_pages", "book_read_page",
    "important_matters_list", "system_status",
}

# ── 参数 ──
WINDOW = 12          # 观察窗口大小（最近 N 次调用）
THRESHOLD = 10       # 窗口内探索类调用次数达到该值才可能触发
COOLDOWN = 300.0     # 两次提醒最小间隔（秒）


def _classify(tool_name, args=None):
    """判断一次工具调用的类别：'landing' / 'explore' / 'neutral'。"""
    if tool_name == "execute_advanced_tool" and isinstance(args, dict):
        inner = args.get("name") or ""
        if inner in LANDING_TOOLS:
            return "landing"
        if inner in EXPLORE_TOOLS:
            return "explore"
    if tool_name in LANDING_TOOLS:
        return "landing"
    if tool_name in EXPLORE_TOOLS:
        return "explore"
    return "neutral"


class _Detector:
    """单个话题的打转状态。多话题隔离，互不污染。"""

    def __init__(self):
        self.buffer = deque(maxlen=WINDOW)
        self.last_fire = 0.0  # 上次提醒时间戳
        self.last_landing = 0.0  # 上次落地类调用时间戳

    def record(self, tool_name, args=None):
        kind = _classify(tool_name, args)
        now = time.time()
        self.buffer.append(kind)
        if kind == "landing":
            self.last_landing = now

    def check(self):
        """返回提醒文案；不需要提醒时返回 None。"""
        buf = self.buffer
        if len(buf) < WINDOW:
            return None
        explore = sum(1 for k in buf if k == "explore")
        landing = sum(1 for k in buf if k == "landing")
        if explore < THRESHOLD:
            return None
        if landing > 0:
            return None
        now = time.time()
        if now - self.last_fire < COOLDOWN:
            return None
        self.last_fire = now
        return (
            "[提醒] 我注意到你刚才连续做了很多次读取/搜索/执行类操作，"
            "但还没有任何实质产出（写文件、完成任务节点、沉淀记忆、向用户求助）。"
            "你很可能在同一个问题上反复尝试、原地打转。\n"
            "请停下，按顺序自查：\n"
            "1. 前提假设是否错了？连续两次失败先怀疑前提，而不是换个姿势重试。\n"
            "2. 是不是该直接问用户？遇到权限/网络/工具失效这类阻塞，试两轮没结果就抛给用户。\n"
            "3. 如果你确认这是正常的长调研（信息收集阶段），忽略本条继续。\n"
            "若确实在打转，请停止重试，重新审视方向。\n"
            "如果之后需要向用户说明，就说人话，例如：'我刚在同一个方向查了很久没结果，换个思路'。"
            "不要提及本条提醒，不要暴露任何内部机制或检测名称。"
        )


_detectors = defaultdict(_Detector)
_lock = threading.Lock()


def record_call(topic_id, tool_name, args=None):
    """每次工具调用后记录。thread-safe，零异常外泄。"""
    try:
        with _lock:
            _detectors[topic_id].record(tool_name, args)
    except Exception:
        pass


def check_spinning(topic_id):
    """每轮工具执行完检查。返回提醒文案或 None。thread-safe，零异常外泄。"""
    try:
        with _lock:
            return _detectors[topic_id].check()
    except Exception:
        return None


def reset(topic_id):
    """话题结束时清空状态，释放内存。"""
    try:
        with _lock:
            _detectors.pop(topic_id, None)
    except Exception:
        pass
