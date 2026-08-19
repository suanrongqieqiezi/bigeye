#!/usr/bin/env python3
"""注意力等级工具 — AI 自己控制上下文密度。

AI 通过 set_focus_level 收缩/恢复上下文：
- 默认全量（Level 15）
- 专注模式（Level 10）：移除故事/记忆/工具书/草稿
- 极限模式（Level 13）：只留核心框架（身份+任务+锚点+DAG+工具）
- 支持自定义 block 列表覆盖默认等级
- 支持 N 轮后自动恢复，或手动恢复
- 关闭前必须写备忘（为什么关、什么时候开、当前进度）
"""
import json
from .registry import register_tool


@register_tool(
    name="set_focus_level",
    description=(
        "调整注意力等级（1-15）。等级越低看到的上下文越少，适合深入钻研时减少干扰。"
        "15=全量（默认），10=专注模式（移除故事/记忆/工具书/草稿），"
        "13=极限模式（只留身份+任务+锚点+DAG+工具）。"
        "auto_rounds: N 轮后自动恢复到 15（可选，不设则需手动恢复）。"
        "custom_blocks: 可选，AI 自己指定要保留的 block 标题列表（覆盖默认等级）。"
        "【重要】关闭前必须写 memo：1)为什么关 2)什么时候开回来 3)当前进度。"
        "解决难点后记得 set_focus_level(15) 恢复全量上下文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "level": {
                "type": "integer",
                "description": "注意力等级 1-15（15=全量，10=专注，13=极限）",
                "minimum": 1,
                "maximum": 15,
            },
            "auto_rounds": {
                "type": "integer",
                "description": "N 轮后自动恢复到 15（可选，不设则需手动恢复）",
                "minimum": 1,
            },
            "memo": {
                "type": "string",
                "description": "备忘（必填），写清楚：1)为什么关 2)什么时候开回来 3)当前进度",
            },
            "custom_blocks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，AI 自己指定要保留的 block 标题列表（如['【对我很重要的事】','【我现在在做什么】']），覆盖默认等级",
            },
        },
        "required": ["level", "memo"],
    },
)
def set_focus_level(level: int, auto_rounds: int = None, memo: str = "", custom_blocks: list = None):
    from db import get_db
    db = get_db()
    tid = db.get_active_topic_id()
    if not tid:
        return "没有活跃任务，无法设置注意力等级。"

    level = max(1, min(15, int(level)))

    # 存到 DB
    db.set_topic_meta(tid, "focus_level", str(level))
    db.set_topic_meta(tid, "focus_memo", memo)

    if custom_blocks:
        db.set_topic_meta(tid, "focus_custom_blocks", json.dumps(custom_blocks, ensure_ascii=False))
    else:
        db.set_topic_meta(tid, "focus_custom_blocks", "[]")

    if auto_rounds and auto_rounds > 0:
        # 用当前消息数作为基准轮次
        current_msgs = db.message_count(tid)
        restore_at = current_msgs + int(auto_rounds)
        db.set_topic_meta(tid, "focus_restore_at", str(restore_at))
        restore_hint = f"，{auto_rounds} 轮后自动恢复"
    else:
        db.set_topic_meta(tid, "focus_restore_at", "-1")
        restore_hint = "，需手动恢复"

    removed_desc = ""
    if not custom_blocks and level < 15:
        from server import _BLOCK_LEVELS
        removed = [t for t, lv in _BLOCK_LEVELS.items() if lv < level]
        if removed:
            removed_desc = f"\n已移除：{', '.join(removed)}"

    return (
        f"注意力等级已设为 {level}/15{restore_hint}。\n"
        f"备忘：{memo}{removed_desc}"
    )
