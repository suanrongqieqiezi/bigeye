#!/usr/bin/env python3
"""Self-discovery: AI can ask what tools it has."""
from .registry import (
    register_tool, get_tool_defs,
    ALWAYS_ON_TOOLS, FOLDED_TOOL_GROUPS, META_TOOLS,
)


@register_tool(
    name="list_tools",
    description="看看自己能做什么——列出常驻工具和折叠工具分组。常驻工具可直接调用；折叠工具需先 discover_tools(group) 获取 schema 再 execute_advanced_tool 执行。",
    parameters={"type": "object", "properties": {}, "required": []}
)
def list_tools():
    tools = get_tool_defs()
    always_on = []
    folded_summary = []
    for t in tools:
        name = t["name"]
        desc = t.get("description", "")
        if name in META_TOOLS:
            always_on.append(f"{name} — {desc}")
        elif name in ALWAYS_ON_TOOLS:
            always_on.append(f"{name} — {desc}")

    # 折叠分组概览
    for group_name, info in FOLDED_TOOL_GROUPS.items():
        folded_summary.append(f"  [{group_name}] {info['description']} ({len(info['tools'])}个工具)")

    lines = ["━━━ 常驻工具（可直接调用）━━━"]
    lines.extend(always_on)
    lines.append("")
    lines.append("━━━ 折叠工具分组（需 discover_tools(group) 发现）━━━")
    lines.extend(folded_summary)
    lines.append("")
    lines.append("提示：调用 discover_tools(group_name) 获取折叠工具的完整 schema，")
    lines.append("然后用 execute_advanced_tool(name, args) 执行。")
    return "\n".join(lines)
