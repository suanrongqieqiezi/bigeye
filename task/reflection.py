#!/usr/bin/env python3
"""
Task-end reflection — sediment task experience into v4.0 L3 memory.

Called when a task finishes (done/failed). Clears work memory afterwards.

Sediments:
  1. Narrative fragment → fragment_store.add (with importance, epistemic, entity_ids)
  2. Entity relations → entity_store.upsert + relation_store.upsert_with_invalidation
  3. Solution knowledge → fragment_store.add (importance scored by LLM)
  4. Daily summary → summary_tree.add_leaf
  5. Clear work memory
"""
import json
import time

SEDIMENT_PROMPT = """写一段第一人称的任务回顾，基于以下任务记录。只记[我]做了什么事。

格式：
<<<碎片记忆>>>
我完成了什么任务、用了什么方法、遇到了什么问题、怎么解决的、学到了什么
<<<碎片记忆>>>

长度：50-200字。用中文。
"""

# CMN P3: 三问压缩提示词，逼出晶体而非泥沙
CRYSTAL_SEDIMENT_PROMPT = """对以下任务记录做三问压缩，形成晶体记忆。答不出就继续提炼直到答出。

任务记录：
{task_brief}

严格按格式输出：
<<<结论>>>
这次任务的核心结论是什么？（一句话，不超过50字）

<<<为什么>>>
为什么是这个结论？关键因果/证据/逻辑链（不超过150字）

<<<下一步>>>
基于这次经验，下次遇到类似情况该怎么做？（不超过80字）

<<<关键实体>>>
逗号分隔的实体列表
"""


def reflect_and_sediment(task_executor, llm_prompt_fn=None, llm_config=None):
    """Task ended (done/failed). Sediment worth-remembering to v4.0 L3 memory.

    Args:
        task_executor: TaskExecutor instance (has dag, wm, memory_store).
        llm_prompt_fn: Optional callable(system, user) → str for narrative gen.
        llm_config: Optional dict for LLM calls (used by extractor).

    Returns: dict with count of sediments.
    """
    dag = task_executor.dag
    wm = task_executor.wm
    store = task_executor.memory_store

    if not store:
        wm.clear()
        return {"narrative": 0, "entities": 0, "solutions": 0, "summary": 0, "error": "no memory store"}

    task = dag.get_task()
    if not task:
        wm.clear()
        return {"error": "no task found"}

    results = {"narrative": 0, "entities": 0, "solutions": 0, "summary": 0, "dag_template": 0}
    ts_str = time.strftime("%Y%m%d%H%M%S")
    now = time.time()

    # 去重：该任务话题消息已被 deep integration 反思过 → 跳过 narrative 生成（内容重复）
    already_reflected = _task_already_deep_reflected(task)
    if already_reflected:
        print(f"[task/reflection] task {task['id']} messages already deep-reflected, skip narrative")

    # Lazy imports for v4.0 stores
    entity_store = None
    relation_store = None
    summary_tree = None

    try:
        from memory.entity_store import EntityStore
        entity_store = EntityStore()
    except Exception:
        pass
    try:
        from memory.relation_store import RelationStore
        relation_store = RelationStore()
    except Exception:
        pass
    try:
        from memory.summary_tree import SummaryTree
        summary_tree = SummaryTree()
    except Exception:
        pass

    # 1. Narrative fragment — CMN P3: 三问压缩格式晶体
    # 已反射过则跳过（narrative 是对话总结，与 deep integration 产出重复）
    narrative_text = "" if already_reflected else _build_narrative(task, dag, llm_prompt_fn)
    if narrative_text:
        try:
            entity_ids = _extract_entity_ids_from_text(
                narrative_text, store, entity_store, llm_config
            )
            # CMN P3: 自传晶体 node_type='self'，authority 由 P4 反思回路提拔
            store.add(
                narrative_text,
                ts=ts_str,
                source="task_narrative",
                topic_id=task["id"],
                tags="task_end,narrative,crystal",
                importance=6.0,  # task narratives are worth remembering
                epistemic="experience",
                entity_ids=entity_ids,
                node_type="self",
                authority_level=0,
                confidence_decay=1.0,
            )
            results["narrative"] = 1
        except Exception:
            pass

    # 2. Entity relations from node results
    nodes = dag.get_nodes(status="done")
    for node in nodes[:5]:
        node_result = node.get("result", "")
        node_task = node.get("task", "")
        combined_text = f"{node_task}: {node_result}"
        if len(combined_text) < 20:
            continue
        try:
            # Extract entities and relations
            entities, relations = _extract_entities_and_relations(
                combined_text, entity_store, relation_store, llm_config
            )
            results["entities"] += entities
            if relations:
                results["entities"] += len(relations)  # count as entity work
        except Exception:
            pass

    # 3. Solution knowledge from work memory solved entries
    solved = _get_solved_entries(wm)
    for entry in solved[:5]:
        if _worth_remembering(entry):
            try:
                text = f"[任务解决方案] {entry['text'][:100]} → {entry.get('answer', '')[:200]}"
                entity_ids = _extract_entity_ids_from_text(
                    text, store, entity_store, llm_config
                )
                store.add(
                    text,
                    ts=ts_str,
                    source="task_solution",
                    topic_id=task["id"],
                    tags="task_solution",
                    importance=5.0,
                    epistemic="experience",
                    entity_ids=entity_ids,
                )
                results["solutions"] += 1
            except Exception:
                pass

    # Also sediment original node results as fragments
    for node in nodes[:3]:
        node_result = node.get("result", "")
        if node_result and len(node_result) > 20:
            try:
                entity_ids = _extract_entity_ids_from_text(
                    node_result[:300], store, entity_store, llm_config
                )
                store.add(
                    f"[节点结果] {node['task'][:80]}: {node_result[:200]}",
                    ts=ts_str,
                    source="task_node_result",
                    topic_id=task["id"],
                    tags="task_result",
                    importance=5.0,
                    epistemic="experience",
                    entity_ids=entity_ids,
                )
                results["solutions"] += 1
            except Exception:
                pass

    # 4b. DAG template (⑥→① 模板复用)
    if store and task.get("status") == "done":
        try:
            tree = dag.get_tree()
            if tree and tree.get("children"):
                request = task.get("user_request", "")
                template_data = json.dumps({
                    "type": "task_template",
                    "template_name": request[:40] if request else "unnamed",
                    "original_request": request,
                    "dag": tree,
                }, ensure_ascii=False)
                store.add(
                    template_data,
                    ts=ts_str,
                    source="task_template",
                    topic_id=task["id"],
                    tags="task_template,auto_generated",
                    importance=7.0,
                    epistemic="experience",
                )
                results["dag_template"] = 1
        except Exception:
            pass

    # 5. Daily summary → summary_tree.add_leaf
    if summary_tree:
        try:
            summary_text = _build_daily_summary(task, dag, llm_prompt_fn)
            if summary_text:
                summary_tree.add_leaf(now, summary_text,
                                      entity_ids=_gather_entity_ids(dag, entity_store))
                results["summary"] = 1
        except Exception:
            pass

    # 6. Clear work memory
    wm.clear()

    return results


def _task_already_deep_reflected(task):
    """Check if this task's topic messages were already processed by deep integration.

    Uses memory/reflection.py checkpoint (topic_id → last processed message id).
    Pure mechanical check, no LLM calls. Returns True only when the checkpoint
    already covers the latest message of the task's topic.
    """
    try:
        from memory.reflection import _load_checkpoint
        cp = _load_checkpoint()
        topic_id = task.get("topic_id")
        if not topic_id or topic_id not in cp:
            return False
        last_processed_id = cp[topic_id]
        from db import get_db
        msgs = get_db().get_messages(topic_id, limit=1)
        if not msgs:
            return True  # 无消息，无内容可沉淀
        return msgs[-1]["id"] <= last_processed_id
    except Exception:
        return False


# ── Helpers ─────────────────────────────────────────

def _extract_entity_ids_from_text(text, store, entity_store, llm_config=None):
    """Extract entity ids from text using extractor, return list of ints."""
    if not entity_store or not text:
        return []
    try:
        from memory.extractor import extract_entities
        ents = extract_entities(text[:1000], llm_config)
        ids = []
        for ent in ents:
            eid = entity_store.get_or_create(
                ent.get("name", ""),
                ent.get("type", "concept"),
                ent.get("aliases"),
            )
            if eid:
                ids.append(eid)
        return ids
    except Exception:
        return []


def _extract_entities_and_relations(text, entity_store, relation_store, llm_config=None):
    """Extract entities and relations from text, persist them. Returns (entity_count, relation_count)."""
    entity_count = 0
    relation_count = 0
    if not entity_store or not text:
        return 0, 0

    try:
        from memory.extractor import extract_entities, extract_relations

        ents = extract_entities(text[:2000], llm_config)
        entity_map = {}  # name → id
        for ent in ents:
            name = ent.get("name", "")
            if not name:
                continue
            eid = entity_store.get_or_create(
                name, ent.get("type", "concept"), ent.get("aliases")
            )
            if eid:
                entity_map[name] = eid
                entity_count += 1

        # Extract relations and persist
        if relation_store and entity_map:
            entity_names = list(entity_map.keys())
            rels = extract_relations(text[:2000], entity_names, llm_config)
            for rel in rels:
                subject_name = rel.get("subject", "")
                subj_id = entity_map.get(subject_name)
                if not subj_id:
                    continue
                obj_name = rel.get("object", "")
                obj_id = entity_map.get(obj_name) if obj_name else None
                edge_type = rel.get("edge_type", "fact")
                confidence = rel.get("confidence", 0.5)
                epistemic = rel.get("epistemic", "experience")

                # Only persist if confidence meets threshold
                min_conf = 0.7 if edge_type == "causal" else 0.5
                if confidence < min_conf:
                    continue

                relation_store.upsert_with_invalidation(
                    subject_id=subj_id,
                    predicate=rel.get("predicate", ""),
                    object_id=obj_id,
                    object_value=rel.get("object_value"),
                    edge_type=edge_type,
                    confidence=confidence,
                )
                relation_count += 1

        return entity_count, relation_count
    except Exception:
        return entity_count, relation_count


def _build_narrative(task, dag, llm_prompt_fn=None):
    """Build first-person narrative of the task.

    CMN P3: LLM 可用时走三问压缩格式（晶体），否则走老碎片记忆格式（泥沙）。
    """
    nodes = dag.get_nodes()
    summary_lines = [
        f"用户请求: {task.get('user_request', '')[:200]}",
        f"完成状态: {task.get('status', 'unknown')}",
    ]
    done_nodes = [n for n in nodes if n.get("status") == "done"]
    if done_nodes:
        summary_lines.append(f"完成节点: {len(done_nodes)}/{len(nodes)}")
    for n in done_nodes[:3]:
        r = n.get("result", "")
        summary_lines.append(f"  - {n['task'][:60]}: {r[:100] if r else '（完成）'}")
    task_brief = "\n".join(summary_lines)

    if llm_prompt_fn:
        try:
            # CMN P3: 优先走三问压缩（晶体格式）
            prompt = CRYSTAL_SEDIMENT_PROMPT.format(task_brief=task_brief[:2000])
            crystal = llm_prompt_fn(prompt, "")
            if crystal and ("<<<结论>>>" in crystal or "<<<" in crystal):
                return crystal.strip()
            # 三问压缩失败，回退到老格式
            narrative = llm_prompt_fn(SEDIMENT_PROMPT, task_brief)
            import re
            fragments = re.findall(r'<<<碎片记忆>>>\s*(.*?)\s*<<<碎片记忆>>>', narrative, re.DOTALL)
            if fragments:
                return fragments[0].strip()
        except Exception:
            pass

    return f"我完成了任务「{task.get('user_request', '')[:50]}」。共处理 {len(nodes)} 个步骤，状态: {task.get('status', 'unknown')}。"


def _build_daily_summary(task, dag, llm_prompt_fn=None):
    """Build a daily summary fragment for summary tree."""
    nodes = dag.get_nodes()
    done_nodes = [n for n in nodes if n.get("status") == "done"]
    parts = []
    for n in done_nodes[:5]:
        r = n.get("result", "")
        if r:
            parts.append(f"{n['task'][:60]}: {r[:100]}")
    if not parts:
        return None
    summary = " | ".join(parts)
    if llm_prompt_fn:
        try:
            compressed = llm_prompt_fn(
                "将以下任务记录压缩为一句话日摘要，保留关键实体：",
                summary[:1000]
            )
            if compressed and len(compressed) > 5:
                return compressed
        except Exception:
            pass
    return f"任务「{task.get('user_request', '')[:30]}」完成。{summary[:200]}"


def _gather_entity_ids(dag, entity_store):
    """Gather entity ids from node text. Fallback if no entity_store."""
    if not entity_store:
        return []
    try:
        nodes = dag.get_nodes()
        all_ids = set()
        for n in nodes[:5]:
            text = (n.get("task", "") + " " + (n.get("result", "") or ""))[:500]
            from memory.extractor import extract_entities
            ents = extract_entities(text)
            for ent in ents:
                eid = entity_store.get_or_create(
                    ent.get("name", ""), ent.get("type", "concept")
                )
                if eid:
                    all_ids.add(eid)
        return list(all_ids)
    except Exception:
        return []


def _get_solved_entries(wm):
    """Get resolved work memory entries."""
    c = wm._conn() if hasattr(wm, '_conn') else None
    if not c:
        return []
    try:
        rows = c.execute(
            "SELECT * FROM work_memory WHERE task_id=? AND status='solved' AND answer IS NOT NULL ORDER BY created_at",
            (wm.task_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def _worth_remembering(entry):
    """Decide if a solved entry is worth sedimenting to long-term memory."""
    answer = entry.get("answer", "") or ""
    text = entry.get("text", "") or ""
    if not answer or not text:
        return False
    return len(answer) > 20 or len(text) > 30
