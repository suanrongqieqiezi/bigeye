#!/usr/bin/env python3
"""
Tool registry — AI-powered function calling loop.
Tools are registered with JSON Schema definitions compatible with OpenAI function calling.
"""
import json
import traceback

# ── Registry ────────────────────────────────────────
_tools = {}  # name -> {"fn": callable, "definition": dict}


def register_tool(name, description, parameters=None, label=None):
    """Decorator to register a tool that AI can call.

    Args:
        name: Tool name (snake_case)
        description: What the tool does
        parameters: JSON Schema for parameters
        label: Optional display label
    """
    def decorator(fn):
        _tools[name] = {
            "fn": fn,
            "definition": {
                "name": name,
                "label": label or name,
                "description": description,
                "parameters": parameters or {
                    "type": "object",
                    "properties": {},
                },
            }
        }
        return fn
    return decorator


def get_tool_defs():
    """Get all tool definitions for LLM function calling, including MCP tools."""
    builtin = [t["definition"] for t in _tools.values()]
    try:
        from mcp_client import get_mcp_tool_defs
        mcp = get_mcp_tool_defs()
        return builtin + [{"type": "function", **d} for d in mcp]
    except Exception:
        return builtin


# ── Tool folding: 常驻工具 + 折叠分组 ──────────────
# 借鉴 OpenAI namespace + Codex tool_search + Synapticlabs BCP：
# - 常驻工具始终暴露给 LLM（高频核心工具）
# - 折叠工具按分组隐藏，模型调用 discover_tools(group) 后获取 schema，
#   再通过 execute_advanced_tool(name, args) 执行
# - 预期省 60%+ 工具 schema token

# 常驻工具集（始终暴露给 LLM，约 41 个高频核心工具）
ALWAYS_ON_TOOLS = {
    # 系统/执行
    "bash", "list_tools", "system_status",
    # Web
    "web_search", "web_fetch",
    # 文件操作
    "read_file", "write_file", "grep", "edit_file",
    # 代码
    "run_python",
    # 任务核心
    "current_topic", "create_task", "get_task_dag",
    # 注意力等级（focus_tools 注册但此前未暴露，AI 无法调用）
    "set_focus_level",
    # 任务挂件（系统提示每轮引用，必须常驻，否则 AI 打转）
    "update_task_brief", "write_draft",
    # 记忆核心
    "remember", "crystal_recall",
    # 上下文管理（工具输出 hint 频繁引用，必须常驻）
    "organize_context", "expand_compressed",
    # 思维导图核心
    "get_mindmap",
    # 领域书核心（系统提示每轮引用 book_hint）
    "book_list_pages", "book_read_page", "book_turn_to",
    # 重要事项（系统提示每轮引用 matters_manage_hint）
    "important_matters_list", "important_matters_add",
    "important_matters_update", "important_matters_remove",
    # 历史翻阅（系统提示每轮引用）
    "read_topic_messages",
}

# ── 工具场景裁剪（v2：常驻工具也按场景动态化）──
# 目标：减少每轮固定注入的 tools schema token（成本画像显示 42 工具 ≈ 10.9k tokens，占 70%+）
# 分三档：
#   CORE_TOOLS     —— 绝对高频核心，永不裁剪（文件/web/任务/记忆写入门）
#   PROTECTED_TOOLS—— 系统提示每轮文字引用（hint）的工具，裁剪会破坏提示-工具对应，永不裁剪
#   其余（场景工具）—— 从 ALWAYS_ON 移出，按话题场景动态注入 + 折叠组 discover 兜底可达
CORE_TOOLS = {
    # 系统/执行
    "bash", "list_tools", "system_status",
    # Web
    "web_search", "web_fetch",
    # 文件操作
    "read_file", "write_file", "grep", "edit_file",
    # 代码
    "run_python",
    # 任务核心
    "current_topic", "create_task", "get_task_dag",
    # 任务挂件（系统提示每轮引用）
    "update_task_brief", "write_draft",
    # 记忆核心（写入口，几乎每轮都可能用）
    "remember",
}

PROTECTED_TOOLS = {
    # 注意力系统自身组件（系统提示条件性引用，不裁）
    "set_focus_level", "organize_context", "expand_compressed",
    "crystal_recall", "get_mindmap", "read_topic_messages",
    # 领域书（book_hint 引用）
    "book_list_pages", "book_read_page", "book_turn_to",
    # 重要事项（matters_manage_hint 引用）
    "important_matters_list", "important_matters_add",
    "important_matters_update", "important_matters_remove",
}

# 场景工具：不在常驻，由 _get_scene_tools(tid) 按话题状态动态注入
# （有 DAG→任务工具；有 mindmap→导图工具；用过记忆→记忆扩展工具）
SCENE_TOOLS = {
    "list_topics", "start_node", "complete_node", "finish_task",
    "trace_memory", "remember_knowledge", "add_mindmap_node",
}

# 折叠分组：group_name -> {description, tools: [tool_names]}
# 每组 ≤10 个工具（业界最佳实践，超过此数检索质量下降）
FOLDED_TOOL_GROUPS = {
    "task_management": {
        "description": "任务管理操作：改名、历史翻阅、工作目录、碎片树、续轮、删除任务、插入DAG节点",
        "tools": [
            "name_task", "rename_topic", "rename_topics_batch",
            "read_history_before", "get_first_message",
            "get_workspace", "get_topic_tree", "continue_task",
            "find_empty_tasks", "delete_task", "insert_dag_node",
            "spawn_agent", "list_topics",
        ],
    },
    "task_dag_advanced": {
        "description": "DAG流程图高级操作：节点编辑、问题处理、模板复用、思维链、执行轨迹、返工",
        "tools": [
            "remove_dag_node", "update_node_deps", "dynamic_split",
            "ask_question", "report_blocker",
            "save_task_template", "load_task_template", "run_task",
            "append_thought_step", "get_thought_chain",
            "get_execution_trace", "rework_subtree",
            "start_node", "complete_node", "finish_task",
        ],
    },
    "memory_advanced": {
        "description": "记忆系统高级操作：记忆编辑/删除、反思回路、自我叙事、技能管理",
        "tools": [
            "edit_memory", "forget", "reinforce",
            "build_self_narrative", "reflection_loop",
            "list_skill_templates", "create_skill",
            "reflect", "trace_memory", "remember_knowledge",
        ],
    },
    "memory_recall": {
        "description": "记忆检索辅助：按主题翻阅碎片、查记忆盲区、查看叙事视角",
        "tools": [
            "recall_by_topic", "check_memory_gaps", "get_perspective",
        ],
    },
    "code_analysis": {
        "description": "代码静态分析：语法检查、结构解析、符号定位",
        "tools": [
            "check_python", "code_ast_parse", "code_find_defs", "code_get_symbol",
        ],
    },
    "mindmap_advanced": {
        "description": "思维导图高级操作：节点更新、连线管理、版本控制、DAG关联",
        "tools": [
            "update_mindmap_node", "add_mindmap_edge", "remove_mindmap_node",
            "remove_mindmap_edge", "update_mindmap_edge",
            "link_mindmap_to_dag", "mindmap_history", "mindmap_undo", "mindmap_redo",
            "add_mindmap_node",
        ],
    },
    "domain_book": {
        "description": "领域说明书操作：页面创建/编辑/删除、搜索、知识页挂载",
        "tools": [
            "book_search",
            "book_create_page", "book_edit_page", "book_delete_page",
            "book_link_knowledge", "book_unlink_knowledge",
        ],
    },
    "misc": {
        "description": "其他工具：图片生成、模板引擎、编辑预览、热重载、文件搜索",
        "tools": [
            "generate_image",
            "list_templates", "create_from_template", "preview_template",
            "edit_preview", "hot_reload", "file_search",
        ],
    },
    "file_versioning": {
        "description": "文件修改历史与回溯：查看修改历史链、恢复文件到任意历史版本",
        "tools": [
            "file_history", "file_restore",
        ],
    },
    "interaction": {
        "description": "用户交互：阻塞式向用户提问并等待回答（ask_user）",
        "tools": [
            "ask_user",
        ],
    },
    "rules_engine": {
        "description": "规则引擎维护：查看/新增/修改/删除触发规则（工具化操作，不直接改文件）",
        "tools": [
            "rule_list", "rule_add", "rule_update", "rule_delete",
        ],
    },
}

# 元工具（discover_tools + execute_advanced_tool）
META_TOOLS = {"discover_tools", "execute_advanced_tool"}


def get_always_on_tool_defs():
    """获取常驻工具定义（不含折叠工具、不含元工具）。

    用于 LLM 请求的 tools 数组，配合 discover_tools/execute_advanced_tool 元工具。
    """
    return [t["definition"] for name, t in _tools.items()
            if name in ALWAYS_ON_TOOLS]


def get_folded_tool_names():
    """获取所有折叠工具名（扁平集合）。"""
    names = set()
    for group in FOLDED_TOOL_GROUPS.values():
        names.update(group["tools"])
    return names


def get_tool_def_by_name(name):
    """按名称获取单个工具定义。"""
    t = _tools.get(name)
    return t["definition"] if t else None


def get_folded_group_defs(group_name):
    """获取指定分组的所有工具完整定义。

    返回 list[dict]，每个 dict 是标准 tool definition。
    若 group_name 不存在返回 None。
    """
    group = FOLDED_TOOL_GROUPS.get(group_name)
    if not group:
        return None
    defs = []
    for tool_name in group["tools"]:
        d = get_tool_def_by_name(tool_name)
        if d:
            defs.append(d)
    return defs


def execute_tool(name, args):
    """Execute a tool by name with given args. Returns result dict."""
    # Route MCP tools
    if name.startswith("mcp_"):
        try:
            from mcp_client import execute_mcp_tool
            return execute_mcp_tool(name, args)
        except Exception as e:
            return {"error": f"MCP 工具执行失败: {e}"}
    tool = _tools.get(name)
    if not tool:
        return {"error": f"未知工具: {name}"}
    try:
        result = tool["fn"](**args)
        # Don't wrap if tool already returned a proper error dict
        if isinstance(result, dict) and "error" in result:
            return result
        return {"result": result}
    except Exception as e:
        traceback.print_exc()
        return {"error": f"工具执行失败: {e}"}


def execute_tool_loop(llm_client, messages, max_turns=10):
    """Full tool calling loop: LLM → tool_calls → execute → feed back → repeat.

    Args:
        llm_client: callable(messages, tools) that yields events
        messages: list of message dicts (will be mutated)
        max_turns: max tool call rounds

    Returns:
        final_text: The final assistant response
        tool_log: list of {name, args, result}
    """
    tool_defs = get_tool_defs()
    tool_log = []
    final_text = ""

    for turn in range(max_turns):
        has_tool_calls = False
        turn_text = ""
        turn_tool_calls = []

        for event in llm_client(messages, tools=tool_defs):
            et = event["type"]
            if et == "text_delta":
                turn_text += event["delta"]
            elif et == "tool_call":
                has_tool_calls = True
                turn_tool_calls.append(event)
            elif et == "error":
                return turn_text, tool_log, event["error"]

        if not has_tool_calls:
            final_text = turn_text
            break

        # Execute each tool call
        for tc in turn_tool_calls:
            tool_name = tc["tool_name"]
            args = tc["arguments"]
            result = execute_tool(tool_name, args)
            tool_log.append({"name": tool_name, "args": args, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc["tool_call_id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

        final_text = turn_text

    return final_text, tool_log, None
