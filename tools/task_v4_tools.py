#!/usr/bin/env python3
"""v4.0 Task execution tools — AI-callable for the task execution layer."""
import json
import sys
import os
import time
import uuid


# Ensure project root is on path for absolute imports
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from .registry import register_tool
from task.dag import DAG
from task.work_memory import WorkMemory
from task.attention_focus import AttentionFocus, slide_out, slide_in
from task.executor import TaskExecutor
from task.reflection import reflect_and_sediment
from task.thought_chain import ThoughtChain

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Lazy store import
def _get_memory_store():
    try:
        sys.path.insert(0, BASE_DIR)
        from memory.fragment_store import get_store
        return get_store()
    except Exception:
        return None

def _get_active_topic_id():
    """Get the active chat topic ID from the database."""
    try:
        from db import get_db
        db = get_db()
        return db.get_active_topic_id()
    except Exception:
        return None

def _save_dag_if_bound(dag):
    """Auto-persist DAG to mission folder if it has a topic_id."""
    try:
        task = dag.get_task()
        if task and task.get("topic_id"):
            dag.save_to_file(task["topic_id"])
    except Exception:
        pass


@register_tool(
    name="create_task",
    description="用户下达≥2步任务时调用（查资料/闲聊不用）。建图纪律：①每节点必须是一件事，能说清产出和验证方法，说不清=继续拆②用户要求'画完整个原理/一步步/每方向'时按内部结构/每步/每领域逐块拆③禁止'执行核心任务/分析和整理结果/整体收尾'类空节点④节点数由真实复杂度定，4个节点往往是模板病信号，复杂任务20+节点正常⑤已有DAG的话题复用现有图，用insert/remove/update_node_deps增量修改，绝不覆盖重建。",
    parameters={
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "用户的任务需求描述",
            },
        },
        "required": ["request"],
    },
)
def create_task(request: str):
    """Plan a task from user request, build initial DAG. Auto-binds to current topic.
    One DAG per topic: if topic already has a DAG (any status), ALWAYS reuse it.
    NEVER overwrite/replace an existing DAG — edit it incrementally instead.

    If the existing DAG is wrong/incomplete/failed, the AI must fix it with:
      - insert_dag_node: add missing steps
      - remove_dag_node: remove wrong steps
      - update_node_deps: fix dependencies
    Do NOT try to rebuild from scratch. Do NOT call create_task again expecting
    a fresh DAG — it will always return the existing one."""
    topic_id = _get_active_topic_id()
    # Check if topic already has a DAG → ALWAYS reuse (never overwrite)
    if topic_id:
        existing_id, existing_task = DAG.get_task_by_topic(topic_id)
        if existing_id:
            existing_status = (existing_task or {}).get("status", "")
            dag = DAG(existing_id)
            task = dag.get_task()
            nodes = dag.get_nodes()
            status_counts = {}
            for n in nodes:
                s = n["status"]
                status_counts[s] = status_counts.get(s, 0) + 1
            # Build guidance message based on DAG state
            if existing_status in ("failed", "stale"):
                guidance = (
                    f"话题已有DAG（{existing_id[:8]}，状态 {existing_status}），不能覆盖重建。"
                    f"如需修改：用 insert_dag_node 增加节点、remove_dag_node 删除节点、"
                    f"update_node_deps 调整依赖。不要重复调用 create_task，它永远返回现有DAG。"
                )
            else:
                guidance = (
                    f"话题已有DAG（{existing_id[:8]}），共 {len(nodes)} 个节点，复用。"
                    f"用 get_task_dag() 查看详情。如需调整：insert_dag_node / remove_dag_node / update_node_deps。"
                )
            return {
                "task_id": existing_id,
                "topic_id": topic_id,
                "reused": True,
                "task": task,
                "nodes": [{"id": n["id"], "task": n["task"], "status": n["status"],
                           "parent_id": n.get("parent_id"), "dependencies": json.loads(n.get("dependencies", "[]"))}
                          for n in nodes],
                "stats": status_counts,
                "info": guidance,
            }
    # No existing DAG → create new one
    task_id = uuid.uuid4().hex[:12]
    dag = DAG(task_id)
    wm = WorkMemory(task_id)
    attention = AttentionFocus()
    store = _get_memory_store()
    executor = TaskExecutor(dag, wm, attention, store)
    try:
        task = executor.init_task(request)
        if topic_id:
            dag.set_topic_id(topic_id)
            dag.save_to_file(topic_id)  # persist to mission folder
        nodes = dag.get_nodes()
        status_counts = {}
        for n in nodes:
            s = n["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
        return {
            "task_id": task_id,
            "topic_id": topic_id,
            "reused": False,
            "task": task,
            "nodes": [{"id": n["id"], "task": n["task"], "status": n["status"],
                       "parent_id": n.get("parent_id"), "dependencies": json.loads(n.get("dependencies", "[]"))}
                      for n in nodes],
            "stats": status_counts,
            "info": f"已创建任务「{request[:60]}」，共 {len(nodes)} 个执行节点。已绑定话题{f' {topic_id[:8]}' if topic_id else ''}，DAG 已存盘。用 get_task_dag() 查看。",
        }
    except Exception as e:
        return {"error": f"任务创建失败: {e}"}


@register_tool(
    name="get_task_dag",
    description="查看任务DAG流程图。无参自动找当前话题，返回节点树、状态统计、可执行节点。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务ID（可选，不传则查找当前话题的任务）",
            },
        },
        "required": [],
    },
)
def get_task_dag(task_id: str = ""):
    """Get DAG tree + task info + node status summary.
    If task_id is empty, lookup by current topic_id."""
    try:
        if not task_id:
            topic_id = _get_active_topic_id()
            if topic_id:
                task_id, _ = DAG.get_task_by_topic(topic_id)
            if not task_id:
                return {"error": "没有找到当前话题关联的任务。用 create_task 创建一个。"}
        dag = DAG(task_id)
        task = dag.get_task()
        if not task:
            return {"error": f"任务 {task_id} 不存在"}
        tree = dag.get_tree()
        nodes = dag.get_nodes()
        status_counts = {}
        for n in nodes:
            s = n["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
        if not nodes and task.get("dag_snapshot"):
            import json
            snap = json.loads(task["dag_snapshot"])
            return {
                "task": task,
                "tree": snap.get("tree"),
                "node_count": len(snap.get("nodes", [])),
                "stats": {},
                "note": "来自DAG快照（历史数据）",
            }
        return {
            "task": task,
            "tree": tree,
            "node_count": len(nodes),
            "stats": status_counts,
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="start_node",
    description="开始执行一个节点：滑入注意力焦点并加载上下文到工作记忆。返回5块注意力信息。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务ID",
            },
            "node_id": {
                "type": "string",
                "description": "要开始执行的节点ID",
            },
        },
        "required": ["task_id", "node_id"],
    },
)
def start_node(task_id: str, node_id: str):
    """Slide attention to a node. Returns 5-block context."""
    try:
        dag = DAG(task_id)
        wm = WorkMemory(task_id)
        attention = AttentionFocus()
        store = _get_memory_store()
        slide_in(dag, wm, node_id, store, attention)
        dag.set_status(node_id, "running")
        node = dag.get_node(node_id)
        context = attention.get_all_blocks_text()
        return {
            "node": {
                "id": node["id"],
                "task": node["task"],
                "status": node["status"],
            },
            "attention_context": context,
            "info": f"注意力已切换到节点「{node['task'][:60]}」。当前节点上下文已加载。",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="complete_node",
    description="完成当前节点。issues 只放真问题，别为触发 roundtrip 硬塞。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务ID",
            },
            "node_id": {
                "type": "string",
                "description": "要完成的节点ID",
            },
            "result": {
                "type": "string",
                "description": "节点执行结果",
            },
            "issues": {
                "type": "string",
                "description": "遇到的问题列表，JSON格式：[{\"type\":\"question|blocker|uncertain\",\"text\":\"...\"}]",
            },
            "next_node": {
                "type": "string",
                "description": "建议的下一个注意力焦点节点ID（可选）",
            },
            "trigger_roundtrip": {
                "type": "boolean",
                "description": "是否立即触发往返求解",
            },
        },
        "required": ["task_id", "node_id", "result"],
    },
)
def complete_node(task_id: str, node_id: str, result: str,
                  issues: str = "[]", next_node: str = "",
                  trigger_roundtrip: bool = False):
    """Complete a node: record result, issues, handle roundtrip triggers."""
    try:
        dag = DAG(task_id)
        wm = WorkMemory(task_id)
        attention = AttentionFocus()

        # Parse issues JSON
        try:
            issue_list = json.loads(issues) if isinstance(issues, str) else issues
        except (json.JSONDecodeError, TypeError):
            issue_list = []

        # Record issues
        for issue in issue_list:
            etype = issue.get("type", "question")
            text = issue.get("text", "")
            if text:
                wm.add_entry(node_id, etype, text)

        # Check for blockers — set status, handling idempotent transitions
        node = dag.get_node(node_id)
        current = node["status"] if node else "pending"
        any_blocker = any(i.get("type") == "blocker" for i in issue_list)
        if any_blocker:
            if current != "blocked":
                dag.set_status(node_id, "blocked", result=result)
            else:
                dag.update_context(node_id, {"_blocker_reported": True})
        elif issue_list and current in ("pending", "running"):
            dag.set_status(node_id, "running", result=result)
        elif current in ("pending", "running"):
            dag.set_status(node_id, "done", result=result)
        # else: already blocked/failed/done — keep as-is

        # Roundtrip check
        rt_info = None
        q_count = wm.count_questions()
        has_blocker = wm.has_blocker()
        if trigger_roundtrip or has_blocker or q_count >= 5:
            from task.work_memory import roundtrip_solve
            rt_results = roundtrip_solve(wm)
            rt_info = {
                "triggered": True,
                "results": rt_results,
            }

        # Slide out
        slide_out(dag, wm, node_id, attention)

        # Get runnable nodes
        runnable = dag.get_runnable_nodes()

        _save_dag_if_bound(dag)
        return {
            "node_status": dag.get_node(node_id)["status"],
            "roundtrip": rt_info,
            "next_node_suggestion": next_node or (runnable[0]["id"] if runnable else None),
            "runnable_nodes": [{"id": n["id"], "task": n["task"][:60]} for n in (runnable or [])],
            "pending_questions": q_count,
            "has_blockers": has_blocker,
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="finish_task",
    description="结束任务，反思沉淀到长时记忆。DAG 数据保留不删，反思简短即可。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务ID",
            },
            "status": {
                "type": "string",
                "enum": ["done", "failed"],
                "description": "最终状态",
            },
            "reason": {
                "type": "string",
                "description": "失败原因（status=failed时填写）",
            },
        },
        "required": ["task_id"],
    },
)
def finish_task(task_id: str, status: str = "done", reason: str = ""):
    """Finish task: update status, reflect, sediment to v3.0, clear work memory."""
    try:
        dag = DAG(task_id)
        wm = WorkMemory(task_id)
        attention = AttentionFocus()
        store = _get_memory_store()
        executor = TaskExecutor(dag, wm, attention, store)

        if status == "done":
            executor.finish_task()
        else:
            executor.fail_task(reason)

        # Reflect and sediment
        sedi = reflect_and_sediment(executor)
        # Persist DAG to mission folder
        try:
            topic_id = dag.get_task().get("topic_id") if dag.get_task() else None
            if topic_id:
                dag.save_to_file(topic_id)
        except Exception:
            pass
        task = dag.get_task()
        info = f"任务已{'完成' if status == 'done' else '失败'}。沉淀了 {sedi.get('narrative', 0)} 条叙事 + {sedi.get('solutions', 0)} 条解决方案到长时记忆。"
        return {
            "task": task,
            "sediment": sedi,
            "info": info,
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="ask_question",
    description="在当前节点执行过程中记录一个问题或发现（不会阻塞执行）。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务ID",
            },
            "node_id": {
                "type": "string",
                "description": "当前执行的节点ID",
            },
            "question": {
                "type": "string",
                "description": "遇到的问题或需要查的信息",
            },
        },
        "required": ["task_id", "node_id", "question"],
    },
)
def ask_question(task_id: str, node_id: str, question: str):
    """Record a question during execution. Does not block."""
    try:
        wm = WorkMemory(task_id)
        entry = wm.add_entry(node_id, "question", question)
        q_count = wm.count_questions()
        return {
            "entry_id": entry.get("id"),
            "entry_type": "question",
            "pending_count": q_count,
            "info": f"问题已记录。当前共有 {q_count} 个待解决问题，达到5条会自动触发往返求解。",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="report_blocker",
    description="报告一个执行阻塞（会阻塞当前节点，立即触发往返求解）。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务ID",
            },
            "node_id": {
                "type": "string",
                "description": "被阻塞的节点ID",
            },
            "description": {
                "type": "string",
                "description": "阻塞描述",
            },
        },
        "required": ["task_id", "node_id", "description"],
    },
)
def report_blocker(task_id: str, node_id: str, description: str):
    """Report a blocker. Marks node blocked, triggers roundtrip."""
    try:
        dag = DAG(task_id)
        wm = WorkMemory(task_id)
        entry = wm.add_entry(node_id, "blocker", description)
        dag.set_status(node_id, "blocked")
        return {
            "entry_id": entry.get("id"),
            "entry_type": "blocker",
            "info": f"阻塞已记录：{description[:80]}。节点已标记为阻塞，等待往返求解。",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="dynamic_split",
    description="将当前节点拆分为多个子节点。场景：执行中发现一个节点太大需要细分。当前节点变split状态，子节点加入DAG。与insert_dag_node区别：split是拆分当前节点，insert是在某节点下追加。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务ID",
            },
            "node_id": {
                "type": "string",
                "description": "要拆分的父节点ID",
            },
            "sub_tasks": {
                "type": "string",
                "description": "子任务列表，JSON数组：[{\"task\":\"子任务描述\",\"dependencies\":[\"前置子节点id\"]}]",
            },
        },
        "required": ["task_id", "node_id", "sub_tasks"],
    },
)
def dynamic_split(task_id: str, node_id: str, sub_tasks: str):
    """Dynamically split a node into children during execution."""
    try:
        dag = DAG(task_id)
        parent = dag.get_node(node_id)
        if not parent:
            return {"error": f"节点 {node_id} 不存在"}

        try:
            tasks = json.loads(sub_tasks) if isinstance(sub_tasks, str) else sub_tasks
        except (json.JSONDecodeError, TypeError):
            return {"error": "sub_tasks 格式错误，需要JSON数组"}

        created = []
        id_map = {}
        for i, st in enumerate(tasks[:5]):
            task_text = st.get("task", st if isinstance(st, str) else "")
            deps = [id_map.get(d, "") for d in st.get("dependencies", []) if d in id_map]
            child = dag.create_node(task_text, parent_id=node_id, dependencies=deps)
            id_map[st.get("id", f"sub_{i}")] = child["id"]
            created.append(child)

        # Mark parent as split
        dag.set_status(node_id, "split")

        _save_dag_if_bound(dag)
        return {
            "parent_status": "split",
            "children": [{"id": c["id"], "task": c["task"][:60]} for c in created],
            "info": f"节点已拆分为 {len(created)} 个子节点。",
        }
    except Exception as e:
        return {"error": str(e)}


# ── 改图工具（流程图即工作面板） ─────────────────────

@register_tool(
    name="insert_dag_node",
    description="向DAG中插入新节点。场景：深化分析、拆分子任务。边会自动创建：parent_id→新节点(flow边)，每个dependency→新节点(dependency边)。不需要单独创建边。task_text用动词开头命名。parent_id不填则放根级。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务ID"},
            "parent_id": {"type": "string", "description": "父节点ID（不填则插入到根级）"},
            "task_text": {"type": "string", "description": "节点任务描述"},
            "dependencies": {
                "type": "string",
                "description": "依赖节点ID列表，JSON数组字符串，如 [\"node1\",\"node2\"]",
            },
        },
        "required": ["task_id", "task_text"],
    },
)
def insert_dag_node(task_id: str, task_text: str, parent_id: str = "",
                  dependencies: str = "[]"):
    """Insert a new node into the task DAG graph."""
    try:
        dag = DAG(task_id)
        deps = json.loads(dependencies) if isinstance(dependencies, str) else dependencies
        pid = parent_id if parent_id else None
        node = dag.insert_node(parent_id=pid, task_text=task_text, dependencies=deps)
        _save_dag_if_bound(dag)
        return {
            "node": {"id": node["id"], "task": node["task"][:60], "status": node["status"],
                     "parent_id": node.get("parent_id"), "dependencies": deps},
            "info": f"新节点「{task_text[:40]}」已插入DAG。",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="remove_dag_node",
    description="从DAG中删除一个节点。场景：步骤不需要了、计划有误。子节点自动重连到父节点，图不断裂。reroute=false 则子节点变孤儿（慎用）。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务ID"},
            "node_id": {"type": "string", "description": "要移除的节点ID"},
            "reroute": {
                "type": "boolean",
                "description": "是否将子节点重连到父节点（默认true，false则断开子节点）",
            },
        },
        "required": ["task_id", "node_id"],
    },
)
def remove_dag_node(task_id: str, node_id: str, reroute: bool = True):
    """Remove a node from the DAG. Children get rerouted to parent."""
    try:
        dag = DAG(task_id)
        result = dag.remove_node(node_id, reroute=reroute)
        if "error" in result:
            return result
        tree = dag.get_tree()
        _save_dag_if_bound(dag)
        return {
            "removed": result,
            "updated_tree": tree,
            "info": f"节点已移除，子节点{'已重连到父节点' if reroute else '已断开'}。使用 get_task_dag() 查看新DAG。",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="update_node_deps",
    description="修改节点的依赖列表。场景：发现依赖关系设错了。规则：只有数据依赖才设（B需要A的输出），无依赖就空数组让它并行。禁止环形依赖。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务ID"},
            "node_id": {"type": "string", "description": "要修改的节点ID"},
            "dependencies": {
                "type": "string",
                "description": "新的依赖节点ID列表，JSON数组，如 [\"node_a\",\"node_b\"]",
            },
        },
        "required": ["task_id", "node_id", "dependencies"],
    },
)
def update_node_deps(task_id: str, node_id: str, dependencies: str):
    """Change a node's dependency list mid-execution."""
    try:
        dag = DAG(task_id)
        deps = json.loads(dependencies) if isinstance(dependencies, str) else dependencies
        updated = dag.update_dependencies(node_id, deps)
        if "error" in updated:
            return updated
        _save_dag_if_bound(dag)
        return {
            "node": {"id": updated["id"], "task": updated["task"][:60], "dependencies": deps},
            "info": f"节点依赖已更新。现在有 {len(deps)} 个前置依赖。",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="get_execution_trace",
    description="获取完整的任务执行轨迹（⑥检查任务用）。包含每个节点的执行状态、耗时、结果预览。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务ID"},
        },
        "required": ["task_id"],
    },
)
def get_execution_trace(task_id: str):
    """Get full execution trace for review/rework."""
    try:
        dag = DAG(task_id)
        trace = dag.get_execution_trace()
        return {
            "trace": trace,
            "total_nodes": len(trace),
            "done": sum(1 for t in trace if t["status"] == "done"),
            "failed": sum(1 for t in trace if t["status"] == "failed"),
            "blocked": sum(1 for t in trace if t["status"] == "blocked"),
            "pending": sum(1 for t in trace if t["status"] == "pending"),
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="rework_subtree",
    description="对子图进行返工（⑤→②返工回路）。标记指定节点为pending，清空关联问题，允许重跑。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务ID"},
            "node_ids": {
                "type": "string",
                "description": "要返工的节点ID列表，JSON数组，如 [\"n1\",\"n2\"]",
            },
        },
        "required": ["task_id", "node_ids"],
    },
)
def rework_subtree(task_id: str, node_ids: str):
    """Mark nodes as pending again for rework. Clears their results and resets work memory entries."""
    try:
        dag = DAG(task_id)
        wm = WorkMemory(task_id)
        ids = json.loads(node_ids) if isinstance(node_ids, str) else node_ids
        reset = []
        errors = []
        for nid in ids:
            node = dag.get_node(nid)
            if not node:
                errors.append(f"node {nid} not found")
                continue
            try:
                dag.set_status(nid, "pending", result=None)
                # Clear work memory entries for this node
                entries = wm.get_node_entries(nid)
                for e in entries:
                    wm.close_entry(e["id"])
                reset.append(nid)
            except ValueError as ve:
                errors.append(f"node {nid}: {ve}")
        info = f"返工 {len(reset)} 个节点"
        if errors:
            info += f"，{len(errors)} 个失败: {'; '.join(errors[:3])}"
        return {
            "reset_nodes": reset,
            "errors": errors,
            "info": info,
        }
    except Exception as e:
        return {"error": str(e)}


# ── 模板工具（⑥→① 模板复用） ─────────────────────

@register_tool(
    name="save_task_template",
    description="将当前任务的DAG结构保存为模板，存入长时记忆。下次类似任务可以load_task_template复用。",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务ID"},
            "template_name": {"type": "string", "description": "模板名称（方便查找）"},
        },
        "required": ["task_id", "template_name"],
    },
)
def save_task_template(task_id: str, template_name: str):
    """Save the task DAG as a reusable template in L3 memory."""
    try:
        dag = DAG(task_id)
        task = dag.get_task()
        tree = dag.get_tree()
        store = _get_memory_store()
        if not store:
            return {"error": "memory store not available"}

        template_data = json.dumps({
            "type": "task_template",
            "template_name": template_name,
            "original_request": task.get("user_request", ""),
            "dag": tree,
        }, ensure_ascii=False)

        fid = store.add(
            template_data,
            ts=time.strftime("%Y%m%d%H%M%S"),
            source="task_template",
            topic_id=task_id,
            tags=f"task_template,{template_name}",
            importance=8.0,
            epistemic="experience",
        )
        return {
            "fragment_id": fid,
            "template_name": template_name,
            "node_count": len(dag.get_nodes()),
            "info": f"模板「{template_name}」已保存到长时记忆。使用 load_task_template() 复用。",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="load_task_template",
    description="从长时记忆中加载任务模板，用于创建相似结构的新任务（⑥→①模板复用）。",
    parameters={
        "type": "object",
        "properties": {
            "template_name": {"type": "string", "description": "模板名称关键词"},
            "new_request": {"type": "string", "description": "新任务的请求描述（可选，留空则使用原请求）"},
        },
        "required": ["template_name"],
    },
)
def load_task_template(template_name: str, new_request: str = ""):
    """Load a task template from L3 and create a new task from it."""
    try:
        store = _get_memory_store()
        if not store:
            return {"error": "memory store not available"}

        # Search for templates by name
        templates = store.recall(
            template_name,
            top_k=5,
            threshold=0.3,
            layer="core",
        )
        if not templates:
            # Try archive
            templates = store.recall_archive(template_name, top_k=5, threshold=0.3) or []

        template_frag = None
        for t in templates:
            try:
                data = json.loads(t["text"])
                if isinstance(data, dict) and data.get("type") == "task_template":
                    tname = data.get("template_name", "")
                    if template_name.lower() in tname.lower():
                        template_frag = data
                        break
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        if not template_frag:
            return {"error": f"未找到模板「{template_name}」", "searched": len(templates)}

        # Create new task from template
        request = new_request or template_frag.get("original_request", "")
        task_id = uuid.uuid4().hex[:12]
        dag = DAG(task_id)
        wm = WorkMemory(task_id)
        attention = AttentionFocus()
        executor = TaskExecutor(dag, wm, attention, store)

        task = dag.create_task(request)
        template_dag = template_frag.get("dag", {})

        # Rebuild DAG from template structure
        def _rebuild(template_node, parent_id=None):
            node = dag.create_node(
                template_node.get("task", ""),
                parent_id=parent_id,
                dependencies=template_node.get("dependencies", []),
            )
            for child in template_node.get("children", []):
                _rebuild(child, node["id"])
            return node

        root = _rebuild(template_dag)
        dag.set_root_node(root["id"])
        dag.update_task_status("running")

        nodes = dag.get_nodes()
        return {
            "task_id": task_id,
            "request": request,
            "template_source": template_frag.get("template_name", template_name),
            "node_count": len(nodes),
            "info": f"已从模板「{template_frag.get('template_name', template_name)}」创建新任务，共 {len(nodes)} 个节点。",
        }
    except Exception as e:
        return {"error": str(e)}


# ── 六步自执行循环 ────────────────────────────────

@register_tool(
    name="run_task",
    description="全自动执行任务：六步法（理解→计划→拆解→执行→检查→沉淀）。自动规划DAG，遍历节点执行，往返求解，最后沉淀记忆和模板。",
    parameters={
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "任务需求描述"},
            "template_name": {
                "type": "string",
                "description": "可选：从已有模板创建，填写模板名称关键词",
            },
            "max_steps": {
                "type": "integer",
                "description": "最大执行步数（默认50）",
            },
        },
        "required": ["request"],
    },
)
def run_task(request: str, template_name: str = "", max_steps: int = 50):
    """Full autopilot: 6-step execution cycle.

    ① 理解需求 → ② 制定计划 → ③ 拆解任务 → ④ 执行任务 → ⑤ 检查任务 → ⑥ 沉淀记忆
    """
    try:
        task_id = uuid.uuid4().hex[:12]
        dag = DAG(task_id)
        wm = WorkMemory(task_id)
        attention = AttentionFocus()
        store = _get_memory_store()
        executor = TaskExecutor(dag, wm, attention, store)

        # ①+② Try template first (⑥→① reuse), fallback to coarse plan
        if template_name and store:
            # Load template
            template_task = load_task_template(template_name, new_request=request)
            if "error" not in template_task:
                # Template loaded — use its task_id instead
                loaded_task_id = template_task.get("task_id", "")
                if loaded_task_id:
                    dag = DAG(loaded_task_id)
                    wm = WorkMemory(loaded_task_id)
                    executor = TaskExecutor(dag, wm, AttentionFocus(), store)
                    return {
                        "status": "template_loaded",
                        "task_id": loaded_task_id,
                        "node_count": template_task.get("node_count", 0),
                        "info": f"从模板创建了任务，共 {template_task.get('node_count', 0)} 个节点。"
                                f"执行器支持六步循环：执行节点→记录问题→往返求解→沉淀记忆。"
                                f"可手动使用 start_node / complete_node / insert_dag_node / remove_dag_node 操作。"
                                f"需要全自动执行请设置 max_steps>0。",
                    }

        # Run the 6-step loop (needs llm_prompt_fn — will use inline LLM call)
        # 如果没有 llm_prompt_fn, run() 会运行但只记录痕迹
        result = executor.run(request, llm_prompt_fn=None, max_steps=max_steps)

        return {
            "status": result["status"],
            "task_id": result["task_id"],
            "steps_executed": result["steps"],
            "task_info": dag.get_task(),
            "tree": dag.get_tree(),
            "trace": result["trace"][-10:],  # last 10 steps
            "sediment": result.get("sediment", {}),
            "info": (
                f"六步循环完成：执行 {result['steps']} 步，最终状态 {result['status']}。"
                f"已沉淀到长时记忆（含DAG模板）。"
                f"可用 get_task_dag('{result['task_id']}') 查看完整DAG，"
                f"用 get_execution_trace('{result['task_id']}') 查看执行轨迹。"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


# ── 思维链工具 ──

@register_tool(
    name="append_thought_step",
    description="在思维链中追加一个执行步。只追加不修改，记录做了什么、为什么做、结果如何。每完成一个关键动作后调用。",
    parameters={
        "type": "object",
        "properties": {
            "action_type": {
                "type": "string",
                "description": "动作类型：tool_call(工具调用) | decision(决策) | reflection(反思) | fix(修复) | plan(规划) | checkpoint(检查点)",
            },
            "summary": {
                "type": "string",
                "description": "一行摘要，热层显示。如'读取了main.py发现端口冲突'",
            },
            "motivation": {
                "type": "string",
                "description": "为什么做这一步——因果链上下文。如'上一步报错提示端口已被占用'",
            },
            "result": {
                "type": "string",
                "description": "结果简述。如'找到了冲突端口8080'",
            },
            "next_suggestion": {
                "type": "string",
                "description": "下一步建议，可为空。如'修改端口并重启服务'",
            },
            "detail": {
                "type": "string",
                "description": "详细内容，温层翻阅用。可选",
            },
        },
        "required": ["action_type", "summary", "motivation", "result"],
    },
)
def append_thought_step(action_type: str, summary: str, motivation: str, result: str, next_suggestion: str = "", detail: str = ""):
    """追加一个思维步到当前任务的思维链中。"""
    try:
        topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有活跃任务，无法追加思维步"}
        
        tc = ThoughtChain(topic_id)
        step = tc.append(
            action_type=action_type,
            summary=summary,
            motivation=motivation,
            result=result,
            next_suggestion=next_suggestion,
            detail=detail,
        )
        hot_layer = tc.format_hot_layer(n=5)
        return {
            "ok": True,
            "step_id": step.step_id,
            "step_count": tc.step_count,
            "hot_layer": hot_layer,
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="get_thought_chain",
    description="获取当前任务的思维链。返回热层（最近N步）和完整链概览。切任务时自动装载对应链。",
    parameters={
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "热层显示步数，默认5",
            },
        },
        "required": [],
    },
)
def get_thought_chain(n: int = 5):
    """获取当前任务思维链的热层视图。"""
    try:
        topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有活跃任务"}
        
        tc = ThoughtChain(topic_id)
        hot_layer = tc.format_hot_layer(n=n)
        last = tc.last_step
        return {
            "ok": True,
            "topic_id": topic_id,
            "step_count": tc.step_count,
            "hot_layer": hot_layer,
            "last_step": {
                "step_id": last.step_id,
                "action_type": last.action_type,
                "summary": last.summary,
                "next_suggestion": last.next_suggestion,
            } if last else None,
        }
    except Exception as e:
        return {"error": str(e)}
