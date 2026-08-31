#!/usr/bin/env python3
"""
Work memory — task-scoped question/blocker/uncertain queue, isolated from v3.0.

Entry lifecycle:
  open → solved / escalated / closed

Roundtrip trigger:
  - question accum >= 5  → batch parallel solve
  - blocker appears       → immediate serial solve
  - no runnable nodes     → solve all, try to unblock

All entries live in work_memory table (chat.db), keyed by task_id.
Cleared on task end (after sedimentation to v3.0).
"""
import json
import os
import sys
import threading
import time
import uuid

_ROOTS = {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}


def _db_path():
    base = next(iter(_ROOTS))
    data_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else base
    return os.path.join(data_dir, "data", "chat.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS work_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    entry_type      TEXT NOT NULL,                  -- 'question'|'blocker'|'uncertain'|'state'|'roundtrip_result'
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open/solved/escalated/closed
    answer          TEXT,
    confidence      REAL DEFAULT 0.8,
    created_at      REAL NOT NULL,
    resolved_at     REAL,
    FOREIGN KEY (task_id) REFERENCES task_instances(id),
    FOREIGN KEY (node_id) REFERENCES task_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_wm_task ON work_memory(task_id);
CREATE INDEX IF NOT EXISTS idx_wm_node ON work_memory(node_id);
CREATE INDEX IF NOT EXISTS idx_wm_status ON work_memory(status);
"""


class WorkMemory:
    """Task-scoped work memory. Thread-safe."""

    def __init__(self, task_id, db_path=None):
        self.task_id = task_id
        self._db_path = db_path or _db_path()
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self):
        """Create work_memory table if not exists."""
        import sqlite3
        c = sqlite3.connect(self._db_path, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        try:
            c.executescript(SCHEMA)
            c.commit()
        finally:
            c.close()

    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self._db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    # ── CRUD ─────────────────────────────────────

    def add_entry(self, node_id, entry_type, text, confidence=0.8):
        """Add a question/blocker/uncertain/state entry. Returns entry dict."""
        if entry_type not in ("question", "blocker", "uncertain", "state"):
            raise ValueError(f"Invalid entry type: {entry_type}")
        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                cur = c.execute(
                    """INSERT INTO work_memory
                       (task_id, node_id, entry_type, text, status, confidence, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (self.task_id, node_id, entry_type, text, 'open', confidence, now)
                )
                c.commit()
                eid = cur.lastrowid
                row = c.execute("SELECT * FROM work_memory WHERE id=?", (eid,)).fetchone()
                return dict(row) if row else {"id": eid}
            finally:
                c.close()

    def resolve_entry(self, entry_id, answer):
        """Mark an entry as solved. Returns updated entry."""
        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                c.execute(
                    "UPDATE work_memory SET status='solved', answer=?, resolved_at=? WHERE id=?",
                    (answer, now, entry_id)
                )
                c.commit()
                row = c.execute("SELECT * FROM work_memory WHERE id=?", (entry_id,)).fetchone()
                return dict(row) if row else None
            finally:
                c.close()

    def escalate_entry(self, entry_id):
        """Mark uncertain as escalated (upgrade to blocker)."""
        with self._lock:
            c = self._conn()
            try:
                c.execute(
                    "UPDATE work_memory SET status='escalated', entry_type='blocker' WHERE id=?",
                    (entry_id,)
                )
                c.commit()
                row = c.execute("SELECT * FROM work_memory WHERE id=?", (entry_id,)).fetchone()
                return dict(row) if row else None
            finally:
                c.close()

    def close_entry(self, entry_id):
        """Close an entry without resolving (e.g. no longer relevant)."""
        with self._lock:
            c = self._conn()
            try:
                c.execute(
                    "UPDATE work_memory SET status='closed' WHERE id=?",
                    (entry_id,)
                )
                c.commit()
            finally:
                c.close()

    # ── Queries ───────────────────────────────────

    def get_open_entries(self, node_id=None, entry_type=None):
        c = self._conn()
        try:
            conditions = ["task_id=?", "status='open'"]
            params = [self.task_id]
            if node_id:
                conditions.append("node_id=?")
                params.append(node_id)
            if entry_type:
                conditions.append("entry_type=?")
                params.append(entry_type)
            rows = c.execute(
                f"SELECT * FROM work_memory WHERE {' AND '.join(conditions)} ORDER BY created_at",
                params
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def count_questions(self):
        """Count open questions + uncertain entries."""
        c = self._conn()
        try:
            row = c.execute(
                "SELECT COUNT(*) as cnt FROM work_memory WHERE task_id=? AND status='open' AND entry_type IN ('question','uncertain')",
                (self.task_id,)
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            c.close()

    def has_blocker(self):
        c = self._conn()
        try:
            row = c.execute(
                "SELECT 1 FROM work_memory WHERE task_id=? AND status='open' AND entry_type='blocker' LIMIT 1",
                (self.task_id,)
            ).fetchone()
            return row is not None
        finally:
            c.close()

    def get_questions(self):
        return self.get_open_entries(entry_type="question")

    def get_blockers(self):
        return self.get_open_entries(entry_type="blocker")

    def get_uncertains(self):
        return self.get_open_entries(entry_type="uncertain")

    def get_node_entries(self, node_id):
        return self.get_open_entries(node_id=node_id)

    # ── Roundtrip result ──────────────────────────

    def save_roundtrip_result(self, result_text):
        """Save a roundtrip result as a special entry readers check for block 5."""
        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                c.execute(
                    """INSERT INTO work_memory
                       (task_id, node_id, entry_type, text, status, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (self.task_id, "__roundtrip__", "roundtrip_result", result_text, "closed", now)
                )
                c.commit()
            finally:
                c.close()

    def get_last_roundtrip_result(self):
        """Get most recent roundtrip result (block 5 for attention focus)."""
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT * FROM work_memory WHERE task_id=? AND entry_type='roundtrip_result' ORDER BY created_at DESC LIMIT 1",
                (self.task_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    # ── Clear (task end) ──────────────────────────

    def clear(self):
        """Delete ALL work memory for this task (after sedimentation)."""
        with self._lock:
            c = self._conn()
            try:
                c.execute("DELETE FROM work_memory WHERE task_id=?", (self.task_id,))
                c.commit()
            finally:
                c.close()


# ── Roundtrip Solver ──────────────────────────────────

def roundtrip_solve(wm, model=None, llm_prompt_fn=None):
    """One roundtrip: batch-solve questions, serial-solve blockers, promote stuck uncertains.

    v4.0 enhanced: fast path does real web search + archive recall for questions;
    slow path uses LLM for blocker decisions. Uncertains promoted to blockers on timeout.

    Args:
        wm: WorkMemory instance.
        model: Optional LLM callable for blocker decisions.
        llm_prompt_fn: Fallback direct-LLM function (system, user) -> str.

    Returns: list of result messages.
    """
    results = []

    # 0. Try to import retrievers (lazy, may not be available)
    _web_search_fn = None
    _archive_recall_fn = None
    try:
        from memory.fragment_store import FragmentStore
        fs = FragmentStore()
        _archive_recall_fn = lambda q: fs.recall_archive(q, top_k=3, threshold=0.4)
    except Exception:
        pass
    try:
        # Try importing web_search from tools
        from tools.registry import execute_tool
        _web_search_fn = lambda q: execute_tool("web_search", {"query": q})
    except Exception:
        pass

    # 1. Questions — fast path: batch parallel retrieval
    questions = wm.get_questions()
    if questions:
        results.append(f"[往返] 批量求解 {len(questions)} 个 question（快路径: 外部队索 + 归档召回）")
        for q in questions:
            q_text = q["text"]
            answers = []

            # Archive recall
            if _archive_recall_fn:
                try:
                    archive_hits = _archive_recall_fn(q_text)
                    if archive_hits:
                        for ah in archive_hits[:2]:
                            answers.append(f"[归档] {ah.get('text', '')[:200]}")
                except Exception:
                    pass

            # Web search
            if _web_search_fn:
                try:
                    web_result = _web_search_fn(q_text)
                    if web_result and isinstance(web_result, dict):
                        web_text = web_result.get("result", "")
                        if web_text:
                            answers.append(f"[搜索] {str(web_text)[:200]}")
                except Exception:
                    pass

            # Resolve with whatever we found
            if answers:
                combined = "\n".join(answers)
                wm.resolve_entry(q["id"], combined)
                results.append(f"  ✓ question [{q_text[:50]}…] → 已解析 ({len(answers)} 条来源)")
            else:
                wm.resolve_entry(q["id"], "【快速路径未找到答案，请手动处理】")
                results.append(f"  ⚠ question [{q_text[:50]}…] → 未找到外部来源")
 
    # 2. Blockers — slow path: serial LLM decision
    blockers = wm.get_blockers()
    for blocker in blockers:
        node_id = blocker["node_id"]
        text = blocker["text"]
        results.append(f"[往返] 慢路径: 处理 blocker: {text}")

        decision = None
        if llm_prompt_fn:
            decision = llm_prompt_fn(
                "你是一个任务决策助手。面对执行阻塞，给出解决方案方向：换方案/分解/查资料/问用户。"
                "如果可以选择换方案，给出具体替代方案。如果建议分解，给出子任务列表。"
                "只输出最终决策，不要解释。",
                f"当前节点卡在: {text}\n请输出决定和具体方案:"
            )
        if not decision:
            decision = "需要人工介入或切换方案"

        # Try web search for blocker too
        if _web_search_fn and "人工介入" in decision:
            try:
                web_result = _web_search_fn(text)
                if web_result and isinstance(web_result, dict):
                    web_text = web_result.get("result", "")
                    if web_text:
                        decision = f"搜索建议: {str(web_text)[:200]}"
            except Exception:
                pass

        wm.resolve_entry(blocker["id"], decision)
        results.append(f"[往返] blocker 决策: {decision}")

    # 3. Uncertain — check stuck timeout, promote to blocker
    uncertains = wm.get_uncertains()
    now = time.time()
    for u in uncertains:
        age = now - u["created_at"]
        if age > 30:  # 30 seconds timeout (configurable)
            wm.escalate_entry(u["id"])
            results.append(f"[往返] uncertain 升级为 blocker: {u['text']}")

    # 4. Save roundtrip record
    summary = "\n".join(results)
    wm.save_roundtrip_result(summary)
    return results
