# -*- coding: utf-8 -*-
"""task_context — 任务运行时上下文通道。

【为什么需要它】
工具层过去用 `db.get_active_topic_id()` 反推"当前任务"。那是全局单例指针，
单任务时代没问题；多任务并行/切换窗口时，全局指针只指向一个任务，
其他任务的 agent 循环调用工具就会读错、写错归属。

已确认事故：
  - ask_user 把提问卡片弹进了另一个任务的窗口（tools/ask_user.py L76）
  - 工具层 30+ 处 get_active_topic_id() 都是潜在串台点

【方案】
agent 循环每轮在自己的线程里显式 `set_current_topic(tid)`，工具层优先读它。
没设置时回落到全局活跃话题 —— 保持脚本、定时任务、后台线程等无请求上下文
场景下的旧行为不变。

【线程模型】
server 用 ThreadingHTTPServer，一个请求一个线程，agent 循环与工具调用同线程
（execute_tool 内联调用），所以 threading.local 就够，无需 contextvars。
与 tools/bash.py 的 set_runtime_context 是同一套模式。

【边界】
- 只解决"读对归属"，不解决业务层的并发写冲突。
- keep-alive 下线程会被复用，所以任务结束必须清理（clear_working 里已挂）。
"""
import threading

_local = threading.local()


def set_current_topic(tid):
    """agent 循环入口调用：把本线程绑定到某个任务。返回是否设置成功。"""
    tid = (tid or "").strip() if isinstance(tid, str) else tid
    _local.topic_id = tid or None
    return bool(_local.topic_id)


def get_current_topic(default=None):
    """工具层读当前任务归属。

    优先级：本线程显式绑定 > 全局活跃话题 > default。
    全局兜底是为了兼容无请求上下文的调用方（脚本/定时任务/后台线程）。
    """
    tid = getattr(_local, "topic_id", None)
    if tid:
        return tid
    try:
        from db import get_db
        tid = get_db().get_active_topic_id()
        if tid:
            return tid
    except Exception:
        pass
    return default


def clear_current_topic():
    """清空本线程绑定（任务生命周期结束时调用）。"""
    _local.topic_id = None


def clear_if_matches(tid):
    """仅当本线程恰好绑定在 tid 上时才清，避免误清其它任务。"""
    if getattr(_local, "topic_id", None) == tid:
        clear_current_topic()


def has_explicit_context():
    """本线程是否显式绑定了任务（供排查/测试用）。"""
    return bool(getattr(_local, "topic_id", None))
