#!/usr/bin/env python3
"""
Aliyun sync server — runs on Aliyun ECS.
REST API for multi-device chat.db + memory sync.
Hash-based incremental sync, last-write-wins conflict resolution.

Deploy:
  - Copy to Aliyun ECS (Ubuntu/Debian)
  - pip install flask
  - python sync_server.py --port 8080 --db /data/sync_master.db
  - Or: nohup python sync_server.py --port 8080 &

API:
  POST /api/v1/sync/hash_tree   ← Client sends hash tree, gets diff
  POST /api/v1/sync/push        ← Client sends changed rows
  GET  /api/v1/sync/pull        ← Client pulls remote changes
  GET  /api/v1/health           ← Health check
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

# Add parent dir so sync_protocol imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from flask import Flask, request, jsonify
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from sync.sync_protocol import (
    SYNC_TABLES,
    compute_hash_tree,
    diff_trees,
    resolve_conflicts,
    fetch_rows,
    fetch_all_rows_since,
    row_hash,
)

# ── CLI fallback (no Flask) ──────────────────────────
if not HAS_FLASK:
    import http.server
    import json as _json
    from urllib.parse import urlparse, parse_qs


class SyncMasterDB:
    """Manages the canonical sync snapshot database + per-device state."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self):
        """Create sync tracking tables."""
        c = self.conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS _sync_devices (
                device_id   TEXT PRIMARY KEY,
                last_sync_ts REAL NOT NULL DEFAULT 0,
                last_heartbeat REAL NOT NULL DEFAULT 0,
                label       TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS _sync_hash_tree (
                device_id   TEXT NOT NULL,
                table_name  TEXT NOT NULL,
                row_id      TEXT NOT NULL,
                row_hash    TEXT NOT NULL,
                updated_ts  REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (device_id, table_name, row_id)
            );
            -- Canonical snapshot: same schema as client tables
        """)
        c.commit()

    def register_device(self, device_id, label=""):
        """Register or touch a device."""
        now = time.time()
        self.conn.execute(
            "INSERT OR REPLACE INTO _sync_devices (device_id, last_sync_ts, last_heartbeat, label) VALUES (?,?,?,?)",
            (device_id, now, now, label)
        )
        self.conn.commit()

    def update_hash_tree(self, device_id, table, hash_tree):
        """Replace stored hash tree for a (device, table)."""
        now = time.time()
        self.conn.execute("DELETE FROM _sync_hash_tree WHERE device_id=? AND table_name=?", (device_id, table))
        rows = [(device_id, table, rid, h, now) for rid, h in hash_tree.items()]
        if rows:
            self.conn.executemany(
                "INSERT INTO _sync_hash_tree (device_id, table_name, row_id, row_hash, updated_ts) VALUES (?,?,?,?,?)",
                rows
            )
        self.conn.execute(
            "UPDATE _sync_devices SET last_sync_ts=?, last_heartbeat=? WHERE device_id=?",
            (now, now, device_id)
        )
        self.conn.commit()

    def get_remote_hash_tree(self, device_id, table):
        """Get the last known hash tree for a device's table."""
        rows = self.conn.execute(
            "SELECT row_id, row_hash FROM _sync_hash_tree WHERE device_id=? AND table_name=?",
            (device_id, table)
        ).fetchall()
        return {r["row_id"]: r["row_hash"] for r in rows}

    def list_devices(self):
        """List all registered devices."""
        rows = self.conn.execute(
            "SELECT device_id, last_sync_ts, last_heartbeat, label FROM _sync_devices ORDER BY last_sync_ts DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def store_canonical_rows(self, table, rows):
        """Upsert rows into canonical snapshot."""
        info = SYNC_TABLES[table]
        key_col = info["key"]
        for row in rows:
            self._upsert(table, row, key_col)
        self.conn.commit()

    def _upsert(self, table, row_dict, key_col):
        keys = list(row_dict.keys())
        placeholders = ",".join("?" for _ in keys)
        cols = ",".join(f'"{k}"' for k in keys)
        sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
        self.conn.execute(sql, [row_dict[k] for k in keys])

    def get_canonical_rows(self, table, row_ids):
        """Fetch rows from canonical snapshot by IDs."""
        if not row_ids:
            return {}
        info = SYNC_TABLES[table]
        key_col = info["key"]
        placeholders = ",".join("?" for _ in row_ids)
        rows = self.conn.execute(
            f"SELECT * FROM {table} WHERE {key_col} IN ({placeholders})", row_ids
        ).fetchall()
        cols = [d[0] for d in self.conn.description_for_table(table)]
        return {str(r[cols.index(key_col)]): dict(zip(cols, r)) for r in rows}

    def get_changes_since(self, table, since_ts):
        """Get canonical rows changed since timestamp."""
        info = SYNC_TABLES[table]
        ts_col = info["ts"]
        order = info["order_by"]
        rows = self.conn.execute(
            f"SELECT * FROM {table} WHERE {ts_col} > ? ORDER BY {order}", (since_ts,)
        ).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        self.conn.close()


# ── Flask API ────────────────────────────────────────

def create_app(db_path):
    app = Flask(__name__)
    master = SyncMasterDB(db_path)

    @app.route("/api/v1/health")
    def health():
        return jsonify({
            "status": "ok",
            "devices": len(master.list_devices()),
            "db": master.db_path,
        })

    @app.route("/api/v1/sync/hash_tree", methods=["POST"])
    def handle_hash_tree():
        """Client sends {device_id, label?, tables: {table: {row_id: hash}}}.
        Server responds with {diff: {table: {local_missing: [...], remote_missing: [...], conflicted: [...]}}, device_list: [...]}.
        """
        data = request.get_json(force=True)
        device_id = data.get("device_id", "")
        if not device_id:
            return jsonify({"error": "device_id required"}), 400

        master.register_device(device_id, data.get("label", ""))
        client_trees = data.get("tables", {})

        diffs = {}
        for table, client_tree in client_trees.items():
            if table not in SYNC_TABLES:
                continue
            remote_tree = master.get_remote_hash_tree(device_id, table)
            local_missing, remote_missing, conflicted = diff_trees(client_tree, remote_tree)

            # Store client's hash tree
            master.update_hash_tree(device_id, table, client_tree)

            diffs[table] = {
                "local_missing": local_missing,    # rows client has but server doesn't
                "remote_missing": remote_missing,  # rows server has but client doesn't
                "conflicted": conflicted,
            }

        return jsonify({
            "status": "ok",
            "diff": diffs,
            "device_list": master.list_devices(),
            "server_ts": time.time(),
        })

    @app.route("/api/v1/sync/push", methods=["POST"])
    def handle_push():
        """Client sends {device_id, table, rows: [row_dict]}.
        Server merges into canonical snapshot.
        """
        data = request.get_json(force=True)
        device_id = data.get("device_id", "")
        table = data.get("table", "")
        rows = data.get("rows", [])

        if not device_id or not table or not rows:
            return jsonify({"error": "device_id, table, rows required"}), 400
        if table not in SYNC_TABLES:
            return jsonify({"error": f"unknown table: {table}"}), 400

        # Resolve conflicts against existing canonical rows
        info = SYNC_TABLES[table]
        key_col = info["key"]
        row_ids = [str(r[key_col]) for r in rows if key_col in r]
        existing = master.get_canonical_rows(table, row_ids)

        to_store = []
        for row in rows:
            rid = str(row[key_col])
            existing_row = existing.get(rid)
            if existing_row:
                # Last-write-wins
                local_ts = row.get(info["ts"], 0) or 0
                remote_ts = existing_row.get(info["ts"], 0) or 0
                if local_ts >= remote_ts:
                    to_store.append(row)
                # else keep existing
            else:
                to_store.append(row)

        master.store_canonical_rows(table, to_store)

        return jsonify({
            "status": "ok",
            "stored": len(to_store),
            "skipped": len(rows) - len(to_store),
        })

    @app.route("/api/v1/sync/pull", methods=["GET"])
    def handle_pull():
        """Client requests changes. Query params: device_id, table, since_ts.
        Returns {rows: [row_dict]}.
        """
        device_id = request.args.get("device_id", "")
        table = request.args.get("table", "")
        since_ts = float(request.args.get("since_ts", 0))

        if not table or table not in SYNC_TABLES:
            return jsonify({"error": "invalid table"}), 400

        rows = master.get_changes_since(table, since_ts)

        if device_id:
            master.register_device(device_id)

        return jsonify({
            "status": "ok",
            "table": table,
            "rows": rows,
            "count": len(rows),
            "server_ts": time.time(),
        })

    @app.route("/api/v1/devices")
    def list_devices_api():
        return jsonify({"devices": master.list_devices()})

    return app, master


# ── HTTP Server (no Flask fallback) ─────────────────

class SyncHTTPHandler(http.server.BaseHTTPRequestHandler):
    master = None

    def _json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/v1/health":
            self._json(200, {"status": "ok", "devices": len(self.master.list_devices())})
        elif path == "/api/v1/devices":
            self._json(200, {"devices": self.master.list_devices()})
        elif path == "/api/v1/sync/pull":
            table = params.get("table", [None])[0]
            since_ts = float(params.get("since_ts", [0])[0])
            if not table or table not in SYNC_TABLES:
                self._json(400, {"error": "invalid table"})
                return
            rows = self.master.get_changes_since(table, since_ts)
            self._json(200, {"status": "ok", "table": table, "rows": rows, "count": len(rows)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/v1/sync/hash_tree":
            data = self._read_body()
            device_id = data.get("device_id", "")
            if not device_id:
                self._json(400, {"error": "device_id required"})
                return
            self.master.register_device(device_id, data.get("label", ""))
            client_trees = data.get("tables", {})

            diffs = {}
            for table, client_tree in client_trees.items():
                if table not in SYNC_TABLES:
                    continue
                remote_tree = self.master.get_remote_hash_tree(device_id, table)
                local_missing, remote_missing, conflicted = diff_trees(client_tree, remote_tree)
                self.master.update_hash_tree(device_id, table, client_tree)
                diffs[table] = {
                    "local_missing": local_missing,
                    "remote_missing": remote_missing,
                    "conflicted": conflicted,
                }

            self._json(200, {
                "status": "ok",
                "diff": diffs,
                "device_list": self.master.list_devices(),
                "server_ts": time.time(),
            })

        elif path == "/api/v1/sync/push":
            data = self._read_body()
            device_id = data.get("device_id", "")
            table = data.get("table", "")
            rows = data.get("rows", [])
            if not device_id or not table or not rows:
                self._json(400, {"error": "device_id, table, rows required"})
                return
            if table not in SYNC_TABLES:
                self._json(400, {"error": f"unknown table: {table}"})
                return

            info = SYNC_TABLES[table]
            key_col = info["key"]
            row_ids = [str(r[key_col]) for r in rows if key_col in r]
            existing = self.master.get_canonical_rows(table, row_ids)
            to_store = []
            for row in rows:
                rid = str(row[key_col])
                existing_row = existing.get(rid)
                if existing_row:
                    local_ts = row.get(info["ts"], 0) or 0
                    remote_ts = existing_row.get(info["ts"], 0) or 0
                    if local_ts >= remote_ts:
                        to_store.append(row)
                else:
                    to_store.append(row)
            self.master.store_canonical_rows(table, to_store)
            self._json(200, {"status": "ok", "stored": len(to_store), "skipped": len(rows) - len(to_store)})

        else:
            self._json(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description="全能王 Sync Server — run on Aliyun ECS")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    parser.add_argument("--db", default="sync_master.db", help="Path to sync master DB")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--no-flask", action="store_true", help="Use stdlib HTTP server instead of Flask")
    args = parser.parse_args()

    if HAS_FLASK and not args.no_flask:
        app, master = create_app(args.db)
        print(f"[sync_server] Flask server on {args.host}:{args.port}")
        print(f"[sync_server] DB: {args.db}")
        app.run(host=args.host, port=args.port, debug=False)
    else:
        master = SyncMasterDB(args.db)
        SyncHTTPHandler.master = master
        server = http.server.HTTPServer((args.host, args.port), SyncHTTPHandler)
        print(f"[sync_server] stdlib server on {args.host}:{args.port}")
        print(f"[sync_server] DB: {args.db}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        finally:
            master.close()


if __name__ == "__main__":
    main()
