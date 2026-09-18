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
    get_tool_def_by_name,
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
        "可用分组见参数 enum。"
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
        "必须先 discover_tools(group) 拿到 schema，再把该工具的参数**完整放进 args 对象**："
        "execute_advanced_tool(name=\"工具名\", args={...})。"
        "例：execute_advanced_tool(name=\"ask_user\", args={\"question\": \"选 A 还是 B？\", \"options\": [\"A\", \"B\"]})；"
        "execute_advanced_tool(name=\"name_task\", args={\"name\": \"任务名\"})。"
        "args 的键名必须和 discover_tools 返回的 schema 完全一致；"
        "无参数的工具传 args={} 或省略 args。"
        "也兼容把内层参数平铺在顶层（如 execute_advanced_tool(name=\"book_read_page\", page_id=\"core_rules\")）。"
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
                "description": "内层工具的参数对象，键名按 discover_tools 返回的 schema；无参数工具可省略",
                "additionalProperties": True,
            },
        },
        "required": ["name"],
        "additionalProperties": True,
    }
)
def execute_advanced_tool(name, args=None, **extra):
    """执行折叠工具。

    容错三种模型常见写法（弱模型经常写错，这里统一兜住）：
      1) execute_advanced_tool(name="x", args={...})       标准
      2) execute_advanced_tool(name="x", args='{"a":1}')   args 是 JSON 字符串
      3) execute_advanced_tool(name="x", a=1)              参数平铺在顶层
    缺必填参数时返回可直接照抄的纠正提示，而不是把 TypeError 抛给模型。
    """
    if not name:
        return {"error": "缺少工具名 name"}

    # 写法 2：args 传成 JSON 字符串
    if isinstance(args, str):
        s = args.strip()
        if not s:
            args = {}
        else:
            try:
                args = json.loads(s)
            except json.JSONDecodeError:
                return {"error": f"args 不是合法 JSON：{s[:200]}"}

    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"error": f"args 必须是对象，收到: {type(args).__name__}"}

    # 写法 3：内层参数平铺在顶层，合并进来（args 里显式写过的优先）
    if extra:
        merged = dict(extra)
        merged.update(args)
        args = merged

    # 校验：name 必须是折叠工具（防止绕过分组机制调用常驻工具）
    from .registry import get_folded_tool_names, ALWAYS_ON_TOOLS
    if name in ALWAYS_ON_TOOLS:
        return {"error": f"{name} 是常驻工具，请直接调用，无需通过 execute_advanced_tool。"}
    if name not in get_folded_tool_names():
        return {"error": f"未知折叠工具: {name}。请先调用 discover_tools(group) 获取可用工具。"}

    # 必填参数预检：缺失时给出可直接照抄的调用示例
    d = get_tool_def_by_name(name) or {}
    required = (d.get("parameters") or {}).get("required") or []
    missing = [p for p in required if p not in args]
    if missing:
        demo = ", ".join(f'"{p}": ...' for p in required)
        return {
            "error": (
                f"{name} 缺少必填参数 {missing}。"
                f"正确写法：execute_advanced_tool(name=\"{name}\", args={{{demo}}})"
            ),
            "required": required,
            "received": sorted(args.keys()),
        }

    return _execute_tool_impl(name, args)
