#!/usr/bin/env python3
"""
Task executor — the main execution loop that orchestrates the v4.0 system.

Core execution cycle:

  1. Plan → coarse plan → build initial DAG
  2. Loop:
     a. Load attention (slide_in → assemble 5 blocks)
     b. Execute node (model processes → produce result + questions + attention_advice)
     c. Record questions/blockers/uncertains to work memory
     d. Check roundtrip triggers
     e. If trigger → roundtrip_solve (blockers serial, questions batch)
     f. Slide out → save state → clear attention
     g. Model decides next node (or executor overrides)
  3. All nodes done → reflect_and_sediment → clear work memory

Key design: "I" is the model + v3.0 memory. The executor is not an agent,
just a mechanism that slides attention and feeds the 5-block context.
"""
import json
import threading
import time
import traceback


ROUNDTRIP_QUESTION_THRESHOLD = 5
UNCERTAIN_TIMEOUT_SEC = 30


class TaskExecutor:
    """Orchestrates task execution with sliding attention and roundtrip solving.

    The executor is NOT an agent — it is a mechanism that:
    - Loads task info into the chat context (5-block window)
    - Detects roundtrip triggers and calls the solver
    - Manages the DAG lifecycle
    - The model (in the chat loop) does the actual work
    """

    def __init__(self, dag, wm, attention, memory_store=None):
        self.dag = dag
        self.wm = wm
        self.attention = attention
        self.memory_store = memory_store
        self._lock = threading.Lock()
        self.current_node_id = None
        self._roundtrip_pending = False

    # ── Lifecycle ──────────────────────────────────

    def init_task(self, user_request, llm_prompt_fn=None):
        """Phase 1: Plan and build initial DAG from user request.
        AI outputs {nodes, edges} — code validates and feeds errors back to AI.
        If AI can't fix after retries, code auto-fixes as fallback."""
        from .planner import coarse_plan_with_feedback

        task = self.dag.create_task(user_request)
        plan = coarse_plan_with_feedback(user_request, llm_prompt_fn, max_retries=3)

        plan_nodes = plan.get("nodes", [])
        plan_edges = plan.get("edges", [])
        plan_errors = plan.get("errors", [])

        # Create root node
        root_task = f"整体任务: {user_request[:200]}"
        root = self.dag.create_node(root_task, parent_id=None)
        self.dag.set_root_node(root["id"])

        # Create planned nodes as children of root
        created_ids = {}
        node_order = []
        for pn in plan_nodes:
            node = self.dag.create_node(
                pn["task"],
                parent_id=root["id"],
                dependencies=pn.get("dependencies", []),
            )
            created_ids[pn["id"]] = node["id"]
            node_order.append(node["id"])

        # 如果 AI 重试后仍有错误，自动修复兜底
        if plan_errors:
            validated_edges = self._auto_fix_edges(
                plan_edges, created_ids, root["id"], node_order
            )
        else:
            # AI 输出无误，直接映射 ID 使用
            validated_edges = []
            seen = set()
            for e in plan_edges:
                src = created_ids.get(e.get("source", ""))
                tgt = created_ids.get(e.get("target", ""))
                if not src or not tgt or src == tgt:
                    continue
                key = f"{src}|{tgt}"
                if key in seen:
                    continue
                seen.add(key)
                validated_edges.append({
                    "source": src, "target": tgt,
                    "edge_type": e.get("edge_type", "flow"),
                })

        for ve in validated_edges:
            self.dag.create_edge(ve["source"], ve["target"], ve.get("edge_type", "flow"))

        self.dag.update_task_status("running")
        return self.dag.get_task()

    @staticmethod
    def validate_edges(raw_edges, valid_node_ids, node_order, lenient=False):
        """纯校验：检查 AI 输出的 edges 是否合法，返回错误列表（不修复）。

        检查项：
        - 无效引用：source/target 不在 nodes 列表中
        - 自环：source == target
        - 重复边：同 source→target 出现多次
        - 缺失入边：节点没有任何入边（孤立节点）  [lenient 模式跳过]
        - 星暴检测：单个 source 连接过多 target（>3 个）  [lenient 模式跳过]

        lenient=True 用于用户手动编辑流程图时，仅校验引用/自环/重复，
        不限制孤立节点和星暴（用户可能有意为之）。
        """
        errors = []
        if not raw_edges:
            if node_order and not lenient:
                errors.append("edges 为空——所有 {} 个节点都没有连线".format(len(node_order)))
            return errors

        seen_pairs = {}
        incoming_count = {}
        outgoing_count = {}

        for i, e in enumerate(raw_edges):
            src = e.get("source", "")
            tgt = e.get("target", "")
            etype = e.get("edge_type", e.get("edgeType", "flow"))

            # 检查 source/target 存在
            if src not in valid_node_ids:
                errors.append("edge[{}]: source '{}' 不在 nodes 列表中".format(i, src))
            if tgt not in valid_node_ids:
                errors.append("edge[{}]: target '{}' 不在 nodes 列表中".format(i, tgt))

            # 检查自环
            if src == tgt and src:
                errors.append("edge[{}]: 自环（source == target == '{}'）".format(i, src))

            # 统计重复
            pair = f"{src}|{tgt}"
            if pair in seen_pairs:
                errors.append("edge[{}]: 重复边（{} → {} 已在第 {} 条出现过）".format(
                    i, src, tgt, seen_pairs[pair]))
            else:
                seen_pairs[pair] = i

            # 统计入边/出边
            incoming_count[tgt] = incoming_count.get(tgt, 0) + 1
            outgoing_count[src] = outgoing_count.get(src, 0) + 1

        if not lenient:
            # 检查孤立节点（既无入边也无出边 = 真正断开的节点）
            # 注意：入口节点（start node）有出边但无入边，是合法的 DAG 结构，不算孤立
            for nid in node_order:
                has_in = nid in incoming_count
                has_out = nid in outgoing_count
                if not has_in and not has_out:
                    errors.append("节点 '{}' 既无入边也无出边（真正孤立）".format(nid))

            # 检查星暴（单个 source 连接 >3 个 target）
            for src, count in outgoing_count.items():
                if count > 3:
                    errors.append("节点 '{}' 的出边过多（{} 条），疑似星暴——应改为链式或分支".format(src, count))

        return errors

    @staticmethod
    def _auto_fix_edges(raw_edges, id_map, root_id, node_order):
        """自动修复兜底：当 AI 重试后仍有错误时使用。"""
        edge_map = {}
        for e in raw_edges:
            src_ai = e.get("source", "")
            tgt_ai = e.get("target", "")
            src = id_map.get(src_ai)
            tgt = id_map.get(tgt_ai)
            if not src or not tgt or src == tgt:
                continue
            key = f"{src}|{tgt}"
            etype = e.get("edge_type", "flow")
            if key not in edge_map:
                edge_map[key] = {"source": src, "target": tgt, "edge_type": etype}
            else:
                if etype == "flow" and edge_map[key]["edge_type"] != "flow":
                    edge_map[key]["edge_type"] = "flow"

        nodes_with_incoming = {e["target"] for e in edge_map.values()}
        orphan_nodes = [nid for nid in node_order if nid not in nodes_with_incoming]

        prev_id = root_id
        for nid in orphan_nodes:
            key = f"{prev_id}|{nid}"
            if key not in edge_map:
                edge_map[key] = {"source": prev_id, "target": nid, "edge_type": "flow"}
            prev_id = nid

        return list(edge_map.values())

    def get_next_node(self):
        """Find the next runnable node. Uses get_runnable_nodes().

        Returns: node dict or None if no runnable nodes.
        """
        runnable = self.dag.get_runnable_nodes()
        if not runnable:
            return None
        # Pick first runnable (model attention_advice may override)
        return runnable[0]

    def slide_to_node(self, node_id):
        """Slide attention from current to target node.

        Returns: assembled attention context text.
        """
        from .attention_focus import slide_out, slide_in

        # Slide out current
        if self.current_node_id and self.attention.current_node_id:
            slide_out(self.dag, self.wm, self.current_node_id, self.attention)

        # Slide in new
        slide_in(self.dag, self.wm, node_id, self.memory_store, self.attention)
        self.current_node_id = node_id

        # Mark node running
        self.dag.set_status(node_id, "running")
        return self.attention.get_all_blocks_text()

    def record_issues(self, node_id, issues):
        """Record questions/blockers from model output.

        Args:
            node_id: str
            issues: list of {"type": "question"|"blocker"|"uncertain", "text": str}
        """
        for issue in issues:
            etype = issue.get("type", "question")
            text = issue.get("text", "")
            if etype == "blocker":
                confidence = 0.9
                self.dag.set_status(node_id, "blocked")
            elif etype == "uncertain":
                confidence = 0.5
            else:
                confidence = 0.8
            self.wm.add_entry(node_id, etype, text, confidence=confidence)

    def check_roundtrip(self):
        """Check if roundtrip should be triggered. Returns True if triggered."""
        from .work_memory import roundtrip_solve

        q_count = self.wm.count_questions()
        has_blocker = self.wm.has_blocker()
        no_progress = self.dag.get_runnable_nodes() is None or len(self.dag.get_runnable_nodes()) == 0

        if has_blocker or q_count >= ROUNDTRIP_QUESTION_THRESHOLD or no_progress:
            self._roundtrip_pending = True
            results = roundtrip_solve(self.wm)
            self._roundtrip_pending = False
            # If blockers were resolved, unblock relevant nodes
            if has_blocker:
                for blocker in self.wm.get_blockers():
                    if blocker["status"] == "solved":
                        # Try to unblock the blocked node
                        node_id = blocker["node_id"]
                        node = self.dag.get_node(node_id)
                        if node and node["status"] == "blocked":
                            self.dag.set_status(node_id, "running")
            return True
        return False

    def _check_uncertain_timeout(self):
        """Promote long-standing uncertains to blockers."""
        uncertains = self.wm.get_uncertains()
        now = time.time()
        for u in uncertains:
            age = now - u["created_at"]
            if age > UNCERTAIN_TIMEOUT_SEC:
                self.wm.escalate_entry(u["id"])

    def finish_task(self):
        """Mark task as done. Returns final status."""
        self.dag.update_task_status("done", finished_at=time.time())

    def fail_task(self, reason=""):
        """Mark task as failed."""
        self.dag.update_task_status("failed", finished_at=time.time())

    def get_task_context_for_model(self):
        """Build the full context for the model: 5-block window + DAG overview.

        Returns: dict with context blocks and node state.
        """
        context = {
            "current_node": self.current_node_id,
            "attention_blocks": self.attention.get_all_blocks_text(),
            "task_status": self.dag.get_task(),
        }
        return context

    # ── Parser: extract model's node output ────────

    def parse_model_output(self, text):
        """Parse model output for structured node result + attention advice.

        Expected format (YAML-like):
          node_output:
            result: "..."
            questions:
              - type: question
                text: "..."
            attention_advice:
              next_node: "node_id"
            trigger_roundtrip: true/false

        Returns: dict with parsed fields, or minimal default.
        """
        result = {
            "result": text,
            "questions": [],
            "attention_advice": {"next_node": None, "reason": ""},
            "trigger_roundtrip": False,
        }

        # Try to find node_output block
        import re
        match = re.search(r'node_output:\s*', text)
        if not match:
            return result

        block = text[match.end():]
        lines = block.split("\n")
        current_key = None
        in_questions = False
        current_question = {}

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("result:"):
                result["result"] = stripped[len("result:"):].strip().strip('"').strip("'")
                current_key = "result"
                in_questions = False
            elif stripped.startswith("trigger_roundtrip:"):
                val = stripped[len("trigger_roundtrip:"):].strip().lower()
                result["trigger_roundtrip"] = val in ("true", "yes", "1")
            elif stripped.startswith("questions:"):
                in_questions = True
            elif stripped.startswith("attention_advice:"):
                in_questions = False
            elif stripped.startswith("next_node:"):
                result["attention_advice"]["next_node"] = stripped[len("next_node:"):].strip().strip('"').strip("'")
            elif stripped.startswith("reason:"):
                result["attention_advice"]["reason"] = stripped[len("reason:"):].strip().strip('"').strip("'")
            elif in_questions and stripped.startswith("-"):
                if current_question:
                    result["questions"].append(current_question)
                current_question = {}
                parts = stripped.lstrip("- ").split(":", 1)
                if len(parts) == 2:
                    current_question[parts[0].strip()] = parts[1].strip().strip('"').strip("'")
            elif in_questions and current_question and ":" in stripped:
                k, v = stripped.split(":", 1)
                current_question[k.strip()] = v.strip().strip('"').strip("'")

        if current_question:
            result["questions"].append(current_question)

        return result

    def extract_sub_tasks(self, text):
        """Extract sub-task descriptions from model output during node execution.

        Looks for 'dynamic_split:' section in model output.

        Returns: list of task description strings.
        """
        import re
        match = re.search(r'dynamic_split:\s*\n', text)
        if not match:
            return []
        block = text[match.end():]
        tasks = []
        for line in block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("-"):
                tasks.append(stripped.lstrip("- ").strip())
            elif stripped.startswith("sub_tasks:") or stripped.startswith("children:"):
                continue
            elif tasks and not stripped:
                break
        return tasks[:5]

    # ── Document-mandated methods ─────────────────

    def no_runnable_nodes(self) -> bool:
        """Check if there are any runnable nodes remaining."""
        runnable = self.dag.get_runnable_nodes()
        return not runnable

    def is_complete(self) -> bool:
        """Check if the task is complete (all nodes done/failed/split)."""
        all_nodes = self.dag.get_nodes()
        if not all_nodes:
            return False
        from .dag import NodeStatus
        return all(n["status"] in NodeStatus.FINAL for n in all_nodes)

    def set_memory_store(self, memory_store):
        """Set or update the memory store reference."""
        self.memory_store = memory_store

    # ── 六步主循环 ─────────────────────────────────

    def run(self, user_request, llm_prompt_fn=None, max_steps=50):
        """Full 6-step execution loop (autopilot mode).

        Steps: ①理解需求→②制定计划→③拆解任务→④完成任务→⑤检查任务→⑥沉淀记忆

        The executor drives the loop directly, calling llm_prompt_fn for each
        node's execution. Returns when all nodes are done/failed or max_steps reached.

        Args:
            user_request: User's task request string.
            llm_prompt_fn: Callable(system_prompt, user_prompt) → LLM output text.
            max_steps: Maximum number of node iterations before forced finish.

        Returns: dict with final status, trace, and sediment result.
        """
        # ①② 理解需求 + 制定计划
        task = self.init_task(user_request, llm_prompt_fn)
        trace = []
        step = 0

        while not self.is_complete() and step < max_steps:
            step += 1
            self._check_uncertain_timeout()

            # ③ 拆解: 检查是否有failed节点需要反思改图
            if self.dag.has_failed_nodes():
                failed = self.dag.get_failed_nodes()
                # Non-removed failed nodes → try rework prompt
                for fn in failed:
                    removal = fn.get("result", "")
                    if isinstance(removal, str):
                        try:
                            removal = json.loads(removal)
                        except (json.JSONDecodeError, TypeError):
                            removal = {}
                    if removal.get("removed"):
                        continue  # was a removal marker, skip
                    # v4.0: retry_count >= 3 不再重试，保持 failed
                    if (fn.get("retry_count") or 0) >= 3:
                        continue
                    if llm_prompt_fn:
                        rework = llm_prompt_fn(
                            "你是一个任务修复专家。节点执行失败，你需要提出修复方案。"
                            "输出：1) 是否改依赖 2) 是否拆分节点 3) 具体修复描述。只输出JSON。",
                            f"失败节点: {fn['task']}\n结果: {fn.get('result', '')}\n修复方案:"
                        )
                        trace.append({"step": step, "action": "rework", "node": fn["id"], "output": rework})
                    # Reset to pending for retry
                    try:
                        self.dag.set_status(fn["id"], "pending")
                    except ValueError:
                        pass  # can't transition from failed, skip

            # ④ 执行
            node = self.get_next_node()
            if node is None:
                # ⑤ 检查: 无可推进节点 → 触发往返求解
                if self.check_roundtrip():
                    trace.append({"step": step, "action": "roundtrip", "detail": "no runnable nodes"})
                    continue
                else:
                    # Still blocked → break
                    break

            # 加载注意力焦点
            context = self.slide_to_node(node["id"])

            # 调 LLM 执行节点
            if llm_prompt_fn:
                system_prompt = (
                    "你正在执行一个任务的子步骤。以下是当前上下文。\n"
                    "执行完毕后输出 node_output:\n"
                    "  result: 执行结果\n"
                    "  questions: [{type, text}, ...]\n"
                    "  attention_advice: {next_node, reason}\n"
                    "  trigger_roundtrip: true/false\n"
                    "如果需要拆分节点，输出 dynamic_split:\n"
                    "  - 子任务1\n"
                    "  - 子任务2"
                )
                output = llm_prompt_fn(system_prompt, f"## 当前节点\n{node['task']}\n## 注意力上下文\n{context}")
            else:
                output = f"[executor] executed node: {node['task']}"

            # 解析输出
            parsed = self.parse_model_output(output)

            # 记录问题
            self.record_issues(node["id"], parsed.get("questions", []))

            # 检查动态拆分
            sub_tasks = self.extract_sub_tasks(output)
            if sub_tasks:
                try:
                    self.dag.set_status(node["id"], "split")
                except ValueError:
                    pass
                from .planner import dynamic_split
                split_result = dynamic_split(node, sub_tasks, llm_prompt_fn)
                for child_node in split_result.get("nodes", []):
                    self.dag.insert_node(parent_id=node["id"], task_text=child_node["task"])
                for edge in split_result.get("edges", []):
                    self.dag.create_edge(edge["source"], edge["target"], edge.get("edge_type", "flow"))
                trace.append({"step": step, "action": "split", "node": node["id"], "children": len(sub_tasks)})

            # 更新节点状态
            try:
                self.dag.set_status(node["id"], "done", result=parsed.get("result", output))
            except ValueError:
                pass

            # ⑤ 检查: 触发往返求解
            if parsed.get("trigger_roundtrip") or self.check_roundtrip():
                trace.append({"step": step, "action": "roundtrip", "detail": "post-execution"})

            # 滑出注意力
            self.attention.clear()
            self.current_node_id = None

            trace.append({
                "step": step,
                "node": node["id"],
                "task": node["task"][:60],
                "status": "done",
                "questions": len(parsed.get("questions", [])),
            })

        # ⑥ 沉淀记忆
        final_status = "done" if self.is_complete() else "partial"
        if final_status == "done":
            self.dag.update_task_status("done", finished_at=time.time())
        else:
            self.dag.update_task_status("blocked", finished_at=time.time())

        # 沉淀
        sedi = {}
        if self.memory_store:
            try:
                from .reflection import reflect_and_sediment
                sedi = reflect_and_sediment(self)
            except Exception:
                pass
            # 同时沉淀 DAG 作为模板（供 ⑥→① 复用）
            try:
                dag_template = self.dag.get_tree()
                self.memory_store.add(
                    json.dumps({"type": "task_template", "request": user_request, "dag": dag_template},
                               ensure_ascii=False),
                    ts=time.strftime("%Y%m%d%H%M%S"),
                    source="task_template",
                    topic_id=self.dag.task_id,
                    tags="task_template,finished",
                    importance=7.0,
                    epistemic="experience",
                )
            except Exception:
                pass

        self.wm.clear()

        return {
            "status": final_status,
            "task_id": self.dag.task_id,
            "steps": step,
            "trace": trace,
            "sediment": sedi,
        }
