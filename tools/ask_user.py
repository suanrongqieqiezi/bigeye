"""ask_user — 阻塞式提问工具。

AI 调用后 agent 循环暂停，等待用户在前端问题卡片上回答。
超时（默认5分钟）自动降级为「自行判断」，后台任务不会永远挂起。
放在折叠分组 interaction，不占常驻 schema。
"""
import threading
import uuid

from .registry import register_tool

# question_id -> {question_id, topic_id, question, options, event, answer}
_PENDING = {}
_LOCK = threading.Lock()

DEFAULT_TIMEOUT = 300  # 5 分钟


def get_pending_question(tid):
    """供 /api/working 轮询：返回该话题当前等待中的问题，无则 None。"""
    with _LOCK:
        for q in _PENDING.values():
            if q.get("topic_id") == tid and not q["event"].is_set():
                return {
                    "question_id": q["question_id"],
                    "question": q["question"],
                    "options": q["options"],
                }
    return None


def submit_answer(question_id, answer):
    """供 /api/ask_user/answer：用户提交回答，唤醒阻塞的工具调用。"""
    with _LOCK:
        q = _PENDING.get(question_id)
    if not q:
        return False
    q["answer"] = answer
    q["event"].set()
    return True


@register_tool(
    name="ask_user",
    description=(
        "向用户提问并暂停等待回答（阻塞式）。"
        "用于遇到必须用户拍板的决策点：方向选择、重要操作确认、关键信息缺失。"
        "用户5分钟内不回答会返回超时提示，届时自行判断继续。"
        "能自己决定的小事不要问。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要问用户的问题",
            },
            "options": {
                "type": "array",
                "description": "可选选项（用户也可自由输入）",
                "items": {"type": "string"},
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "等待超时秒数，默认300",
                "default": 300,
            },
        },
        "required": ["question"],
    },
)
def ask_user(question: str, options: list = None, timeout_seconds: int = DEFAULT_TIMEOUT):
    from db import get_db
    from server import set_working

    tid = get_db().get_active_topic_id() or ""
    qid = uuid.uuid4().hex[:8]
    entry = {
        "question_id": qid,
        "topic_id": tid,
        "question": question,
        "options": options or [],
        "event": threading.Event(),
        "answer": None,
    }
    with _LOCK:
        _PENDING[qid] = entry

    # 状态置为等待回答，前端轮询时弹问题卡片
    try:
        set_working(tid, "waiting_answer", thinking=f"等待你确认：{question}")
    except Exception:
        pass

    # 轮询等待：回答事件 或 用户取消任务，每秒检查一次
    from server import _cancel_events
    cancel_evt = _cancel_events.get(tid)
    waited = False
    cancelled = False
    deadline = timeout_seconds or DEFAULT_TIMEOUT
    for _ in range(int(deadline)):
        if entry["event"].wait(timeout=1.0):
            waited = True
            break
        if cancel_evt is not None and cancel_evt.is_set():
            cancelled = True
            break

    with _LOCK:
        _PENDING.pop(qid, None)

    if cancelled:
        return {
            "user_answer": None,
            "note": "用户取消了任务，停止当前工作。",
        }
    if waited and entry["answer"] is not None:
        return {
            "user_answer": entry["answer"],
            "note": "以上是用户的回答，据此继续任务。",
        }
    return {
        "user_answer": None,
        "note": f"用户{deadline}秒内未回答。请自行做最合理判断继续，或结束本轮等用户下条消息。",
    }
