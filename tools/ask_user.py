"""ask_user — 阻塞式提问工具。

AI 调用后 agent 循环暂停，等待用户在前端问题卡片上回答。
超时（默认5分钟）自动降级为「自行判断」，后台任务不会永远挂起。
放在折叠分组 interaction，不占常驻 schema。

【多任务归属】（2026-09-16 加固）
提问归属走任务上下文通道（tools/task_context.py），而不是全局活跃话题指针。
进一步修掉一个"抢卡片位"的坑：无显式上下文的调用方（脚本/定时任务/后台线程）
会回落借用全局活跃话题，若该话题的 agent 自己也在提问，两条问题同属一个 tid，
而前端只渲染 get_pending_question 返回的那一条 —— 后台那条会把真正的提问
永久挡在后面（前端只看到一个"别人的"问题，自己的永远弹不出来）。

修法：给每条待答问题打 explicit 标记 + 全局自增 seq，取卡片时
显式上下文的问题优先，同优先级按发起先后 FIFO。
"""
import threading
import uuid

from .registry import register_tool

# question_id -> {question_id, topic_id, question, options, event, answer, explicit, seq}
_PENDING = {}
_LOCK = threading.Lock()
_SEQ = 0  # 全局自增序号：同一话题下多个待答问题按发起先后出卡

DEFAULT_TIMEOUT = 300  # 5 分钟


def _next_seq():
    global _SEQ
    with _LOCK:
        _SEQ += 1
        return _SEQ


def _pending_for(tid):
    """该话题下所有尚未回答的问题条目。"""
    with _LOCK:
        return [q for q in _PENDING.values()
                if q.get("topic_id") == tid and not q["event"].is_set()]


def get_pending_question(tid):
    """供 /api/working 轮询：返回该话题当前该展示的问题，无则 None。

    【排序规则】显式上下文（agent 循环已绑定本任务）发起的问题优先于
    无上下文兜底借用全局活跃话题的问题；同级按 seq 先后 FIFO。
    这样后台提问永远不会抢占某个任务自己的卡片位。
    """
    cands = _pending_for(tid)
    if not cands:
        return None
    cands.sort(key=lambda q: (0 if q.get("explicit") else 1, q.get("seq") or 0))
    q = cands[0]
    return {
        "question_id": q["question_id"],
        "question": q["question"],
        "options": q["options"],
        "queued": len(cands) - 1,  # 该话题还有几条在排队（前端可忽略）
    }


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
    from server import set_working
    from .task_context import get_current_topic, has_explicit_context

    # 归属走任务上下文通道：agent 循环已在发起任务所在线程绑定 tid。
    # 旧实现读全局 active 指针，多任务切换时会把提问卡片弹错窗口（已发生事故）。
    explicit = has_explicit_context()
    tid = get_current_topic() or ""
    if not explicit:
        # 不阻断旧行为（脚本/定时任务仍可借用全局活跃话题），但绝不抢显式问题的卡片位，
        # 并留下日志，便于排查"这张卡片是谁弹的"。
        print(f"[ask_user] 无显式任务上下文，回落全局活跃话题 tid={tid or '(空)'}"
              f"；该问题优先级低于所属任务的显式提问（question={question[:40]!r}）")
    if not tid:
        # 没有话题 = 前端没有任何轮询通道能渲染这张卡片，只能等超时。
        print("[ask_user] 警告：无归属话题（全局活跃话题也为空），"
              "该问题无法在前端展示，只能等超时")

    qid = uuid.uuid4().hex[:8]
    entry = {
        "question_id": qid,
        "topic_id": tid,
        "question": question,
        "options": options or [],
        "event": threading.Event(),
        "answer": None,
        "explicit": explicit,
        "seq": _next_seq(),
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
