"""Meta-tools for tool folding: discover_tools + execute_advanced_tool.

借鉴 OpenAI namespace + Codex tool_search + Synapticlabs BCP 模式：
- 常驻工具始终暴露给 LLM
- 折叠工具按分组隐藏，模型调用 discover_tools(group) 获取 schema
- 再通过 execute_advanced_tool(name, args) 执行

预期省 60%+ 工具 schema token（从 ~29k 降到 ~11k）
"""
import json

from .registry import (
    register_tool,
    get_folded_group_defs,
    FOLDED_TOOL_GROUPS,
    execute_tool as _execute_tool_impl,
)


def _format_group_index():
    """生成分组索引文本（用于 discover_tools 的返回提示）。"""
    lines = []
    for name, info in FOLDED_TOOL_GROUPS.items():
        lines.append(f"  - {name}: {info['description']} ({len(info['tools'])}个工具)")
    return "\n".join(lines)


@register_tool(
    name="discover_tools",
    description=(
        "按分组发现折叠工具的完整 schema。常驻工具(web_search/bash/read_file/edit_file/"
        "remember/current_topic等约40个)无需发现，直接调用。"
        "需要高级操作时先调用本工具获取该组工具的参数格式，再用 execute_advanced_tool 执行。"
        "可用分组：task_management(任务管理)、task_dag_advanced(DAG高级)、"
        "memory_advanced(记忆高级/技能管理)、mindmap_advanced(思维导图高级)、"
        "domain_book(领域书页面CRUD)、misc(图片/模板/热重载)、"
        "interaction(向用户提问并等待回答)、file_versioning(文件历史与回溯)。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "group": {
                "type": "string",
                "enum": list(FOLDED_TOOL_GROUPS.keys()),
                "description": "要发现的工具分组名",
            }
        },
        "required": ["group"],
    }
)
def discover_tools(group):
    """返回指定分组的所有工具完整 schema。

    返回 dict:
      - group: 分组名
      - description: 分组描述
      - tools: [工具定义列表，每个含 name/description/parameters]
      - hint: 使用提示
    """
    defs = get_folded_group_defs(group)
    if defs is None:
        return {"error": f"未知分组: {group}。可用分组: {list(FOLDED_TOOL_GROUPS.keys())}"}

    group_info = FOLDED_TOOL_GROUPS[group]
    # 精简工具定义：只返回 name + description + parameters（去掉 label）
    slim_defs = []
    for d in defs:
        slim_defs.append({
            "name": d.get("name"),
            "description": d.get("description", ""),
            "parameters": d.get("parameters", {"type": "object", "properties": {}}),
        })

    return {
        "group": group,
        "description": group_info["description"],
        "tool_count": len(slim_defs),
        "tools": slim_defs,
        "hint": (
            f"已加载 {len(slim_defs)} 个工具。下一步：按上述 schema 准备参数，"
            f"调用 execute_advanced_tool(name=工具名, args=参数对象) 执行。"
        ),
    }


@register_tool(
    name="execute_advanced_tool",
    description=(
        "执行通过 discover_tools 发现的折叠工具。"
        "必须先调用 discover_tools(group) 获取工具 schema，"
        "然后按返回的参数格式调用本工具。"
        "name 是工具名（从 discover_tools 返回结果中获取），"
        "args 是参数对象（按 discover_tools 返回的 schema 提供）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "工具名（从 discover_tools 返回结果中获取）",
            },
            "args": {
                "type": "object",
                "description": "工具参数对象（按 discover_tools 返回的 schema 提供）",
                "properties": {},
            },
        },
        "required": ["name", "args"],
    }
)
def execute_advanced_tool(name, args=None):
    """执行折叠工具。

    复用 registry.execute_tool 路由到具体工具实现。
    args 为空时用空 dict。
    """
    if not name:
        return {"error": "缺少工具名 name"}

    if args is None:
        args = {}
    elif not isinstance(args, dict):
        return {"error": f"args 必须是对象，收到: {type(args).__name__}"}

    # 校验：name 必须是折叠工具（防止绕过分组机制调用常驻工具）
    from .registry import get_folded_tool_names, ALWAYS_ON_TOOLS
    if name in ALWAYS_ON_TOOLS:
        return {"error": f"{name} 是常驻工具，请直接调用，无需通过 execute_advanced_tool。"}
    if name not in get_folded_tool_names():
        return {"error": f"未知折叠工具: {name}。请先调用 discover_tools(group) 获取可用工具。"}

    result = _execute_tool_impl(name, args)
    return result
