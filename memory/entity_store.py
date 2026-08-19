#!/usr/bin/env python3
"""
Entity store — CRUD for memory entities with normalization and deactivation.

Schema matches: 自传体记忆系统完整实现计划.md §3.1 memory_entities

Entity lifecycle:
  active → inactive (180 days no mention, data preserved)
  Entities are NEVER deleted, only deactivated.
"""
import json
import os
import sys
import sqlite3
import time

from .embedder import embed, cosine_sim, EMBEDDING_DIM
from .vec_index import VecIndex

ENTITY_ACTIVE_DAYS = 180


def _get_db_path():
    MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(MEMORY_DIR)
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    type          TEXT DEFAULT 'person',
    aliases       TEXT DEFAULT '[]',
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 1,
    embedding     TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON memory_entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_status ON memory_entities(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name_type ON memory_entities(name, type);
"""


def _normalize_name(name: str) -> str:
    """Normalize entity name: strip, lower, collapse whitespace."""
    return " ".join(name.strip().lower().split())


class EntityStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or _get_db_path()
        self._ensure_schema()
        self.vec_index = VecIndex(self.db_path)
        self._vec_table = "idx_entities"
        self.vec_index.ensure_table(self._vec_table, EMBEDDING_DIM)

    def _ensure_schema(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            conn.executescript(SCHEMA)
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

    def upsert(self, name: str, entity_type: str = "person",
               aliases: list[str] = None) -> int:
        """Insert or update entity by normalized name+type. Returns entity id."""
        norm = _normalize_name(name)
        if not norm:
            return None
        now = time.time()
        aliases_str = json.dumps([a for a in (aliases or []) if a and a != name])
        entity_emb = embed(name)
        emb_json = json.dumps(entity_emb)

        conn = self._conn()
        try:
            existing = conn.execute(
                "SELECT id, mention_count, aliases, embedding FROM memory_entities WHERE name=? AND type=?",
                (norm, entity_type)
            ).fetchone()
            if existing:
                new_count = existing["mention_count"] + 1
                # Merge aliases
                old_aliases = set(json.loads(existing["aliases"] or "[]"))
                new_aliases = set((aliases or []))
                merged = list(old_aliases | new_aliases)
                # Keep embedding freshest (re-embed on each upsert)
                conn.execute(
                    """UPDATE memory_entities
                       SET mention_count=?, last_seen=?, aliases=?, embedding=?
                       WHERE id=?""",
                    (new_count, now, json.dumps(merged), emb_json, existing["id"])
                )
                conn.commit()
                if entity_emb and len(entity_emb) > 0:
                    self.vec_index.insert(self._vec_table, existing["id"], entity_emb)
                return existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO memory_entities
                       (name, type, aliases, first_seen, last_seen, mention_count, embedding, status)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (norm, entity_type, aliases_str, now, now, 1, emb_json, "active")
                )
                conn.commit()
                new_id = cur.lastrowid
                if entity_emb and len(entity_emb) > 0:
                    self.vec_index.insert(self._vec_table, new_id, entity_emb)
                return new_id
        finally:
            conn.close()

    def get(self, entity_id: int) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM memory_entities WHERE id=?", (entity_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_by_name(self, name: str, entity_type: str = None) -> dict | None:
        norm = _normalize_name(name)
        conn = self._conn()
        try:
            if entity_type:
                row = conn.execute(
                    "SELECT * FROM memory_entities WHERE name=? AND type=?",
                    (norm, entity_type)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM memory_entities WHERE name=? ORDER BY last_seen DESC LIMIT 1",
                    (norm,)
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_or_create(self, name: str, entity_type: str = "person",
                      aliases: list[str] = None) -> int:
        """Get existing entity id or create new one."""
        existing = self.get_by_name(name, entity_type)
        if existing:
            return existing["id"]
        return self.upsert(name, entity_type, aliases)

    def list_all(self, status: str = "active", limit: int = 100, offset: int = 0) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_entities WHERE status=? ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (status, limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def search_by_embedding(self, query_emb: list, top_k: int = 5) -> list[dict]:
        conn = self._conn()
        try:
            # v4.0: 用 vec_index KNN 预筛，不可用时回退全表扫描
            rows = None
            if self.vec_index.is_available():
                knn = self.vec_index.query(self._vec_table, query_emb, top_k=max(top_k * 4, 20))
                if knn:
                    rowids = [r["rowid"] for r in knn]
                    placeholders = ",".join("?" for _ in rowids)
                    rows = conn.execute(
                        f"SELECT * FROM memory_entities WHERE id IN ({placeholders}) AND status='active'",
                        rowids
                    ).fetchall()
            if rows is None:
                rows = conn.execute(
                    "SELECT * FROM memory_entities WHERE status='active'"
                ).fetchall()
            scored = []
            for row in rows:
                emb = json.loads(row["embedding"])
                if not emb or len(emb) == 0:
                    continue
                sim = cosine_sim(query_emb, emb)
                scored.append((dict(row), sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [s[0] for s in scored[:top_k]]
        finally:
            conn.close()

    def search_by_name(self, keyword: str, limit: int = 10) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_entities WHERE name LIKE ? ORDER BY mention_count DESC LIMIT ?",
                (f"%{keyword}%", limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def deactivate_stale(self, days: int = ENTITY_ACTIVE_DAYS) -> int:
        """Mark entities inactive if not seen for `days`. Returns count."""
        conn = self._conn()
        try:
            cutoff = time.time() - days * 86400
            cur = conn.execute(
                "UPDATE memory_entities SET status='inactive' WHERE status='active' AND last_seen < ?",
                (cutoff,)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def reactivate(self, entity_id: int) -> bool:
        """Reactivate an inactive entity."""
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE memory_entities SET status='active', last_seen=? WHERE id=?",
                (time.time(), entity_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._conn()
        try:
            active = conn.execute("SELECT COUNT(*) as c FROM memory_entities WHERE status='active'").fetchone()["c"]
            inactive = conn.execute("SELECT COUNT(*) as c FROM memory_entities WHERE status='inactive'").fetchone()["c"]
            return {"active": active, "inactive": inactive}
        finally:
            conn.close()
