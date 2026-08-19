#!/usr/bin/env python3
"""
Attention Focus — sliding window, 5 blocks of information.

Cowan's 4±1: human working memory holds ~4 chunks.
v4.0 translates this to 5 structured blocks:

  Block 1: Current node execution context
  Block 2: Current node question queue (≤5 entries)
  Block 3: Parent summary + sibling status
  Block 4: Related entity state (from v3.0 memory)
  Block 5: Last roundtrip result

Slide protocol:
  slide_out(node)  → save state to work memory (L2), clear focus
  slide_in(node)   → load state from work memory/L3, assemble 5 blocks
"""
import json
import time


class AttentionFocus:
    """Sliding window — 5 blocks of structured information.

    Thread-unsafe by design: only one attention at a time.
    """

    MAX_BLOCKS = 5

    def __init__(self):
        self.blocks = {
            "block_1": None,  # node exec context
            "block_2": None,  # node question queue
            "block_3": None,  # parent + sibling status
            "block_4": None,  # entity state from v3.0
            "block_5": None,  # last roundtrip result
        }
        self.current_node_id = None

    def is_empty(self):
        return all(v is None for v in self.blocks.values())

    def set_blocks(self, blocks_dict):
        """Set one or more blocks. blocks_dict keys: block_1..block_5."""
        self.blocks.update(blocks_dict)

    def get_block_text(self, block_key):
        """Get a single block as formatted text, or empty string."""
        val = self.blocks.get(block_key)
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            return json.dumps(val, ensure_ascii=False)
        if isinstance(val, list):
            return "\n".join(str(x) for x in val)
        return str(val)

    def get_all_blocks_text(self):
        """Assemble all 5 blocks into a single context string for prompt injection.

        Returns formatted text ready to inject into system message.
        """
        parts = []
        labels = {
            "block_1": "📋 当前节点上下文",
            "block_2": "❓ 待解决问题",
            "block_3": "📍 任务位置（父节点 + 兄弟状态）",
            "block_4": "🧠 相关实体状态",
            "block_5": "🔄 最近求解结果",
        }
        for key in ("block_1", "block_2", "block_3", "block_4", "block_5"):
            text = self.get_block_text(key)
            if text:
                parts.append(f"【{labels.get(key, key)}】\n{text}")
        if not parts:
            return ""
        return "\n\n" + "\n\n".join(parts)

    def clear(self):
        """Release all 5 blocks (after slide_out)."""
        for k in self.blocks:
            self.blocks[k] = None
        self.current_node_id = None


# ── Slide protocol ────────────────────────────────────

def slide_out(dag, wm, node_id, attention):
    """Slide out current node: save state to work memory, release attention focus.

    Args:
        dag: DAG instance.
        wm: WorkMemory instance.
        node_id: Node id being slid out.
        attention: AttentionFocus instance.

    Returns: summary dict of saved state.
    """
    node = dag.get_node(node_id)
    if not node:
        return {"error": f"node {node_id} not found"}

    # Save node context to dag (persists exec_context)
    state = {
        "exec_context": node.get("exec_context", "{}"),
        "result": node.get("result", ""),
        "status": node.get("status", ""),
    }
    if isinstance(state["exec_context"], str):
        try:
            state["exec_context"] = json.loads(state["exec_context"])
        except (json.JSONDecodeError, TypeError):
            state["exec_context"] = {"saved": node.get("exec_context", "")}

    dag.update_context(node_id, {
        "_last_slid_out": time.time(),
        "_saved_state": True,
    })

    # Save node questions to work memory (if not already there)
    node_entries = wm.get_node_entries(node_id)
    if not node_entries:
        # Fragments still in context — save them as state entry
        wm_entries = []
        for ep in [q.strip() for q in node.get("result", "").split("\n") if q.strip()]:
            if len(ep) > 10:
                wm_entries.append(ep)
        if wm_entries:
            wm.add_entry(node_id, "state",
                         f"节点滑出时状态: {'; '.join(wm_entries[:3])}")

    # Clear attention focus
    attention.clear()
    return {
        "node_id": node_id,
        "saved_state": state,
        "status": node.get("status", ""),
    }


def slide_in(dag, wm, node_id, memory_store, attention):
    """Slide into a new node: load state, assemble 5 blocks.

    v4.0: Block 4 now pulls from relation_store.fetch_active_facts (entity relation graph)
    instead of fragment_store.recall. Falls back to fragment recall if relation_store unavailable.

    Args:
        dag: DAG instance.
        wm: WorkMemory instance.
        node_id: Node id to slide into.
        memory_store: v4.0 FragmentStore instance (fallback for entity state).
        attention: AttentionFocus instance.

    Returns: the assembled AttentionFocus instance.
    """
    node = dag.get_node(node_id)
    if not node:
        raise ValueError(f"Node {node_id} not found")

    ctx = node.get("exec_context", "{}")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {"raw": ctx}

    # Block 1: Node execution context
    block_1_parts = [f"当前节点任务: {node['task']}"]
    if ctx:
        for k, v in ctx.items():
            if k.startswith("_"):
                continue
            block_1_parts.append(f"  {k}: {v}")
    if node.get("result"):
        block_1_parts.append(f"已有结果: {node['result'][:300]}")
    block_1 = "\n".join(block_1_parts)

    # Block 2: Question queue for this node (≤5)
    entries = wm.get_node_entries(node_id)
    question_lines = []
    for e in entries[:5]:
        icon = {"question": "❓", "blocker": "🚫", "uncertain": "🤔", "state": "📝"}.get(e["entry_type"], "•")
        status_str = "未解决" if e["status"] == "open" else e["status"]
        question_lines.append(f"  {icon} [{e['entry_type']}] {e['text']} ({status_str})")
    block_2 = "\n".join(question_lines) if question_lines else "（无待解决问题）"

    # Block 3: Parent summary + sibling status
    parent_summary = dag.get_parent_summary(node_id)
    sibling_status = dag.get_sibling_status(node_id)
    sib_lines = []
    for sid, st in sibling_status.items():
        icon = {"pending": "⏳", "running": "▶️", "blocked": "🚫", "done": "✅", "failed": "❌", "split": "🔀"}.get(st, "•")
        sib_lines.append(f"  {icon} {sid[:8]}: {st}")
    block_3_parts = [f"父节点: {parent_summary}"]
    if sib_lines:
        block_3_parts.append("兄弟节点:")
        block_3_parts.extend(sib_lines)
    block_3 = "\n".join(block_3_parts)

    # Block 4: Entity state — try relation_store first, then fallback to fragment recall
    block_4 = ""
    task_text = node.get("task", "")

    # Try relation_store (v4.0 L3 enhanced)
    try:
        from memory.entity_store import EntityStore
        from memory.relation_store import RelationStore
        es = EntityStore()
        rs = RelationStore()

        # Extract entity names from node task text via simple heuristic
        words = task_text.replace("，", " ").replace("、", " ").replace("。", " ").split()
        known_entities = []
        for word in words:
            if len(word) >= 2:
                ent = es.get_by_name(word)
                if ent:
                    known_entities.append(ent["id"])

        if known_entities:
            facts = rs.fetch_active_facts(known_entities)
            if facts:
                fact_lines = []
                for f in facts[:5]:
                    subj = f.get("subject_name", f"#{f['subject_id']}")
                    obj = f.get("object_name", f.get("object_value", ""))
                    ep = f.get("edge_type", "fact")
                    conf = f.get("confidence", "")
                    fact_lines.append(f"  - {subj} {f['predicate']} {obj} ({ep}" + (f", {conf:.1f})" if conf else ")"))
                block_4 = "相关实体当前状态:\n" + "\n".join(fact_lines)
    except Exception:
        pass

    # Fallback to fragment recall if relation_store unavailable or no entities found
    if not block_4 and memory_store:
        try:
            fragments = memory_store.recall(task_text, top_k=3, threshold=0.4)
            if fragments:
                entity_lines = []
                for f in fragments[:3]:
                    entity_lines.append(f"  [{f.get('ts', '')[:8]}] {f['text'][:200]}")
                block_4 = "相关记忆:\n" + "\n".join(entity_lines)
        except Exception:
            pass

    if not block_4:
        block_4 = "（无相关实体状态）"

    # Block 5: Last roundtrip result
    last_rt = wm.get_last_roundtrip_result()
    block_5 = ""
    if last_rt:
        block_5 = last_rt[0].get("text", "")[:300]
    if not block_5:
        block_5 = "（本轮尚未触发往返求解）"

    attention.set_blocks({
        "block_1": block_1,
        "block_2": block_2,
        "block_3": block_3,
        "block_4": block_4,
        "block_5": block_5,
    })
    attention.current_node_id = node_id

    return attention
