#!/usr/bin/env python3
"""
Node planner — coarse-grained initial plan + dynamic splitting.

Strategy: hybrid
  - Start: coarse plan of 3-7 high-level nodes
  - During execution: model dynamically splits nodes when sub-tasks are found

AI outputs X6-standard {nodes, edges} JSON — no tree/dependency conversion needed.
"""
import json
import re
import time
import uuid

COARSE_PLANNER_PROMPT = """你是一个任务规划专家。请将用户请求拆分为 3-7 个粗粒度执行步骤（节点），并用 edges 明确指定节点之间的连线关系。

输出 JSON 格式（X6 标准）：
{
  "nodes": [
    { "id": "step_1", "task": "第一步做什么（一句话）" },
    { "id": "step_2", "task": "第二步做什么（一句话）" },
    { "id": "step_3", "task": "第三步做什么（一句话）" }
  ],
  "edges": [
    { "source": "step_1", "target": "step_2", "edge_type": "flow" },
    { "source": "step_2", "target": "step_3", "edge_type": "flow" }
  ]
}

edge_type 说明：
- flow = 执行顺序（A 做完才做 B）
- dependency = 数据依赖（B 需要 A 的产出，但不必等 A 做完）

连线规则（重要！）：
1. edges 必须完整覆盖所有节点间的执行/依赖关系——不能只写 nodes 不写 edges
2. 不要把所有节点都连到第一个节点形成星暴——按执行顺序链式或分支连接
3. 可并行的节点之间不连线（例如 step_2 和 step_3 都只依赖 step_1，但互不依赖）
4. 先想清楚"谁连谁"再输出，不是先列节点再补连线
5. 不要创建覆盖相同内容的平行节点链
6. 节点数 3-7 个，每个节点一句话说清做什么
7. 只输出 JSON，不要额外文字
"""


def coarse_plan(user_request, llm_prompt_fn):
    """Generate initial DAG plan from user request.

    Args:
        user_request: Raw user request text.
        llm_prompt_fn: Callable(system_prompt, user_prompt) → str.

    Returns:
        Dict with "nodes" and "edges" lists:
        {"nodes": [{"id","task"}], "edges": [{"source","target","edge_type"}]}
    """
    if not llm_prompt_fn:
        return _fallback_plan(user_request)

    response = llm_prompt_fn(COARSE_PLANNER_PROMPT, user_request)
    return _parse_plan(response) or _fallback_plan(user_request)


def coarse_plan_with_feedback(user_request, llm_prompt_fn, max_retries=3):
    """Generate initial DAG plan with compiler-style error feedback.

    Flow:
      1. AI outputs {nodes, edges}
      2. Code validates → if errors, feed errors back to AI
      3. AI retries with error context (up to max_retries)
      4. If still errors after retries, auto-fix as fallback

    Returns:
        Dict with "nodes", "edges", and "errors" (list of validation errors, empty if clean).
    """
    from .executor import TaskExecutor

    if not llm_prompt_fn:
        plan = _fallback_plan(user_request)
        plan["errors"] = []
        return plan

    # 第一次规划
    response = llm_prompt_fn(COARSE_PLANNER_PROMPT, user_request)
    plan = _parse_plan(response) or _fallback_plan(user_request)

    # 收集 AI 输出中的节点 ID
    plan_nodes = plan.get("nodes", [])
    ai_node_ids = {n.get("id", "") for n in plan_nodes if n.get("id")}
    node_order = [n.get("id", "") for n in plan_nodes if n.get("id")]

    # 校验 + 收集错误
    errors = TaskExecutor.validate_edges(plan.get("edges", []), ai_node_ids, node_order)

    if not errors:
        plan["errors"] = []
        return plan

    # 有错误 → 反馈给 AI 重试
    for attempt in range(max_retries):
        error_report = _format_error_report(errors, attempt + 1)
        retry_prompt = (
            f"你刚才输出的流程图 JSON 有以下语法错误：\n\n{error_report}\n\n"
            f"请修正这些错误，重新输出完整的 {len(plan_nodes)} 个节点的 {{nodes, edges}} JSON。"
            f"保持节点内容不变，只修正 edges 的语法问题。只输出 JSON。"
        )
        response = llm_prompt_fn(COARSE_PLANNER_PROMPT, retry_prompt)
        plan = _parse_plan(response) or plan
        ai_node_ids = {n.get("id", "") for n in plan.get("nodes", []) if n.get("id")}
        node_order = [n.get("id", "") for n in plan.get("nodes", []) if n.get("id")]
        errors = TaskExecutor.validate_edges(plan.get("edges", []), ai_node_ids, node_order)
        if not errors:
            plan["errors"] = []
            return plan

    # 超过重试次数，返回带错误的 plan，由 init_task 自动修复
    plan["errors"] = errors
    return plan


def _format_error_report(errors, attempt):
    """Format validation errors as a readable report for the AI."""
    lines = [f"第 {attempt} 次校验发现 {len(errors)} 个错误："]
    for i, e in enumerate(errors, 1):
        lines.append(f"  {i}. {e}")
    return "\n".join(lines)


def _parse_plan(text):
    """Try to extract JSON plan from LLM response.
    Returns {"nodes": [...], "edges": [...]} or None.
    """
    text = text.strip()
    # Strip markdown fenced blocks
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
    try:
        data = json.loads(text)
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        if nodes and isinstance(nodes, list):
            # Backward compat: if AI returned dependencies but no edges, derive edges
            if not edges:
                edges = _derive_edges_from_dependencies(nodes)
            return {"nodes": nodes, "edges": edges}
    except (json.JSONDecodeError, TypeError):
        pass
    # Try to find JSON object in text
    m = re.search(r'\{[^{}]*"nodes"[^{}]*\[.*?\][^{}]*\}', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            nodes = data.get("nodes", [])
            edges = data.get("edges", [])
            if nodes and isinstance(nodes, list):
                if not edges:
                    edges = _derive_edges_from_dependencies(nodes)
                return {"nodes": nodes, "edges": edges}
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _derive_edges_from_dependencies(nodes):
    """Backward compat: if AI only returned dependencies (old format), derive edges."""
    edges = []
    for n in nodes:
        deps = n.get("dependencies", [])
        for dep_id in deps:
            edges.append({
                "source": dep_id,
                "target": n["id"],
                "edge_type": "dependency",
            })
    return edges


def _fallback_plan(user_request):
    """Fallback deterministic plan when LLM unavailable."""
    return {
        "nodes": [
            {"id": "step_1", "task": f"分析需求: {user_request[:100]}"},
            {"id": "step_2", "task": "执行核心任务"},
            {"id": "step_3", "task": "验证和整理结果"},
        ],
        "edges": [
            {"source": "step_1", "target": "step_2", "edge_type": "flow"},
            {"source": "step_2", "target": "step_3", "edge_type": "flow"},
        ],
    }


def dynamic_split(node, sub_tasks, llm_prompt_fn=None):
    """Dynamically split a node into child nodes.

    Returns list of child node dicts with edges:
    {"nodes": [...], "edges": [...]}
    """
    if not sub_tasks:
        return {"nodes": [], "edges": []}
    # Simple: one sub-task per child, sequential by default
    children = []
    edges = []
    prev_id = None
    for i, task_desc in enumerate(sub_tasks[:5]):  # max 5 children
        cid = f"{node['id']}_sub_{i+1}"
        children.append({"id": cid, "task": task_desc})
        # Edge from parent to first child
        if i == 0:
            edges.append({"source": node["id"], "target": cid, "edge_type": "flow"})
        else:
            # Sequential: prev child → this child
            edges.append({"source": prev_id, "target": cid, "edge_type": "flow"})
        prev_id = cid
    return {"nodes": children, "edges": edges}


def template_load(user_request, memory_store=None, llm_prompt_fn=None):
    """Try to load a matching task template from L3 memory.

    ⑥→① 模板复用: 先找相似历史任务的DAG模板，修改后复用。
    Falls back to coarse_plan if no template found.

    Returns: {"nodes": [...], "edges": [...]} or None.
    """
    if not memory_store:
        return None

    try:
        # Search for task_template fragments
        templates = memory_store.recall(
            user_request,
            top_k=5,
            threshold=0.3,
            layer="core",
            topic_id=None,
        )
        if not templates:
            templates = memory_store.recall_archive(
                user_request, top_k=3, threshold=0.3
            ) or []

        for t in templates:
            try:
                data = json.loads(t.get("text", ""))
                if not isinstance(data, dict) or data.get("type") != "task_template":
                    continue
                template_dag = data.get("dag", {})
                if not template_dag:
                    continue

                # Convert template DAG to plan nodes + edges
                plan = _template_tree_to_plan(template_dag)
                if not plan or not plan.get("nodes"):
                    continue

                # Adapt with LLM if available
                if llm_prompt_fn:
                    tname = data.get("template_name", "未命名")
                    adapted = llm_prompt_fn(
                        "你是一个任务规划适配专家。以下是一个已有任务的DAG模板结构。"
                        "请根据新的用户请求，对模板进行增删改适配。"
                        "输出格式与输入一致（X6 标准 {nodes, edges}），每个节点包含 id, task。",
                        f"原模板「{tname}」的结构:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n新请求: {user_request}\n\n适配后的结构(JSON):"
                    )
                    if adapted:
                        parsed = _parse_plan(adapted)
                        if parsed:
                            return parsed

                return plan
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        return None
    except Exception:
        return None


def _template_tree_to_plan(tree_node, prefix="step_"):
    """Convert a template DAG tree to flat plan {nodes, edges}."""
    nodes = []
    edges = []
    counter = [0]

    def _walk(node, parent_nid=None):
        counter[0] += 1
        nid = f"{prefix}{counter[0]}"
        nodes.append({
            "id": nid,
            "task": node.get("task", ""),
        })
        if parent_nid:
            edges.append({"source": parent_nid, "target": nid, "edge_type": "flow"})
        # Template dependencies
        deps = node.get("dependencies", [])
        for dep_id in deps:
            edges.append({"source": dep_id, "target": nid, "edge_type": "dependency"})
        for child in node.get("children", []):
            _walk(child, nid)

    _walk(tree_node)
    return {"nodes": nodes, "edges": edges}
