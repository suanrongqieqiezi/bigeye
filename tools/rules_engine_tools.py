#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则引擎管理工具 — 通过框架工具维护 rules.json，不直接读文件。
注册 4 个工具：rule_list / rule_add / rule_update / rule_delete
平时收在折叠分组 rules_engine 里，不占上下文；需要时 discover_tools('rules_engine') 取 schema。
"""
import json
import os
import sys
import threading

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from tools.registry import register_tool

RULES_DIR = os.path.join(_root, "rules_engine")
RULES_FILE = os.path.join(RULES_DIR, "rules.json")
_lock = threading.Lock()


def _load():
    """读取 rules.json，返回完整 dict。文件不存在时返回空结构。"""
    if not os.path.exists(RULES_FILE):
        return {"version": 1, "rules": []}
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    """原子写回 rules.json。"""
    tmp = RULES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RULES_FILE)


def _validate_condition(condition):
    """校验条件表达式语法（与 run_rules.py 的白名单一致）。"""
    import ast
    try:
        ast.parse(condition, mode="eval")
    except SyntaxError as e:
        return f"条件表达式语法错误: {e}"
    return None


@register_tool(
    name="rule_list",
    description="列出规则引擎当前所有规则（id/名称/等级/触发条件/提示/动作）。"
                "规则引擎是事件触发：你发消息瞬间检查，命中只在上下文里提示，不写库不推APP。",
    parameters={"type": "object", "properties": {}, "required": []}
)
def rule_list():
    with _lock:
        data = _load()
    rules = data.get("rules", [])
    if not rules:
        return {"rules": [], "total": 0, "note": "当前没有规则，用 rule_add 添加"}
    lines = []
    for idx, r in enumerate(rules):
        lines.append(f"[{idx+1}] {r.get('id')} | {r.get('name')} | {r.get('severity', 'medium')}")
        lines.append(f"    条件: {r.get('condition')}")
        lines.append(f"    提示: {r.get('hint')}")
        lines.append(f"    动作: {r.get('action', '')}")
    return {"total": len(rules), "rules": rules, "summary": "\n".join(lines)}


@register_tool(
    name="rule_add",
    description="新增一条规则引擎规则。id 用 snake_case 唯一标识；condition 是布尔表达式，可用变量："
                "topic_title/topic_id/msg_count/dag_nodes/task_status/extra_pages_open 等（与 run_rules.py build_env 一致）。"
                "severity 取值 high/medium/low。改完即生效（引擎实时读 rules.json），无需重启。",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "规则唯一标识，snake_case，如 topic_unnamed"},
            "name": {"type": "string", "description": "规则名称，如'任务未命名'"},
            "condition": {"type": "string", "description": "触发条件表达式，如 topic_title == '新任务'"},
            "hint": {"type": "string", "description": "命中后给 AI 的提示文本"},
            "desc": {"type": "string", "description": "规则说明（可选）", "default": ""},
            "severity": {"type": "string", "description": "等级 high/medium/low，默认 medium", "default": "medium"},
            "action": {"type": "string", "description": "建议动作（可选）", "default": ""},
        },
        "required": ["id", "name", "condition", "hint"],
    }
)
def rule_add(id, name, condition, hint, desc="", severity="medium", action=""):
    with _lock:
        data = _load()
        rules = data.setdefault("rules", [])
        if any(r.get("id") == id for r in rules):
            return {"error": f"规则 id '{id}' 已存在，用 rule_update 修改"}
        err = _validate_condition(condition)
        if err:
            return {"error": err}
        if severity not in ("high", "medium", "low"):
            return {"error": f"severity 必须是 high/medium/low，收到 '{severity}'"}
        rule = {"id": id, "name": name, "desc": desc, "severity": severity,
                "condition": condition, "hint": hint, "action": action}
        rules.append(rule)
        _save(data)
        return {"success": True, "added": rule, "total": len(rules)}


@register_tool(
    name="rule_update",
    description="修改一条已有规则。rule_id 指定规则，其余参数只传要改的字段（不传保持不变）。"
                "改完即生效，无需重启。",
    parameters={
        "type": "object",
        "properties": {
            "rule_id": {"type": "string", "description": "要修改的规则 id"},
            "name": {"type": "string", "description": "新名称（可选）"},
            "condition": {"type": "string", "description": "新触发条件（可选）"},
            "hint": {"type": "string", "description": "新提示文本（可选）"},
            "desc": {"type": "string", "description": "新说明（可选）"},
            "severity": {"type": "string", "description": "新等级 high/medium/low（可选）"},
            "action": {"type": "string", "description": "新建议动作（可选）"},
        },
        "required": ["rule_id"],
    }
)
def rule_update(rule_id, name=None, condition=None, hint=None, desc=None, severity=None, action=None):
    with _lock:
        data = _load()
        rules = data.get("rules", [])
        rule = next((r for r in rules if r.get("id") == rule_id), None)
        if not rule:
            return {"error": f"规则 id '{rule_id}' 不存在，用 rule_list 查看全部"}
        if condition is not None:
            err = _validate_condition(condition)
            if err:
                return {"error": err}
            rule["condition"] = condition
        if name is not None:
            rule["name"] = name
        if hint is not None:
            rule["hint"] = hint
        if desc is not None:
            rule["desc"] = desc
        if severity is not None:
            if severity not in ("high", "medium", "low"):
                return {"error": f"severity 必须是 high/medium/low，收到 '{severity}'"}
            rule["severity"] = severity
        if action is not None:
            rule["action"] = action
        _save(data)
        return {"success": True, "updated": rule, "total": len(rules)}


@register_tool(
    name="rule_delete",
    description="删除一条规则。rule_id 指定要删除的规则 id。删完即生效，无需重启。",
    parameters={
        "type": "object",
        "properties": {
            "rule_id": {"type": "string", "description": "要删除的规则 id"},
        },
        "required": ["rule_id"],
    }
)
def rule_delete(rule_id):
    with _lock:
        data = _load()
        rules = data.get("rules", [])
        before = len(rules)
        rules = [r for r in rules if r.get("id") != rule_id]
        if len(rules) == before:
            return {"error": f"规则 id '{rule_id}' 不存在，用 rule_list 查看全部"}
        data["rules"] = rules
        _save(data)
        return {"success": True, "deleted": rule_id, "total": len(rules)}
