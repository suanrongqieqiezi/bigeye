#!/usr/bin/env python3
"""Crystal session context — 透明桥接 read_file → remember。

哲学：AI 无感。
- read_file 读已阅文件时，把当前文件路径 + 切片 id 列表写入线程上下文
- remember 写入记忆时，自动从上下文取切片 id 作为 raw_source_id
- AI 完全不知道有"晶体会话"这种东西存在

用法：
    # read_file 内部
    set_session(path="src/auth.py", slice_ids=["fc_abc", "fc_def"])

    # remember 内部
    session = get_session()
    if session and "raw_source" not in args:
        args["raw_source"] = session["latest_slice_id"]
"""
import threading

_session = threading.local()


def set_session(path: str, slice_ids: list, source_type: str = ""):
    """设置当前线程的晶体会话上下文。

    Args:
        path: 文件路径
        slice_ids: 本次 read_file 涉及的切片 id 列表
        source_type: 来源类型（knowledge_base / ai_downloads / url）
    """
    # 累积：一次对话可能读多个文件，保留全部历史
    history = getattr(_session, "history", [])
    latest_slice_id = slice_ids[-1] if slice_ids else None
    history.append({
        "path": path,
        "slice_ids": slice_ids,
        "latest_slice_id": latest_slice_id,
        "source_type": source_type,
    })
    _session.history = history
    _session.current_path = path
    _session.current_slice_id = latest_slice_id


def get_session() -> dict:
    """获取当前线程的晶体会话上下文。

    Returns:
        {"path": str, "slice_ids": [str], "latest_slice_id": str,
         "history": [...], "current_path": str, "current_slice_id": str}
        或 None（无会话）
    """
    history = getattr(_session, "history", [])
    if not history:
        return None
    latest = history[-1]
    return {
        "path": latest["path"],
        "slice_ids": latest["slice_ids"],
        "latest_slice_id": latest["latest_slice_id"],
        "source_type": latest["source_type"],
        "history": history,
        "current_path": getattr(_session, "current_path", None),
        "current_slice_id": getattr(_session, "current_slice_id", None),
    }


def get_current_slice_id() -> str:
    """快捷获取最近一次 read_file 的切片 id（remember 自动建桥用）。"""
    return getattr(_session, "current_slice_id", None)


def get_current_path() -> str:
    """快捷获取最近一次 read_file 的文件路径。"""
    return getattr(_session, "current_path", None)


def clear_session():
    """清空当前线程的会话上下文。

    由 server.py 在对话回合结束时调用。
    """
    _session.history = []
    _session.current_path = None
    _session.current_slice_id = None
