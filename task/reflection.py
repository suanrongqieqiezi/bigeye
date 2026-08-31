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

import os

import re

import sys

# 反思产物双路沉淀：高价值行为规则 → 重要事项；可复用方法 → 技能书
_LESSONS_PROMPT = """你是大眼的反思助手。基于下面的任务记录，提炼两类可沉淀的产物。

【判断标准】
1. <<<重要事项>>>：只提炼"高频行为规则/判断修正"——下次遇到类似情况应该怎么做、
   用户偏好的做事方式、容易踩的坑。必须是能反复约束后续行为的一句话规则。
   任务本身的描述、普通项目进度、一次性结论不算。
2. <<<技能>>>：只提炼"可复用的方法流程"——这次任务里摸索出的标准做法、调试套路、
   操作流程，下次做同类任务可以直接照着做。零散的步骤说明不算。
   倒推判断法：先想下次遇到什么场景会需要它；说不清触发场景的技能不值得沉淀。

【输出格式】严格按下面格式，无则写 NONE，不要额外内容：
<<<重要事项>>>
（一句话规则，≤50字）

<<<技能>>>
技能名:（≤12字）
目的:（一句话）
触发场景:（1-3个"何时想起它"，分号分隔，如：打包后路径报错；exe找不到文件）
内容:（3-8条要点，每条一行，具体可操作）

任务记录：
{task_brief}
"""

# 重要事项数量上限（每次对话全量注入，不能无限膨胀）
_MAX_MATTERS = 15

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

    # 默认 LLM：调用方没传 llm_prompt_fn 时，用 memory/reflection._llm_prompt 兜底，
    # 否则三问压缩/叙事生成永远走回退模板（历史 bug：server 调此函数从未传过 fn）
    if llm_prompt_fn is None:
        try:
            from memory.reflection import _llm_prompt
            llm_prompt_fn = lambda s, u: _llm_prompt(s, u) or ""
        except Exception:
            pass

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

    # 6. Behavior lessons → important matters + skills (双路沉淀)
    try:
        lessons = _sediment_behavior_lessons(task, dag, llm_prompt_fn)
        results["matter"] = lessons.get("matter", "skipped")
        results["skill"] = lessons.get("skill", "skipped")
    except Exception:
        results["matter"] = "error"
        results["skill"] = "error"

    # 7. Clear work memory
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


def _task_brief_for_lessons(task, dag):
    """Build a compact task summary for lesson extraction."""
    nodes = dag.get_nodes()
    lines = [
        f"用户请求: {task.get('user_request', '')[:200]}",
        f"任务状态: {task.get('status', 'unknown')}",
    ]
    done_nodes = [n for n in nodes if n.get("status") == "done"]
    for n in done_nodes[:6]:
        r = n.get("result", "") or ""
        lines.append(f"- {n['task'][:60]}: {r[:120]}")
    return "\n".join(lines)[:2500]


def _call_llm(prompt, user_text, llm_prompt_fn=None):
    """Call LLM: prefer caller-provided fn, fallback to memory/reflection._llm_prompt."""
    if llm_prompt_fn:
        try:
            return llm_prompt_fn(prompt, user_text) or ""
        except Exception:
            pass
    try:
        from memory.reflection import _llm_prompt
        return _llm_prompt(prompt, user_text) or ""
    except Exception:
        return ""


def _extract_section(text, tag):
    """Extract content between <<<tag>>> and next <<< or EOF. Returns stripped or ''."""
    start = text.find(f"<<<{tag}>>>")
    if start < 0:
        return ""
    body = text[start + len(f"<<<{tag}>>>"):]
    nxt = body.find("<<<")
    if nxt >= 0:
        body = body[:nxt]
    return body.strip()


def _sediment_matter(text):
    """Add a behavior rule to important matters with dedup + cap. Returns action str."""
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from tools.important_matters import get_matters, set_matters, _record_lineage

        text = text.strip().strip('"').strip("'")
        if not text or text.upper() == "NONE" or len(text) < 8:
            return "skipped-empty"

        matters = get_matters()
        # 查重：已有语义相似条目（同关键词或长公共子串）则跳过
        import difflib
        for existing in matters:
            existing = str(existing)
            if existing == text:
                return "skipped-duplicate"
            ratio = difflib.SequenceMatcher(None, existing, text).ratio()
            # 关键词包含也算重（短规则常见），长规则用相似度
            if ratio > 0.75 or (len(text) > 12 and text[:12] in existing):
                return "skipped-duplicate"

        if len(matters) >= _MAX_MATTERS:
            return "skipped-cap"
        matters.append(text)
        if not set_matters(matters):
            return "error-save"
        try:
            _record_lineage("add", len(matters), new_content=text)
        except Exception:
            pass
        return "added"
    except Exception:
        import traceback
        traceback.print_exc()
        return "error"


def _sediment_skill(parsed_text, llm_prompt_fn=None):
    """Create or update a skill file from parsed LLM output. Returns action str."""
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from skills_scanner import SKILLS_DIR, scan_skills
        import datetime

        name = purpose = content = ""
        name = purpose = ""
        trigger_text = ""
        # 解析：技能名/目的/触发场景/内容都在冒号后，内容行按行收集
        lines = [l.strip() for l in parsed_text.split("\n") if l.strip()]
        name = ""
        purpose = ""
        trigger_text = ""
        content_lines = []
        mode = None
        for line in lines:
            if line.startswith("技能名:"):
                name = line[len("技能名:"):].strip().strip('"').strip("'")
                mode = "name"
            elif line.startswith("目的:"):
                purpose = line[len("目的:"):].strip()
                mode = "purpose"
            elif line.startswith("触发场景:"):
                trigger_text = line[len("触发场景:"):].strip()
                mode = "trigger"
            elif line.startswith("内容:"):
                mode = "content"
            elif mode == "content" and line and line.upper() != "NONE":
                content_lines.append(line.lstrip("-• "))

        if not name or not content_lines:
            return "skipped-malformed"
        if not purpose:
            purpose = name

        os.makedirs(SKILLS_DIR, exist_ok=True)
        skill_path = os.path.join(SKILLS_DIR, f"{name}.md")
        exists = os.path.isfile(skill_path)

        today = datetime.date.today().isoformat()
        content_block = "\n".join(f"- {c}" for c in content_lines)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

        if not exists:
            # 新建：按规范格式写，扫描器能识别；新技能一律 trial（试用→实测→转正）
            trig_items = [t.strip() for t in re.split(r"[；;]", trigger_text) if t.strip()] if trigger_text else []
            trig_yaml = "[" + ", ".join(f'"{t}"' for t in trig_items) + "]"
            body = (
                f"---\n"
                f'name: "{name}"\n'
                f'description: "{name} — {purpose}"\n'
                f'tags: [反思, 自动生成]\n'
                f'triggers: {trig_yaml}\n'
                f'status: trial\n'
                f"---\n\n"
                f"# {name}\n\n"
                f"> {purpose}\n\n"
                f"## 步骤\n\n{content_block}\n\n"
                f"## 来源\n\n- 任务反思自动沉淀 {ts}（试用期：下次匹配场景实测验证后改 status: active）\n"
            )
            with open(skill_path, "w", encoding="utf-8") as f:
                f.write(body)
            return "created"
        else:
            # 已存在：追加「经验更新」节（不覆盖原内容，保留既有知识）
            with open(skill_path, "r", encoding="utf-8") as f:
                old = f.read()
            update_marker = f"## 经验更新（{today}）"
            if update_marker in old:
                return "skipped-same-day"
            addition = (
                f"\n## 经验更新（{today}）\n\n"
                f"任务反思自动沉淀，补充要点：\n\n{content_block}\n\n"
            )
            # 顺手补缺：存量技能缺 triggers 的补上（不覆盖已有值；status 不动，存量默认 active 不打回试用）
            try:
                if "\ntriggers:" not in old and "\ntriggers:" not in addition:
                    trig_items = [t.strip() for t in re.split(r"[；;]", trigger_text) if t.strip()] if trigger_text else []
                    trig_yaml = "[" + ", ".join(f'"{t}"' for t in trig_items) + "]"
                    m = re.search(r"^description:.*$", old, re.M)
                    if m:
                        old = old[:m.end()] + f"\ntriggers: {trig_yaml}" + old[m.end():]
            except Exception:
                pass
            with open(skill_path, "w", encoding="utf-8") as f:
                f.write(old.rstrip() + "\n" + addition)
            return "updated"
    except Exception:
        import traceback
        traceback.print_exc()
        return "error"


def _sediment_behavior_lessons(task, dag, llm_prompt_fn=None):
    """Task-end: extract behavior rules → important matters, methods → skills.

    双路沉淀：LLM 从任务记录提炼，无新增价值则跳过，不阻塞任务完成。
    Returns dict {"matter": action, "skill": action}.
    """
    result = {"matter": "skipped", "skill": "skipped"}
    try:
        brief = _task_brief_for_lessons(task, dag)
        if not brief:
            return result
        raw = _call_llm(_LESSONS_PROMPT.format(task_brief=brief[:2500]), "", llm_prompt_fn)
        if not raw:
            return result

        matter_text = _extract_section(raw, "重要事项")
        if matter_text and matter_text.upper() != "NONE":
            result["matter"] = _sediment_matter(matter_text)

        skill_text = _extract_section(raw, "技能")
        if skill_text and skill_text.upper() != "NONE":
            result["skill"] = _sediment_skill(skill_text, llm_prompt_fn)
        return result
    except Exception:
        import traceback
        traceback.print_exc()
        return result
