# -*- coding: utf-8 -*-
"""重要事项工具层——多任务分段 + 写缓冲。

实现迁移到 tools/mission_overlay.py（数据层单文件），本文件提供：
- AI 工具：important_matters_add/update/remove/list
- 互动基调：get_tone/set_tone/interaction_tone_update
- 旧接口兼容：get_matters/set_matters（reflection 等调用方不变）

注入（提示词里的列表）由 server 侧 get_matters(active_tid) 生成：全局段+任务段+缓冲叠加。
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
    """字符串列表视图：全局段+任务段+挂起缓冲叠加。"""
    return _get_matters_impl(tid)


def set_matters(entries):
    """整表写全局段（兼容旧调用）。返回 True/False。"""
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
    description="新增一条重要事项（200字内）。默认写入当前任务段，只对本任务可见，任务结束自动清空；"
                "确属跨任务长期有效才用 scope=global 写全局段（全局段有20条上限，慎加）。",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "事项内容，一句话，≤200字"},
            "scope": {"type": "string", "description": "global=全局长期有效；task=仅当前任务（默认）"},
            "reason": {"type": "string", "description": "为什么重要（一句话，挂起缓冲合并时留痕用）"}
        },
        "required": ["content"]
    }
)
def important_matters_add(content: str, scope: str = None, reason: str = ""):
    return add_matter(content, scope=scope, reason=reason, by="tool")


@register_tool(
    name="important_matters_update",
    description="修改一条重要事项。优先用 important_matters_list 拿到的 id 定位；也可用注入列表里的序号。",
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
    description="删除一条重要事项（已过期/已失效/被更好的条目取代）。用 list 拿 id 定位。",
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
    description="查看全部重要事项的结构化清单（id/序号/归属/内容）。改删前用它拿准 id。",
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
