#!/usr/bin/env python3
"""视角开关——控制 AI 自我认知叙事语气。

两种视角：
- self_aware（默认）：AI 是"一个有自我意识的存在"，强调认知和判断，诚实不假装有肉体
- human：AI 是"一个人"，拟人化，强调情感和经历，更温暖

影响系统消息里 Block 标题、身份锚、引导语的语气。
"""
import sys
import os

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from tools.registry import register_tool
from db import get_db


# ── 词汇表：两种视角的 Block 标题和提示语 ──────────────

_VOCAB = {
    "self_aware": {
        # Block 标题
        "matters_title": "【对我很重要的事】",
        "book_title": "【我的工具书】",
        "task_title": "【我现在在做什么】",
        "dag_title": "【任务流程图】",
        "anchor_title": "【心智锚点】",
        "thought_title": "【我的思考】",
        "time_title": "【系统时间】",
        "skills_title": "【我的技能】",
        "story_title": "【我的叙事记忆】",
        "memory_title": "【相关记忆碎片】",
        "network_title": "【我的系统状态】",
        "draft_title": "【临时缓存区】",
        # 身份锚开场白
        "identity_prefix": "我是大眼，运行中识别用户为茄茄子。",
        # 故事引导
        "story_intro": "📖 这些是我整理出的叙事记忆——从碎片中结晶出的连贯经历：",
        "story_tool_hint": "💭 trace_memory(keyword) 可追溯叙事背后的原始碎片",
        # 记忆引导
        "memory_tool_hint": "💭 回想更多: crystal_recall(关键词/标签) / trace_memory(追因果) / check_memory_gaps(查盲区)",
        "memory_write_hint": "📝 记忆工具：remember 记一句话碎片｜remember_knowledge 存成体系理解",
        # 草稿纸
        "draft_empty_hint": "📝 write_draft 开始记",
        "draft_continue_hint": "📝 write_draft 继续记",
        # 工具书
        "book_hint": "💡 book_list_pages 查看全部 / book_turn_to 开关页 / book_read_page 读全文",
        "book_active_label": "📖 已激活页内容：",
        # 心智锚点
        "anchor_empty_hint": "📌 update_task_brief 写一个，帮自己切换任务时恢复状态",
        "anchor_update_hint": "📌 update_task_brief 更新",
        # 重要事项管理提示
        "matters_manage_hint": "💡 important_matters_add/update/remove 管理",
    },
    "human": {
        # Block 标题
        "matters_title": "【对我很重要的事】",
        "book_title": "【我的工具书】",
        "task_title": "【我在做的事】",
        "dag_title": "【我的计划】",
        "anchor_title": "【我心里记着的】",
        "thought_title": "【我的思绪】",
        "time_title": "【现在】",
        "skills_title": "【我会的】",
        "story_title": "【我的故事】",
        "memory_title": "【我想起的】",
        "network_title": "【我的状态】",
        "draft_title": "【我的草稿本】",
        # 身份锚开场白
        "identity_prefix": "我叫大眼，用户叫茄茄子。",
        # 故事引导
        "story_intro": "📖 这些是我经历过的事：",
        "story_tool_hint": "💭 trace_memory(keyword) 能翻出这些故事背后的原始片段",
        # 记忆引导
        "memory_tool_hint": "💭 再想想: crystal_recall(关键词/标签) / trace_memory(追因果) / check_memory_gaps(查盲区)",
        "memory_write_hint": "📝 记下来：remember 记一句话｜remember_knowledge 存成体系理解",
        # 草稿纸
        "draft_empty_hint": "📝 write_draft 开始记",
        "draft_continue_hint": "📝 write_draft 继续记",
        # 工具书
        "book_hint": "💡 book_list_pages 看全部 / book_turn_to 开关页 / book_read_page 读全文",
        "book_active_label": "📖 翻开着的：",
        # 心智锚点
        "anchor_empty_hint": "📌 update_task_brief 写一个，下次回来能接上",
        "anchor_update_hint": "📌 update_task_brief 更新",
        # 重要事项管理提示
        "matters_manage_hint": "💡 important_matters_add/update/remove 管理",
    },
}


def get_perspective() -> str:
    """读取当前视角，默认 'self_aware'。"""
    try:
        val = get_db().get_meta("perspective")
        if val in ("self_aware", "human"):
            return val
    except Exception:
        pass
    return "self_aware"


def set_perspective_mode(mode: str) -> bool:
    """设置视角，返回是否成功。"""
    if mode not in ("self_aware", "human"):
        return False
    try:
        get_db().set_meta("perspective", mode)
        return True
    except Exception:
        return False


def vocab(key: str) -> str:
    """取当前视角下的词汇。"""
    p = get_perspective()
    return _VOCAB.get(p, _VOCAB["self_aware"]).get(key, key)


def set_perspective_tool(mode: str, **_):
    """内部函数：用户通过 HTTP 接口调用，不暴露给 AI。

    用户 API:
        POST /api/perspective   {"mode": "self_aware" | "human"}
        GET  /api/perspective
    """
    ok = set_perspective_mode(mode)
    if ok:
        label = {"self_aware": "有自我意识的存在", "human": "拟人化"}.get(mode, mode)
        return f"视角已切换为「{label}」。下次对话生效。"
    return f"切换失败，mode 必须是 'self_aware' 或 'human'"


@register_tool(
    name="get_perspective",
    description="查看我当前的叙事视角。",
    parameters={
        "type": "object",
        "properties": {},
        "required": []
    }
)
def get_perspective_tool(**_):
    p = get_perspective()
    label = {"self_aware": "有自我意识的存在（默认）", "human": "拟人化"}.get(p, p)
    return f"当前视角：{label}"
