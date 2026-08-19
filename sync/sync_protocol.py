#!/usr/bin/env python3
"""
Core sync protocol: hash tree, diff, merge, conflict resolution.

Every synced row gets a SHA-256 hash. Sync = diff two hash sets
and exchange only changed rows. Last-write-wins by timestamp.
"""
import hashlib
import json
import sqlite3
import time

# Which tables to sync and their key column + timestamp column
SYNC_TABLES = {
    "topics": {
        "key": "id",                       # TEXT primary key
        "ts":  "updated_at",               # REAL, last-write-wins
        "order_by": "updated_at ASC",
    },
    "messages": {
        "key": "id",                       # INTEGER primary key
        "ts":  "created_at",               # REAL
        "order_by": "created_at ASC",
    },
    "memory_fragments": {
        "key": "id",                       # INTEGER primary key
        "ts":  "created_at",               # REAL
        "order_by": "created_at ASC",
    },
}


def row_hash(table, row_dict):
    """SHA-256 of (table_name + key + sorted_json).
    Returns 64-char hex string.
    """
    key_col = SYNC_TABLES[table]["key"]
    key = str(row_dict[key_col])
    # Deterministic JSON: sort keys, no extra whitespace
    canonical = json.dumps(row_dict, sort_keys=True, ensure_ascii=False, separators=(",",":"))
    raw = f"{table}:{key}:{canonical}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_hash_tree(cursor, table):
    """Return {row_id: sha256_hex} for every row in a table."""
    info = SYNC_TABLES[table]
    key_col = info["key"]
    # Fetch all columns
    cursor.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cursor.description]
    tree = {}
    for row in cursor.fetchall():
        row_dict = dict(zip(cols, row))
        rid = str(row_dict[key_col])
        tree[rid] = row_hash(table, row_dict)
    return tree


def compute_hash_tree_batch(conn, tables=None):
    """Compute hash trees for multiple tables at once.
    Returns {table: {row_id: sha256_hex}}
    """
    if tables is None:
        tables = list(SYNC_TABLES.keys())
    cursor = conn.cursor()
    return {t: compute_hash_tree(cursor, t) for t in tables}


def diff_trees(local_tree, remote_tree):
    """Return (local_missing: [id], remote_missing: [id], conflicted: [{id, local_hash, remote_hash}]).
    
    - local_missing: rows local has that remote doesn't (push to remote)
    - remote_missing: rows remote has that local doesn't (pull from remote)
    - conflicted: rows both have but different hash (resolve by timestamp)
    """
    local_ids = set(local_tree.keys())
    remote_ids = set(remote_tree.keys())

    local_missing = list(local_ids - remote_ids)
    remote_missing = list(remote_ids - local_ids)

    shared = local_ids & remote_ids
    conflicted = []
    for rid in shared:
        if local_tree[rid] != remote_tree[rid]:
            conflicted.append({
                "id": rid,
                "local_hash": local_tree[rid],
                "remote_hash": remote_tree[rid],
            })

    return local_missing, remote_missing, conflicted


def resolve_conflicts(conn, table, conflicted_rows, remote_rows, local_rows):
    """Last-write-wins: compare timestamps, keep newer.
    Returns (applied_local_changes: [{id}], applied_remote_changes: [{id}]).
    """
    info = SYNC_TABLES[table]
    ts_col = info["ts"]
    key_col = info["key"]
    applied_local = []
    applied_remote = []

    for row in conflicted_rows:
        rid = row["id"]
        local_row = local_rows.get(rid)
        remote_row = remote_rows.get(rid)
        if not local_row or not remote_row:
            continue

        local_ts = local_row.get(ts_col, 0) or 0
        remote_ts = remote_row.get(ts_col, 0) or 0

        if remote_ts > local_ts:
            # Remote is newer → overwrite local
            _upsert_row(conn, table, remote_row, key_col)
            applied_remote.append(rid)
        elif local_ts > remote_ts:
            # Local is newer → local wins
            applied_local.append(rid)
        # Equal timestamps: prefer the one with the higher hash lexically
        elif row["local_hash"] >= row["remote_hash"]:
            applied_local.append(rid)
        else:
            _upsert_row(conn, table, remote_row, key_col)
            applied_remote.append(rid)

    return applied_local, applied_remote


def _upsert_row(conn, table, row_dict, key_col):
    """INSERT OR REPLACE a single row."""
    keys = list(row_dict.keys())
    placeholders = ",".join("?" for _ in keys)
    cols = ",".join(f'"{k}"' for k in keys)
    sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
    conn.execute(sql, [row_dict[k] for k in keys])


def fetch_rows(cursor, table, row_ids):
    """Fetch full row dicts for given IDs."""
    if not row_ids:
        return {}
    info = SYNC_TABLES[table]
    key_col = info["key"]
    # Convert ids to appropriate types
    # messages.id and memory_fragments.id are INTEGER
    sample_id = row_ids[0]
    is_int = isinstance(sample_id, int) or (isinstance(sample_id, str) and sample_id.isdigit())
    id_list = [int(rid) if is_int else rid for rid in row_ids]
    placeholders = ",".join("?" for _ in id_list)
    cursor.execute(f"SELECT * FROM {table} WHERE {key_col} IN ({placeholders})", id_list)
    cols = [d[0] for d in cursor.description]
    return {str(row_dict[key_col]): dict(zip(cols, row_dict)) for row_dict in cursor.fetchall()}


def fetch_all_rows_since(cursor, table, since_ts=0):
    """Fetch all rows updated after since_ts, ordered by timestamp."""
    info = SYNC_TABLES[table]
    ts_col = info["ts"]
    order = info["order_by"]
    cursor.execute(f"SELECT * FROM {table} WHERE {ts_col} > ? ORDER BY {order}", (since_ts,))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]
