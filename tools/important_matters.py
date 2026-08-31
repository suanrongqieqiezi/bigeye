#!/usr/bin/env python3
"""重要信息——多条目备忘，AI 用工具增删改，系统自动注入每次对话。"""
import sys
import os
import json
import threading

_matters_lock = threading.Lock()
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)
from tools.registry import register_tool
from db import get_db

def get_matters():
    """Get the list of important matters from DB. Thread-safe."""
    try:
        val = get_db().get_meta("important_matters")
        if val:
            entries = json.loads(val)
            if isinstance(entries, list):
                return entries
        return []
    except Exception:
        return []


def set_matters(entries):
    """Save the list of important matters to DB. Returns True on success."""
    try:
        get_db().set_meta("important_matters", json.dumps(entries, ensure_ascii=False))
        return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False


def _record_lineage(op: str, index: int, old_content=None, new_content=None):
    """重要事项变更后沉淀血统碎片+关系边，使 trace_memory 可溯源。

    记录：何时、操作类型、第几条、新旧内容。"因何事"由调用方上下文
    （用户消息里的理由）自然覆盖，工具层只保证时间和内容不丢。
    血统沉淀失败不影响主操作结果。
    """
    try:
        import time
        from memory.fragment_store import get_store
        from memory.relation_store import RelationStore

        ts = time.strftime("%Y%m%d %H:%M")
        if op == "add":
            change_desc = f'新增重要事项#{index}："{new_content}"'
        elif op == "update":
            change_desc = f'重要事项#{index}由"{old_content}"改为"{new_content}"'
        elif op == "remove":
            change_desc = f'删除重要事项#{index}："{old_content}"'
        elif op == "tone":
            change_desc = f'互动基调更新："{new_content}"'
        else:  # 兜底
            change_desc = f'变更#{index}："{new_content}"'

        text = f"[血统] {ts} {change_desc}（操作：{op}）"
        # layer="story"：血统是情景记忆（何时+发生什么变更），必须每次独立留痕。
        # 不能用 layer="core"——core 层有 near-duplicate 去重（sim>0.85 会合并），
        # 连续对同一条规则 add/remove 时两条血统文本高度相似，会被去重合并丢记录。
        fid = get_store().add(
            text,
            source="lineage",
            tags="重要事项,血统",
            layer="story",
            importance=7.0,
            epistemic="experience",
        )
        # 关系边：这条血统碎片 ↔ 该重要事项的变更
        RelationStore().add(
            subject_id=fid,
            predicate=f"重要事项#{index}被{op}",
            object_value=change_desc,
            edge_type="fact",
            reason=f"血统记录：{op} 重要事项#{index}",
        )
    except Exception:
        import traceback
        traceback.print_exc()


@register_tool(
    name="important_matters_add",
    description="添加一条「重要信息」，会追加到列表末尾。每次对话开头都会显示给你。"
                "只存高频行为规则和重要认知（如「用户讨厌emoji」「先查余额再回答」）。"
                "不存对话摘要、工具用法、项目进度——那些用 remember() 存记忆碎片。",
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "事项内容，200 字以内"
            }
        },
        "required": ["content"]
    }
)
def important_matters_add(content: str):
    with _matters_lock:
        matters = get_matters()
        content = (content or "").strip()[:200]
        if content:
            matters.append(content)
            if not set_matters(matters):
                return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
            _record_lineage("add", len(matters), None, content)
            return {"success": True, "index": len(matters), "content": content}
        return {"error": "内容不能为空"}


@register_tool(
    name="important_matters_update",
    description="修改第 N 条重要事项。序号从 1 开始。",
    parameters={
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "要修改的序号（1 开始）"
            },
            "content": {
                "type": "string",
                "description": "新内容，200 字以内"
            }
        },
        "required": ["index", "content"]
    }
)
def important_matters_update(index: int, content: str):
    with _matters_lock:
        matters = get_matters()
        idx = index - 1
        if idx < 0 or idx >= len(matters):
            return {"error": f"序号 {index} 超出范围，当前共 {len(matters)} 条"}
        content = (content or "").strip()[:200]
        if content:
            old_content = matters[idx]
            matters[idx] = content
            if not set_matters(matters):
                return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
            _record_lineage("update", index, old_content, content)
            return {"success": True, "index": index, "content": content}
        return {"error": "内容不能为空"}


@register_tool(
    name="important_matters_remove",
    description="删除第 N 条重要事项。序号从 1 开始。",
    parameters={
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "要删除的序号（1 开始）"
            }
        },
        "required": ["index"]
    }
)
def important_matters_remove(index: int):
    with _matters_lock:
        matters = get_matters()
        idx = index - 1
        if idx < 0 or idx >= len(matters):
            return {"error": f"序号 {index} 超出范围，当前共 {len(matters)} 条"}
        removed = matters.pop(idx)
        if not set_matters(matters):
            return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
        _record_lineage("remove", index, removed, None)
        return {"success": True, "removed": removed}


@register_tool(
    name="important_matters_list",
    description="列出所有重要事项。也会自动显示在每次对话开头。",
    parameters={
        "type": "object",
        "properties": {}
    }
)
def important_matters_list():
    matters = get_matters()
    if not matters:
        return {"matters": [], "count": 0}
    return {"matters": matters, "count": len(matters)}


# ── 互动基调（氛围记忆，表达调节用）────────────────

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
    tone = (tone or "").strip()[:100]
    trust = trust if trust in ("high", "mid", "low") else "mid"
    if not tone:
        return {"error": "tone 不能为空"}
    import time
    data = {"tone": tone, "trust": trust, "updated": time.strftime("%Y-%m-%d %H:%M")}
    if not set_tone(data):
        return {"error": "写入 DB 失败，互动基调未保存。"}
    _record_lineage("tone", 0, None, f"{tone} (trust={trust})")
    return {"success": True, "tone": tone, "trust": trust, "updated": data["updated"]}
