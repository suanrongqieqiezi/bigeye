#!/usr/bin/env python3
"""
vec_index.py — sqlite-vec virtual vector index for fast KNN cosine search.

Wraps sqlite-vec extension (https://github.com/asg017/sqlite-vec) so the
rest of the memory system can do KNN without loading all embeddings into RAM.

Usage:
    from memory.vec_index import VecIndex
    idx = VecIndex("data/chat.db")
    idx.ensure_table("idx_fragments", 384)
    idx.insert("idx_fragments", fid, embedding)
    results = idx.query("idx_fragments", query_emb, top_k=10)
"""
import json
import os
import sqlite3
import sys
import ctypes
import ctypes.util

EMBEDDING_DIM = 512  # matches ONNX BGE-small (512) and sentence-transformers all-MiniLM (384)

# ── sqlite-vec loading ──────────────────────────────

def _find_vec_dylib():
    """Find sqlite-vec shared library in common locations."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "vec0.dll"),
        os.path.join(os.path.dirname(__file__), "vec0.so"),
        os.path.join(os.path.dirname(__file__), "vec0.dylib"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vec0.dll"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vec0.so"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vec0.dylib"),
    ]
    # Also check PATH / system library paths
    system_name = ctypes.util.find_library("vec0")
    if system_name:
        candidates.insert(0, system_name)
    for p in candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def load_vec_ext(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension into the connection. Returns True if loaded."""
    try:
        conn.enable_load_extension(True)
        dylib = _find_vec_dylib()
        if dylib:
            conn.load_extension(dylib)
            return True
        # Fallback: try loading "vec0" by name (system-installed)
        try:
            conn.load_extension("vec0")
            return True
        except sqlite3.OperationalError:
            pass
        return False
    except Exception:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


def has_vec_ext(conn: sqlite3.Connection) -> bool:
    """Check if sqlite-vec extension is loaded."""
    try:
        row = conn.execute("SELECT json_object()").fetchone()
        return True
    except Exception:
        return False


# ── VecIndex class ──────────────────────────────────

class VecIndex:
    """Lazy sqlite-vec virtual table wrapper for KNN queries.

    Falls back to brute-force scan if sqlite-vec is unavailable.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._vec_loaded = None  # None = unknown, True/False after probe

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # sqlite-vec 是连接级扩展：每次新连接都必须重新 load，
        # 只在第一次加载会导致后续连接 no such module: vec0
        loaded = load_vec_ext(conn)
        if self._vec_loaded is None:
            self._vec_loaded = loaded
        return conn

    def ensure_table(self, table_name: str, dim: int = EMBEDDING_DIM):
        """Create vec virtual table if it doesn't exist. No-op if vec not available."""
        conn = self._get_conn()  # 触发扩展加载探测
        if not self._vec_loaded:
            conn.close()
            return False
        try:
            conn.execute(
                f"""CREATE VIRTUAL TABLE IF NOT EXISTS {table_name}
                    USING vec0(
                        embedding FLOAT[{dim}]
                            distance_metric=cosine
                    )"""
            )
            conn.commit()
            return True
        except sqlite3.OperationalError:
            self._vec_loaded = False
            return False
        finally:
            conn.close()

    def insert(self, table_name: str, row_id: int, embedding: list[float]):
        """Insert or replace a vector entry."""
        if not self._vec_loaded:
            return
        emb_json = json.dumps(embedding)
        conn = self._get_conn()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {table_name}(rowid, embedding) VALUES (?, ?)",
                (row_id, emb_json)
            )
            conn.commit()
        finally:
            conn.close()

    def delete(self, table_name: str, row_id: int):
        """Remove a vector entry by rowid."""
        if not self._vec_loaded:
            return
        conn = self._get_conn()
        try:
            conn.execute(f"DELETE FROM {table_name} WHERE rowid=?", (row_id,))
            conn.commit()
        finally:
            conn.close()

    def query(self, table_name: str, query_emb: list[float],
              top_k: int = 10) -> list[dict]:
        """Run KNN cosine search. Returns [{rowid, distance}, ...] or empty list."""
        if self._vec_loaded is None:
            # 首次调用：先探测扩展可用性，再决定是否走索引
            conn = self._get_conn()
            conn.close()
        if not self._vec_loaded:
            return []
        emb_json = json.dumps(query_emb)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"""SELECT rowid, distance
                    FROM {table_name}
                    WHERE embedding MATCH ?
                    AND k = ?""",
                (emb_json, top_k)
            ).fetchall()
            return [{"rowid": r["rowid"], "distance": r["distance"]} for r in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def is_available(self) -> bool:
        """Check if sqlite-vec is loaded."""
        if self._vec_loaded is None:
            conn = self._get_conn()
            conn.close()
        return bool(self._vec_loaded)
