#!/usr/bin/env python3
"""
Relation store — temporal relation graph with fact state machine and causal edges.

Schema matches: 自传体记忆系统完整实现计划.md §3.1 memory_relations

Fact state machine:
  New fact (active, valid_to=NULL)
    │ subject+predicate collides with existing active fact
    ▼
  Old fact → superseded, valid_to = new fact valid_from
  New fact → active, valid_to = NULL

Edge types:
  fact    — state/action/attribute (default)
  causal  — causal link (higher recall weight, one-hop propagation)
  temporal — pure temporal ordering
"""
import json
import os
import sys
import sqlite3
import time

from .embedder import cosine_sim, embed

RELATION_CONFIDENCE_MIN = 0.5
CAUSAL_CONFIDENCE_MIN = 0.7
CAUSAL_PROPAGATION_MIN = 0.8


def _get_db_path():
    MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(MEMORY_DIR)
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_relations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id      INTEGER NOT NULL,
    predicate       TEXT NOT NULL,
    object_id       INTEGER,
    object_value    TEXT,
    valid_from      REAL NOT NULL,
    valid_to        REAL DEFAULT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    source_fragment_id INTEGER,
    confidence      REAL NOT NULL DEFAULT 0.8,
    edge_type       TEXT NOT NULL DEFAULT 'fact',
    embedding       TEXT NOT NULL DEFAULT '[]',
    -- CMN (P0): edge metadata for derive/support/negate/weak_assoc
    reason              TEXT DEFAULT NULL,      -- 建边理由（弱关联和否定边必填）
    negate_timestamp    REAL DEFAULT NULL       -- 否定边时间戳
);
CREATE INDEX IF NOT EXISTS idx_rel_subject ON memory_relations(subject_id);
CREATE INDEX IF NOT EXISTS idx_rel_object ON memory_relations(object_id);
CREATE INDEX IF NOT EXISTS idx_rel_status ON memory_relations(status);
CREATE INDEX IF NOT EXISTS idx_rel_valid ON memory_relations(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_rel_edge_type ON memory_relations(edge_type);
CREATE INDEX IF NOT EXISTS idx_rel_predicate ON memory_relations(predicate);
"""


class RelationStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or _get_db_path()
        self._ensure_schema()

    def _ensure_schema(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA)
            # CMN (P0) Migration — add reason/negate_timestamp for new edge types
            try:
                conn.execute("SELECT reason FROM memory_relations LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE memory_relations ADD COLUMN reason TEXT DEFAULT NULL")
            try:
                conn.execute("SELECT negate_timestamp FROM memory_relations LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE memory_relations ADD COLUMN negate_timestamp REAL DEFAULT NULL")
            conn.commit()
        finally:
            conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def add(self, subject_id: int, predicate: str, object_id: int = None,
            object_value: str = None, valid_from: float = None,
            edge_type: str = "fact", confidence: float = 0.8,
            source_fragment_id: int = None,
            reason: str = None, negate_timestamp: float = None) -> int:
        """Insert a new relation. Returns relation id.

        CMN P0: 支持 reason（建边理由，弱关联和否定边必填）和 negate_timestamp（否定边时间戳）。
        """
        if valid_from is None:
            valid_from = time.time()
        # Build embedding from predicate + object for similarity search
        rel_text = f"{predicate} {object_value or ''}"
        rel_emb = embed(rel_text)
        emb_json = json.dumps(rel_emb)

        conn = self._conn()
        try:
            cur = conn.execute(
                """INSERT INTO memory_relations
                   (subject_id, predicate, object_id, object_value, valid_from,
                    status, source_fragment_id, confidence, edge_type, embedding,
                    reason, negate_timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (subject_id, predicate, object_id, object_value, valid_from,
                 "active", source_fragment_id, confidence, edge_type, emb_json,
                 reason, negate_timestamp)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def upsert_with_invalidation(self, subject_id: int, predicate: str,
                                  object_id: int = None, object_value: str = None,
                                  valid_from: float = None,
                                  edge_type: str = "fact", confidence: float = 0.8,
                                  source_fragment_id: int = None) -> dict:
        """Fact state machine: supersede old active fact, insert new one.

        If subject+predicate has an active fact, mark it superseded with
        valid_to = new valid_from, then insert the new fact as active.

        Returns {"new_id": int, "superseded_id": int|None, "old_relation": dict|None}.
        """
        if valid_from is None:
            valid_from = time.time()

        conn = self._conn()
        try:
            result = {"new_id": None, "superseded_id": None, "old_relation": None}

            # Find existing active fact for same subject+predicate
            old = conn.execute(
                """SELECT * FROM memory_relations
                   WHERE subject_id=? AND predicate=? AND status='active'
                   ORDER BY valid_from DESC LIMIT 1""",
                (subject_id, predicate)
            ).fetchone()

            if old:
                # Supersede old fact
                conn.execute(
                    "UPDATE memory_relations SET status='superseded', valid_to=? WHERE id=?",
                    (valid_from, old["id"])
                )
                result["superseded_id"] = old["id"]
                result["old_relation"] = dict(old)

            # Insert new fact as active
            rel_text = f"{predicate} {object_value or ''}"
            rel_emb = embed(rel_text)
            emb_json = json.dumps(rel_emb)
            cur = conn.execute(
                """INSERT INTO memory_relations
                   (subject_id, predicate, object_id, object_value, valid_from, status,
                    source_fragment_id, confidence, edge_type, embedding)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (subject_id, predicate, object_id, object_value, valid_from,
                 "active", source_fragment_id, confidence, edge_type, emb_json)
            )
            result["new_id"] = cur.lastrowid
            conn.commit()
            return result
        finally:
            conn.close()

    def fetch_active_facts(self, entity_ids: list[int]) -> list[dict]:
        """Return all status=active facts for given entity ids (as subject or object)."""
        if not entity_ids:
            return []
        conn = self._conn()
        try:
            placeholders = ",".join("?" for _ in entity_ids)
            rows = conn.execute(
                f"""SELECT r.*, s.name as subject_name, o.name as object_name
                    FROM memory_relations r
                    LEFT JOIN memory_entities s ON r.subject_id = s.id
                    LEFT JOIN memory_entities o ON r.object_id = o.id
                    WHERE r.status='active'
                    AND (r.subject_id IN ({placeholders})
                         OR (r.object_id IS NOT NULL AND r.object_id IN ({placeholders})))
                    ORDER BY r.valid_from DESC
                    LIMIT 50""",
                entity_ids + entity_ids
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def fetch_causal_neighbors(self, entity_ids: list[int],
                                max_hop: int = 1,
                                min_confidence: float = CAUSAL_PROPAGATION_MIN) -> list[dict]:
        """Follow causal edges from given entities, one hop, min confidence threshold."""
        if not entity_ids:
            return []
        conn = self._conn()
        try:
            placeholders = ",".join("?" for _ in entity_ids)
            rows = conn.execute(
                f"""SELECT r.*, s.name as subject_name, o.name as object_name
                    FROM memory_relations r
                    LEFT JOIN memory_entities s ON r.subject_id = s.id
                    LEFT JOIN memory_entities o ON r.object_id = o.id
                    WHERE r.status='active'
                    AND r.edge_type='causal'
                    AND r.confidence >= ?
                    AND (r.subject_id IN ({placeholders})
                         OR (r.object_id IS NOT NULL AND r.object_id IN ({placeholders})))
                    ORDER BY r.confidence DESC
                    LIMIT 10""",
                [min_confidence] + entity_ids
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def fetch_entity_history(self, entity_id: int, limit: int = 20) -> list[dict]:
        """Full history of an entity's relations (all statuses)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT r.*, s.name as subject_name, o.name as object_name
                   FROM memory_relations r
                   LEFT JOIN memory_entities s ON r.subject_id = s.id
                   LEFT JOIN memory_entities o ON r.object_id = o.id
                   WHERE r.subject_id=? OR r.object_id=?
                   ORDER BY r.valid_from DESC LIMIT ?""",
                (entity_id, entity_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_all(self, status: str = None, edge_type: str = None,
                 limit: int = 100, offset: int = 0) -> list[dict]:
        conn = self._conn()
        try:
            conditions = []
            params = []
            if status:
                conditions.append("r.status=?")
                params.append(status)
            if edge_type:
                conditions.append("r.edge_type=?")
                params.append(edge_type)
            where = " AND ".join(conditions) if conditions else "1=1"
            rows = conn.execute(
                f"""SELECT r.*, s.name as subject_name, o.name as object_name
                    FROM memory_relations r
                    LEFT JOIN memory_entities s ON r.subject_id = s.id
                    LEFT JOIN memory_entities o ON r.object_id = o.id
                    WHERE {where}
                    ORDER BY r.valid_from DESC LIMIT ? OFFSET ?""",
                params + [limit, offset]
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get(self, relation_id: int) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute(
                """SELECT r.*, s.name as subject_name, o.name as object_name
                   FROM memory_relations r
                   LEFT JOIN memory_entities s ON r.subject_id = s.id
                   LEFT JOIN memory_entities o ON r.object_id = o.id
                   WHERE r.id=?""",
                (relation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) as c FROM memory_relations").fetchone()["c"]
            active = conn.execute("SELECT COUNT(*) as c FROM memory_relations WHERE status='active'").fetchone()["c"]
            superseded = conn.execute("SELECT COUNT(*) as c FROM memory_relations WHERE status='superseded'").fetchone()["c"]
            causal = conn.execute("SELECT COUNT(*) as c FROM memory_relations WHERE edge_type='causal' AND status='active'").fetchone()["c"]
            # CMN P4: 新边类型统计
            by_edge_type = {}
            for r in conn.execute(
                "SELECT edge_type, COUNT(*) as c FROM memory_relations WHERE status='active' GROUP BY edge_type"
            ).fetchall():
                by_edge_type[r["edge_type"]] = r["c"]
            return {"total": total, "active": active, "superseded": superseded,
                    "causal_active": causal, "by_edge_type": by_edge_type}
        finally:
            conn.close()

    # ── CMN P4: 反思回路辅助查询 ──────────────────────

    def get_edges_by_type(self, edge_type: str, limit: int = 100) -> list:
        """按 edge_type 查边。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_relations WHERE edge_type=? AND status='active' LIMIT ?",
                (edge_type, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def find_weak_assocs_for(self, subject_id: int) -> list:
        """查某节点的所有弱关联边。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_relations WHERE subject_id=? AND edge_type='weak_assoc' AND status='active'",
                (subject_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def find_isolated_fragments(self, limit: int = 50) -> list:
        """找孤立晶体：没有任何 active 边的 fragment id。

        供反思回路建弱关联用。
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT f.id, f.text, f.created_at FROM memory_fragments f
                   WHERE f.dirty=1 AND f.node_type='self'
                   AND NOT EXISTS (
                       SELECT 1 FROM memory_relations r
                       WHERE r.source_fragment_id=f.id AND r.status='active'
                   )
                   AND f.crystal_parent_id IS NULL
                   ORDER BY f.created_at DESC LIMIT ?""",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_edges_for_fragment(self, fragment_id: int) -> int:
        """统计某 fragment 关联的边数。"""
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) as c FROM memory_relations WHERE source_fragment_id=? AND status='active'",
                (fragment_id,)
            ).fetchone()["c"]
        finally:
            conn.close()

    def delete_edge(self, relation_id: int) -> bool:
        """删除某条边（反熵修剪用）。"""
        conn = self._conn()
        try:
            conn.execute("DELETE FROM memory_relations WHERE id=?", (relation_id,))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def find_duplicate_edges(self) -> list:
        """找重复边（同 subject+object+edge_type 多条 active）。

        供反熵修剪合并用。
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT subject_id, object_id, edge_type, COUNT(*) as c,
                          GROUP_CONCAT(id) as ids
                   FROM memory_relations
                   WHERE status='active' AND object_id IS NOT NULL
                   GROUP BY subject_id, object_id, edge_type
                   HAVING c > 1"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
