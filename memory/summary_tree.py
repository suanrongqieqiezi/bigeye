#!/usr/bin/env python3
"""
Summary tree — RAPTOR-style recursive summary with Merkle incremental caching.

Schema matches: 自传体记忆系统完整实现计划.md §3.1 memory_summary_nodes

Layer structure:
  0: monthly   — each entry = one month (period = "YYYY-MM")
  1: quarterly — each entry = three months ("YYYY-QQ")
  2: yearly    — each entry = one year ("YYYY")

Promotion rule: when a layer has ≥ 3 siblings, trigger parent layer summary.

Merkle: node_hash = SHA256(text + "|" + concat(child_hashes))
  - On leaf change, recompute upward until hash stabilizes or root reached.
  - Unchanged subtrees are NEVER recomputed (incremental guarantee).
"""
import hashlib
import json
import os
import sys
import sqlite3
import time
import calendar


def _get_db_path():
    MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(MEMORY_DIR)
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_summary_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    layer           INTEGER NOT NULL,
    period          TEXT NOT NULL,
    text            TEXT NOT NULL,
    embedding       TEXT NOT NULL DEFAULT '[]',
    child_ids       TEXT NOT NULL DEFAULT '[]',
    child_hashes    TEXT NOT NULL DEFAULT '[]',
    node_hash       TEXT NOT NULL,
    entity_ids      TEXT DEFAULT NULL,
    created_at      REAL NOT NULL,
    last_recomputed REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_layer ON memory_summary_nodes(layer);
CREATE INDEX IF NOT EXISTS idx_summary_period ON memory_summary_nodes(period);
CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_layer_period ON memory_summary_nodes(layer, period);
"""

MAX_LAYER = 2  # 0=monthly, 1=quarterly, 2=yearly
PROMOTE_THRESHOLD = 3  # same-layer siblings ≥ 3 → promote


def _hash(text: str, child_hashes: list[str]) -> str:
    """Merkle hash: SHA256(text + "|" + concat(child_hashes))."""
    raw = text + "|" + "".join(child_hashes)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _period_for_layer(ts: float, layer: int) -> str:
    """Compute period string from timestamp and layer."""
    t = time.gmtime(ts)
    y, m = t.tm_year, t.tm_mon
    if layer == 0:
        return f"{y:04d}-{m:02d}"
    elif layer == 1:
        q = (m - 1) // 3 + 1
        return f"{y:04d}-Q{q}"
    else:
        return f"{y:04d}"


def _parent_period(period: str) -> str | None:
    """Given a period string, return parent period or None if already max."""
    parts = period.split("-")
    try:
        y = int(parts[0])
    except ValueError:
        return None
    if len(parts) == 2 and "Q" not in parts[1]:
        # monthly → quarterly
        m = int(parts[1])
        q = (m - 1) // 3 + 1
        return f"{y:04d}-Q{q}"
    elif "Q" in parts[1]:
        # quarterly → yearly
        return f"{y:04d}"
    else:
        return None  # yearly = max layer


def _parent_layer(layer: int) -> int | None:
    """Return parent layer number, or None if already max."""
    return layer + 1 if layer < MAX_LAYER else None


def _layers_below(period: str, layer: int) -> list[str]:
    """List all child periods that belong to this parent period."""
    parts = period.split("-")
    y = int(parts[0])
    if layer == 1:
        # quarterly → list months
        q = int(parts[1].replace("Q", ""))
        start_m = (q - 1) * 3 + 1
        return [f"{y:04d}-{m:02d}" for m in range(start_m, start_m + 3)]
    elif layer == 2:
        # yearly → list quarters
        return [f"{y:04d}-Q{q}" for q in range(1, 5)]
    return []


# ── SummaryTree ────────────────────────────────────

class SummaryTree:
    def __init__(self, db_path=None):
        self.db_path = db_path or _get_db_path()
        self._ensure_schema()

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

    def add_leaf(self, ts: float, text: str, entity_ids: list[int] = None,
                 llm_prompt_fn=None) -> dict:
        """Add a daily summary as a leaf node (layer 0, period = YYYY-MM).

        If a node for this period already exists, append to it.
        Triggers Merkle recomputation upward + possible promotion.

        Args:
            ts: timestamp
            text: summary text content
            entity_ids: optional list of entity ids mentioned
            llm_prompt_fn: callable(prompt, text) → LLM response for summarization

        Returns: the created/updated node dict.
        """
        period = _period_for_layer(ts, layer=0)
        entity_ids_str = json.dumps(entity_ids or [])

        conn = self._conn()
        try:
            now = time.time()

            # Check if leaf exists
            existing = conn.execute(
                "SELECT * FROM memory_summary_nodes WHERE layer=0 AND period=?",
                (period,)
            ).fetchone()

            if existing:
                # Append: combine existing + new text, recompute
                combined = existing["text"] + "\n" + text
                if llm_prompt_fn:
                    combined = llm_prompt_fn(
                        "将以下碎片记忆合并成一段连贯的月度摘要，50-80字，保留关键实体。",
                        combined
                    ) or combined
                child_hashes = json.loads(existing["child_hashes"] or "[]")
                node_hash = _hash(combined, child_hashes)
                # Merge entity_ids
                old_entities = set(json.loads(existing["entity_ids"] or "[]"))
                new_entities = old_entities | set(entity_ids or [])
                conn.execute(
                    """UPDATE memory_summary_nodes
                       SET text=?, node_hash=?, entity_ids=?, last_recomputed=?
                       WHERE id=?""",
                    (combined, node_hash, json.dumps(list(new_entities)), now, existing["id"])
                )
                conn.commit()
                row = conn.execute("SELECT * FROM memory_summary_nodes WHERE id=?", (existing["id"],)).fetchone()
                node = dict(row)
            else:
                # Create new leaf
                child_hashes = []
                node_hash = _hash(text, child_hashes)
                cur = conn.execute(
                    """INSERT INTO memory_summary_nodes
                       (layer, period, text, embedding, child_ids, child_hashes, node_hash,
                        entity_ids, created_at, last_recomputed)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (0, period, text, "[]", "[]", json.dumps(child_hashes), node_hash,
                     entity_ids_str, now, now)
                )
                conn.commit()
                row = conn.execute("SELECT * FROM memory_summary_nodes WHERE id=?", (cur.lastrowid,)).fetchone()
                node = dict(row)

            # Merkle recompute upward from this node
            self._recompute_upward(node)

            # Check if promotion needed
            self._maybe_promote_to_parent_layer(node)

            # Re-fetch after promotions
            row = conn.execute("SELECT * FROM memory_summary_nodes WHERE id=?", (node["id"],)).fetchone()
            return dict(row) if row else node
        finally:
            conn.close()

    def get_summary(self, period: str, layer: int = 0) -> dict | None:
        """Get summary node by period + layer."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM memory_summary_nodes WHERE layer=? AND period=?",
                (layer, period)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_children(self, node_id: int) -> list[dict]:
        """Get all children of a summary node by following child_ids."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT child_ids FROM memory_summary_nodes WHERE id=?", (node_id,)).fetchone()
            if not row:
                return []
            child_ids = json.loads(row["child_ids"] or "[]")
            if not child_ids:
                return []
            placeholders = ",".join("?" for _ in child_ids)
            rows = conn.execute(
                f"SELECT * FROM memory_summary_nodes WHERE id IN ({placeholders})",
                child_ids
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_tree(self, root_layer: int = 2, root_period: str = None) -> dict | None:
        """Get full summary tree from root downward. Returns nested dict."""
        if root_period is None:
            root_period = time.strftime("%Y")
        conn = self._conn()
        try:
            root = conn.execute(
                "SELECT * FROM memory_summary_nodes WHERE layer=? AND period=?",
                (root_layer, root_period)
            ).fetchone()
            if not root:
                return None
            return self._build_tree(dict(root), conn)
        finally:
            conn.close()

    def _build_tree(self, node: dict, conn) -> dict:
        children = []
        child_ids = json.loads(node.get("child_ids", "[]"))
        if child_ids:
            placeholders = ",".join("?" for _ in child_ids)
            rows = conn.execute(
                f"SELECT * FROM memory_summary_nodes WHERE id IN ({placeholders})",
                child_ids
            ).fetchall()
            for row in rows:
                children.append(self._build_tree(dict(row), conn))
        return {
            "id": node["id"],
            "layer": node["layer"],
            "period": node["period"],
            "text": node["text"],
            "node_hash": node["node_hash"],
            "entity_ids": json.loads(node.get("entity_ids", "[]")),
            "created_at": node["created_at"],
            "last_recomputed": node["last_recomputed"],
            "children": children,
        }

    def list_layer(self, layer: int) -> list[dict]:
        """List all nodes at a given layer."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_summary_nodes WHERE layer=? ORDER BY period",
                (layer,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _recompute_upward(self, node: dict):
        """Merkle incremental recomputation: from node up to root.

        If node_hash didn't change, stop (no upward cascade).
        If changed, update parent's child_hashes, recompute parent, recurse.
        """
        node_id = node["id"]
        conn = self._conn()
        try:
            old_hash = node["node_hash"]
            child_hashes = json.loads(node.get("child_hashes", "[]"))
            new_hash = _hash(node["text"], child_hashes)

            if new_hash == old_hash:
                return  # No change, stop upward

            now = time.time()
            conn.execute(
                "UPDATE memory_summary_nodes SET node_hash=?, last_recomputed=? WHERE id=?",
                (new_hash, now, node_id)
            )
            conn.commit()

            # Find parent (a node whose child_ids contains this node)
            parent = conn.execute(
                "SELECT * FROM memory_summary_nodes WHERE child_ids LIKE ?",
                (f"%{node_id}%",)
            ).fetchone()
            if parent:
                parent = dict(parent)
                parent_child_ids = json.loads(parent.get("child_ids", "[]"))
                parent_child_hashes = json.loads(parent.get("child_hashes", "[]"))
                # Update the hash for this child
                for i, cid in enumerate(parent_child_ids):
                    if cid == node_id:
                        if i < len(parent_child_hashes):
                            parent_child_hashes[i] = new_hash
                        break
                conn.execute(
                    "UPDATE memory_summary_nodes SET child_hashes=?, last_recomputed=? WHERE id=?",
                    (json.dumps(parent_child_hashes), now, parent["id"])
                )
                conn.commit()
                # Recurse upward
                self._recompute_upward(parent)
        finally:
            conn.close()

    def _maybe_promote_to_parent_layer(self, node: dict):
        """Check if same-layer siblings ≥ PROMOTE_THRESHOLD → generate parent summary."""
        period = node["period"]
        layer = node["layer"]
        parent_lyr = _parent_layer(layer)
        if parent_lyr is None:
            return  # Already max layer

        parent_prd = _parent_period(period)
        if parent_prd is None:
            return

        conn = self._conn()
        try:
            child_periods = _layers_below(parent_prd, parent_lyr)
            if not child_periods:
                return
            sibling_rows = conn.execute(
                f"SELECT * FROM memory_summary_nodes WHERE layer=? AND period IN ({','.join('?' for _ in child_periods)})",
                [node["layer"]] + child_periods
            ).fetchall()
            siblings = [dict(r) for r in sibling_rows]
            if len(siblings) < PROMOTE_THRESHOLD:
                return

            # Generate or update parent summary
            sibling_texts = "\n---\n".join(
                f"[{s['period']}] {s['text']}" for s in siblings
            )
            sibling_ids = [s["id"] for s in siblings]
            sibling_hashes = [s["node_hash"] for s in siblings]

            # Combine sibling texts into a parent summary
            combined_text = sibling_texts[:2000]  # hard cap

            # Check if parent already exists
            existing_parent = conn.execute(
                "SELECT * FROM memory_summary_nodes WHERE layer=? AND period=?",
                (parent_lyr, parent_prd)
            ).fetchone()

            node_hash = _hash(combined_text, sibling_hashes)

            # Merge entity_ids from children
            all_entity_ids = set()
            for s in siblings:
                eids = json.loads(s.get("entity_ids", "[]"))
                all_entity_ids.update(eids)

            now = time.time()
            if existing_parent:
                conn.execute(
                    """UPDATE memory_summary_nodes
                       SET text=?, child_ids=?, child_hashes=?, node_hash=?,
                           entity_ids=?, last_recomputed=?
                       WHERE id=?""",
                    (combined_text, json.dumps(sibling_ids), json.dumps(sibling_hashes),
                     node_hash, json.dumps(list(all_entity_ids)), now, existing_parent["id"])
                )
                conn.commit()
                parent_node = dict(existing_parent)
            else:
                cur = conn.execute(
                    """INSERT INTO memory_summary_nodes
                       (layer, period, text, embedding, child_ids, child_hashes, node_hash,
                        entity_ids, created_at, last_recomputed)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (parent_lyr, parent_prd, combined_text, "[]",
                     json.dumps(sibling_ids), json.dumps(sibling_hashes), node_hash,
                     json.dumps(list(all_entity_ids)), now, now)
                )
                conn.commit()
                row = conn.execute("SELECT * FROM memory_summary_nodes WHERE id=?", (cur.lastrowid,)).fetchone()
                parent_node = dict(row)

            # Recurse: parent may need its own promotion
            self._recompute_upward(parent_node)
            self._maybe_promote_to_parent_layer(parent_node)
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._conn()
        try:
            layer0 = conn.execute("SELECT COUNT(*) as c FROM memory_summary_nodes WHERE layer=0").fetchone()["c"]
            layer1 = conn.execute("SELECT COUNT(*) as c FROM memory_summary_nodes WHERE layer=1").fetchone()["c"]
            layer2 = conn.execute("SELECT COUNT(*) as c FROM memory_summary_nodes WHERE layer=2").fetchone()["c"]
            return {"monthly": layer0, "quarterly": layer1, "yearly": layer2}
        finally:
            conn.close()
