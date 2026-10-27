# -*- coding: utf-8 -*-
"""「该任务重要的信息」工具层——跟随任务，不跨任务（2026-10-26 降级）。

原先是「重要事项」：一份全局清单，以 Block 1【对我很重要的事】注入每个任务。
实测 15 条里 13 条是某一次具体任务的技术细节，却每次对话都读一遍，持续误导
工作方向。正式降级为**跟随任务的信息**：
- 只属于当前任务，注入位置挪到【当前任务】之后；别的任务看不到；
- 每条同时镜像进记忆网络（tools/matters_memory.py），语义相关时才被召回；
- 跨任务该记的东西不属于这里 —— 行为准则进领域书，知识/教训进 remember_knowledge。

本文件提供：
- AI 工具：important_matters_add/update/remove/list（名字保留，语义已降级）
- 互动基调：get_tone/set_tone/interaction_tone_update
- 旧接口兼容：get_matters/set_matters（set_matters 已废止，恒 False）
"""
import json

from tools.registry import register_tool
from db import get_db

from tools.mission_overlay import (
    combined_entries, get_matters as _get_matters_impl,
    set_matters as _set_matters_impl,
    add_matter, update_matter, remove_matter,
    _record_lineage, get_active_tid,
)


# ── 旧接口兼容（reflection / server / 老脚本）─────────────

def get_matters(tid=None):
    """字符串列表视图：只有该任务段（全局段已废止）。"""
    return _get_matters_impl(tid)


def set_matters(entries):
    """已废止（原为整表写全局段）。恒返回 False —— 调用方必须据此报错，
    不要以为存上了。"""
    return _set_matters_impl(entries)


def get_tone():
    """读取互动基调。返回 dict 或 None。"""
    try:
        val = get_db().get_meta("interaction_tone")
        if val:
            data = json.loads(val)
            if isinstance(data, dict):
                return data
        return None
    except Exception:
        return None


def set_tone(data):
    """保存互动基调。"""
    try:
        get_db().set_meta("interaction_tone", json.dumps(data, ensure_ascii=False))
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


# ── AI 工具 ──────────────────────────────────────────────

@register_tool(
    name="important_matters_add",
    description="记一条「该任务重要的信息」：只属于当前任务，别的任务看不到，也不会变成跨任务规则。"
                "适合记 —— 这个任务里反复要用到的约定／参数／路径／用户明确偏好／已拍板的决策"
                "（记下来才不用每次都问或每次都猜）。"
                "不适合记 —— 跨任务通用的行为准则（写领域书）、可复用的知识或踩过的坑"
                "（用 remember_knowledge / remember）、任务进度与下一步（用 update_task_brief）。"
                "写完的同时会进记忆网络，任务结束也不会消失，语义相关时才被想起。",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "信息内容，一句话说清，≤200字"},
            "reason": {"type": "string", "description": "为什么记（一句话，审计留痕用）"}
        },
        "required": ["content"]
    }
)
def important_matters_add(content: str, reason: str = ""):
    return add_matter(content, scope="task", reason=reason, by="tool")


@register_tool(
    name="important_matters_update",
    description="改写一条「该任务重要的信息」。用 important_matters_list 拿 id 定位，"
                "也可用注入列表里的序号。只在内容真的变了才改（信息过期比没有更误导）。",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "条目id（list 工具返回的 id 字段）"},
            "index": {"type": "integer", "description": "注入列表序号（1开始），id 缺省时用"},
            "content": {"type": "string", "description": "新的内容"},
            "reason": {"type": "string", "description": "为什么改"}
        },
        "required": ["content"]
    }
)
def important_matters_update(content: str, id: str = None, index: int = None, reason: str = ""):
    return update_matter(index=index, mid=id, content=content, reason=reason, by="tool")


@register_tool(
    name="important_matters_remove",
    description="删除一条「该任务重要的信息」（已失效／已过期／被更好的条目取代）。"
                "用 list 拿 id 定位。留着过期信息比不记更糟 —— 它会一直以“重要”的身份被读到。",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "条目id（list 工具返回的 id 字段）"},
            "index": {"type": "integer", "description": "注入列表序号（1开始），id 缺省时用"},
            "reason": {"type": "string", "description": "为什么删"}
        },
        "required": []
    }
)
def important_matters_remove(id: str = None, index: int = None, reason: str = ""):
    return remove_matter(index=index, mid=id, reason=reason, by="tool")


@register_tool(
    name="important_matters_list",
    description="查看当前任务的重要信息清单（id/序号/内容）。改删前用它拿准 id。"
                "注意：只看得到当前任务这一份，别的任务的信息不在这里。",
    parameters={"type": "object", "properties": {}}
)
def important_matters_list():
    tid = get_active_tid()
    entries = combined_entries(tid)
    out = []
    for i, e in enumerate(entries, 1):
        item = {
            "seq": i,
            "id": e["id"],
            "scope": e.get("scope", "global"),
            "content": e["content"],
        }
        if e.get("_pending"):
            item["pending"] = True
        out.append(item)
    return {"total": len(out), "matters": out}


@register_tool(
    name="interaction_tone_update",
    description="更新「互动基调」：最近对话的氛围+对你的信任度。只在氛围明显变化时更新"
                "（如庆祝成功/刚纠正过错/久别重逢/正在攻坚）。tone 用一句话描述当前氛围；"
                "trust 填 high（可直说少客气）/mid（正常）/low（谨慎多解释少玩笑）。",
    parameters={
        "type": "object",
        "properties": {
            "tone": {
                "type": "string",
                "description": "一句话氛围描述，如「刚完成大验证，用户心情不错」"
            },
            "trust": {
                "type": "string",
                "description": "high/mid/low"
            }
        },
        "required": ["tone", "trust"]
    }
)
def interaction_tone_update(tone: str, trust: str = "mid"):
    if not tone:
        return {"error": "tone 不能为空"}
    if trust not in ("high", "mid", "low"):
        trust = "mid"
    import time
    data = {"tone": tone, "trust": trust, "updated": time.strftime("%Y-%m-%d %H:%M")}
    if not set_tone(data):
        return {"error": "写入 DB 失败，互动基调未保存。"}
    _record_lineage("tone", 0, None, f"{tone} (trust={trust})")
    return {"success": True, "tone": tone, "trust": trust, "updated": data["updated"]}
