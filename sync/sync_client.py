#!/usr/bin/env python3
"""
Sync client — runs on each PC, polls local chat.db, syncs with Aliyun server.

Strategy:
  - Every N seconds, compute hash tree of local chat.db
  - Send hash tree to server, receive diff
  - Push rows server is missing
  - Pull rows client is missing (conflict resolution on both sides)
  - SHA-256 per row; collision probability ~2^-256

Usage:
  python sync_client.py --server http://your-aliyun-ip:8080 --device pc-home --db /path/to/chat.db
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.sync_protocol import (
    SYNC_TABLES,
    compute_hash_tree,
    compute_hash_tree_batch,
    diff_trees,
    resolve_conflicts,
    fetch_rows,
    row_hash,
)


class SyncClient:
    """Two-way sync client. Polls local DB, pushes/pulls via HTTP to Aliyun."""

    def __init__(self, server_url, device_id, db_path, label="", poll_interval=30):
        self.server = server_url.rstrip("/")
        self.device_id = device_id
        self.label = label
        self.db_path = db_path
        self.poll_interval = poll_interval
        self._last_sync_ts = 0
        self._stats = {"push": 0, "pull": 0, "conflicts": 0}

    def _api_post(self, path, data):
        """POST JSON to API endpoint, return parsed response."""
        url = f"{self.server}{path}"
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8", errors="replace")
            print(f"[sync] HTTP {e.code} on {path}: {err_text}")
            return None
        except Exception as e:
            print(f"[sync] Connection error to {url}: {e}")
            return None

    def _api_get(self, path, params=None):
        """GET from API with query params."""
        url = f"{self.server}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url += f"?{qs}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8", errors="replace")
            print(f"[sync] HTTP {e.code} on GET {path}: {err_text}")
            return None
        except Exception as e:
            print(f"[sync] Connection error to {url}: {e}")
            return None

    def _open_db(self):
        """Open local chat.db read-only."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def sync_all(self):
        """Full sync cycle: send hash tree, push diffs, pull diffs."""
        if not self._server_available():
            print("[sync] Server unavailable, skipping cycle")
            return False

        conn = self._open_db()
        try:
            # Step 1: Compute local hash trees for all tables
            local_trees = compute_hash_tree_batch(conn)

            # Step 2: Send hash trees, get diff
            hash_body = {
                "device_id": self.device_id,
                "label": self.label,
                "tables": local_trees,
            }
            diff_resp = self._api_post("/api/v1/sync/hash_tree", hash_body)
            if not diff_resp:
                return False

            diffs = diff_resp.get("diff", {})

            # Step 3: For each table, push local_missing rows, handle conflicts
            for table, diff in diffs.items():
                local_missing = diff.get("local_missing", [])
                conflicted = diff.get("conflicted", [])

                # Push rows server doesn't have
                if local_missing:
                    cursor = conn.cursor()
                    rows_to_push = fetch_rows(cursor, table, local_missing)
                    if rows_to_push:
                        push_data = {
                            "device_id": self.device_id,
                            "table": table,
                            "rows": list(rows_to_push.values()),
                        }
                        push_resp = self._api_post("/api/v1/sync/push", push_data)
                        if push_resp:
                            self._stats["push"] += push_resp.get("stored", 0)
                            print(f"[sync] Pushed {push_resp['stored']} rows to {table}")

                # Handle conflicts (re-fetch both sides, resolve locally)
                if conflicted:
                    self._stats["conflicts"] += len(conflicted)
                    self._resolve_local_conflicts(conn, table, conflicted)

            # Step 4: Pull remote changes since last sync
            remote_missing = {}
            for table, diff in diffs.items():
                rml = diff.get("remote_missing", [])
                if rml:
                    remote_missing[table] = rml

            # Also pull any rows changed on server side
            for table in SYNC_TABLES:
                pull_resp = self._api_get("/api/v1/sync/pull", {
                    "device_id": self.device_id,
                    "table": table,
                    "since_ts": self._last_sync_ts,
                })
                if pull_resp and pull_resp.get("rows"):
                    self._apply_remote_rows(conn, table, pull_resp["rows"])
                    self._stats["pull"] += len(pull_resp["rows"])
                    print(f"[sync] Pulled {len(pull_resp['rows'])} rows from {table}")

            self._last_sync_ts = diff_resp.get("server_ts", time.time())
            conn.commit()
            return True

        finally:
            conn.close()

    def _resolve_local_conflicts(self, conn, table, conflicted):
        """Resolve conflicts: fetch both sides, apply server's version if newer."""
        info = SYNC_TABLES[table]
        ts_col = info["ts"]
        key_col = info["key"]
        conflict_ids = [c["id"] for c in conflicted]

        # Get server's version of these rows
        pull_resp = self._api_get("/api/v1/sync/pull", {
            "table": table,
            "device_id": self.device_id,
            "since_ts": 0,
        })
        if not pull_resp or not pull_resp.get("rows"):
            return

        server_rows = {str(r[key_col]): r for r in pull_resp["rows"] if key_col in r}

        cursor = conn.cursor()
        local_rows = fetch_rows(cursor, table, conflict_ids)

        for c in conflicted:
            rid = c["id"]
            local_row = local_rows.get(rid)
            server_row = server_rows.get(rid)
            if not local_row or not server_row:
                continue
            local_ts = local_row.get(ts_col, 0) or 0
            server_ts = server_row.get(ts_col, 0) or 0
            if server_ts > local_ts:
                # Overwrite local with server version (newer)
                self._upsert_local(conn, table, server_row, key_col)
                print(f"[sync]  Conflict resolved ({table}:{rid}) — server wins (ts={server_ts} > local={local_ts})")
            # else local wins — no change needed

    def _apply_remote_rows(self, conn, table, rows):
        """Upsert remote rows into local DB (server's canonical version)."""
        if not rows:
            return
        info = SYNC_TABLES[table]
        key_col = info["key"]
        for row in rows:
            self._upsert_local(conn, table, row, key_col)

    def _upsert_local(self, conn, table, row_dict, key_col):
        keys = list(row_dict.keys())
        placeholders = ",".join("?" for _ in keys)
        cols = ",".join(f'"{k}"' for k in keys)
        conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
                     [row_dict[k] for k in keys])

    def _server_available(self):
        """Quick health check."""
        result = self._api_get("/api/v1/health")
        return result is not None

    def sync_loop(self):
        """Run sync in a loop until interrupted."""
        print(f"[sync] Client '{self.device_id}' started, polling every {self.poll_interval}s")
        print(f"[sync] Server: {self.server}")
        print(f"[sync] DB: {self.db_path}")
        print(f"[sync] SHA-256 per row (collision probability: 2^-256)")
        while True:
            try:
                ok = self.sync_all()
                if ok:
                    print(f"[sync] Cycle OK | push={self._stats['push']} pull={self._stats['pull']} conflicts={self._stats['conflicts']}")
                else:
                    print("[sync] Cycle FAILED")
            except KeyboardInterrupt:
                print("\n[sync] Stopped by user")
                break
            except Exception as e:
                print(f"[sync] Error: {e}")
            time.sleep(self.poll_interval)

    def run_once(self):
        """Single sync cycle, for cron / one-shot use."""
        ok = self.sync_all()
        print(json.dumps({
            "status": "ok" if ok else "fail",
            "stats": self._stats,
            "server": self.server,
        }, ensure_ascii=False))
        return ok


def main():
    parser = argparse.ArgumentParser(description="全能王 Sync Client — runs on each PC")
    parser.add_argument("--server", required=True, help="Aliyun sync server URL (e.g. http://1.2.3.4:8080)")
    parser.add_argument("--device", required=True, help="Unique device ID (e.g. pc-home, pc-office)")
    parser.add_argument("--db", default=None, help="Path to chat.db (default: auto-detect next to script)")
    parser.add_argument("--label", default="", help="Human-readable device label")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Single sync cycle, then exit")
    args = parser.parse_args()

    # Auto-detect DB path
    db_path = args.db
    if not db_path:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "data", "chat.db")

    client = SyncClient(
        server_url=args.server,
        device_id=args.device,
        db_path=db_path,
        label=args.label,
        poll_interval=args.interval,
    )

    if args.once:
        client.run_once()
    else:
        client.sync_loop()


if __name__ == "__main__":
    main()
