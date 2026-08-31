#!/usr/bin/env python3
"""
SQLite database for chat-server — topics + messages persistence.
Replaces topics.json and provides fast, consistent message storage.
"""
import json
import os
import sqlite3
import sys
import threading
import time
import uuid

BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
# Use project root (not slot dir) for the shared database
_ROOT = os.path.dirname(os.path.dirname(BASE_DIR)) if os.path.basename(BASE_DIR) in ('a', 'b') else BASE_DIR
DATA_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else _ROOT
DB_PATH = os.path.join(DATA_DIR, "data", "chat.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新任务',
    session_path TEXT NOT NULL DEFAULT '',
    mission_path TEXT DEFAULT NULL,
    total_cost  REAL NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    last_message TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK(role IN ('user','ai','tool')),
    text        TEXT NOT NULL DEFAULT '',
    args        TEXT DEFAULT NULL,
    thinking    TEXT DEFAULT '',
    ts          REAL NOT NULL,
    created_at  REAL NOT NULL DEFAULT (CAST(strftime('%s','now') AS REAL))
);

CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_role  ON messages(topic_id, role, ts);

-- Active topic singleton (memory tables in memory/fragment_store.py)
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- v4.0 Task Execution Layer tables
CREATE TABLE IF NOT EXISTS task_instances (
    id              TEXT PRIMARY KEY,
    user_request    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planning',
    root_node_id    TEXT,
    updated_at     REAL,
    finished_at     REAL,
    version_hash    TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task_instances(status);

CREATE TABLE IF NOT EXISTS task_nodes (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    parent_id       TEXT,
    task            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    dependencies    TEXT DEFAULT '[]',
    exec_context    TEXT DEFAULT '{}',
    result          TEXT,
    version_hash    TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task_instances(id),
    FOREIGN KEY (parent_id) REFERENCES task_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_node_task ON task_nodes(task_id);
CREATE INDEX IF NOT EXISTS idx_node_status ON task_nodes(status);

CREATE TABLE IF NOT EXISTS work_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    entry_type      TEXT NOT NULL,
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
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

-- API 原始请求/响应日志已迁移到独立库 api_logs.db（见 api_logger.py），
-- 避免大 body 写入阻塞主业务 messages 表导致 database is locked。
-- Nested context compression tree (v5.0)
CREATE TABLE IF NOT EXISTS compression_tree (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id        TEXT NOT NULL,
    parent_id       INTEGER REFERENCES compression_tree(id) ON DELETE SET NULL,
    depth           INTEGER NOT NULL DEFAULT 0,
    anchor_start    INTEGER NOT NULL,
    anchor_end      INTEGER NOT NULL,
    item_count      INTEGER NOT NULL DEFAULT 0,
    summary_text    TEXT NOT NULL DEFAULT '',
    msg_db_id       INTEGER,
    created_at      REAL NOT NULL,
    FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ct_topic ON compression_tree(topic_id);
CREATE INDEX IF NOT EXISTS idx_ct_parent ON compression_tree(parent_id);
CREATE INDEX IF NOT EXISTS idx_ct_range ON compression_tree(topic_id, anchor_start, anchor_end);

-- 文件修改历史（write_file/edit_file 自动记录，供 AI 回溯）
CREATE TABLE IF NOT EXISTS file_edit_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id        TEXT NOT NULL,
    file_path       TEXT NOT NULL,              -- 绝对路径
    tool            TEXT NOT NULL,              -- 'write_file' / 'edit_file'
    turn            INTEGER NOT NULL DEFAULT 0, -- agent 循环轮次
    parent_id       INTEGER REFERENCES file_edit_history(id) ON DELETE SET NULL,
    before_hash     TEXT,                       -- 改前内容 SHA256（新建文件为 NULL）
    after_hash      TEXT NOT NULL,              -- 改后内容 SHA256
    before_content  TEXT,                       -- 改前全文快照（新建文件为 NULL）
    operation       TEXT,                       -- edit_file 的操作 JSON（write_file 为 NULL）
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feh_topic ON file_edit_history(topic_id);
CREATE INDEX IF NOT EXISTS idx_feh_path ON file_edit_history(file_path);
CREATE INDEX IF NOT EXISTS idx_feh_parent ON file_edit_history(parent_id);
"""


class Database:
    def __init__(self, path=DB_PATH):
        self.path = path
        self._local = threading.local()
        # 主线程连接 + schema 初始化（每线程独立连接，无全局锁）
        conn = self._get_conn()
        conn.executescript(SCHEMA)
        conn.commit()
        # ── Migration: add mission_path column if missing ──
        try:
            conn.execute("SELECT mission_path FROM topics LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE topics ADD COLUMN mission_path TEXT DEFAULT NULL")
        # ── Migration: add total_cost column if missing ──
        try:
            conn.execute("SELECT total_cost FROM topics LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE topics ADD COLUMN total_cost REAL NOT NULL DEFAULT 0")
            conn.commit()
        # ── Migration: add total_tokens column if missing ──
        try:
            conn.execute("SELECT total_tokens FROM topics LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE topics ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    @property
    def conn(self):
        """兼容属性：返回当前线程的连接。旧代码直接写 db.conn.execute(...)。"""
        return self._get_conn()

    def _get_conn(self):
        """获取当前线程的连接。每线程独立连接，无全局锁。

        替代之前的单连接+RLock方案。WAL模式支持多读单写：
        - 读操作完全并发（无锁竞争）
        - 写操作靠 busy_timeout + _retry 处理冲突
        """
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=True, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _retry(self, fn, max_retries=5, base_delay=0.1):
        """静默重试：处理瞬时锁冲突，对上层完全透明。

        database is locked 是 SQLite 并发写的正常现象，不该暴露给用户。
        busy_timeout=30s 已经处理大部分情况，这里作为第二道防线。
        """
        import random as _r
        for attempt in range(max_retries):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise  # 非锁相关错误，直接抛
                if attempt == max_retries - 1:
                    print(f"[db] 重试 {max_retries} 次后仍锁: {e}")
                    raise
                # 指数退避 + 抖动，避免多线程同时重试又撞一起
                delay = base_delay * (2 ** attempt) + _r.random() * 0.05
                time.sleep(delay)
            except sqlite3.IntegrityError:
                raise  # 数据完整性错误不重试
        return None  # unreachable

    def _execute(self, sql, params=None):
        conn = self._get_conn()
        return self._retry(lambda: conn.execute(sql, params) if params is not None else conn.execute(sql))

    def _executemany(self, sql, seq):
        conn = self._get_conn()
        return self._retry(lambda: conn.executemany(sql, seq))

    def _commit(self):
        conn = self._get_conn()
        self._retry(lambda: conn.commit())

    def _fetchall(self, sql, params=None):
        conn = self._get_conn()
        def _do():
            cur = conn.execute(sql, params) if params is not None else conn.execute(sql)
            return cur.fetchall()
        return self._retry(_do)

    def _fetchone(self, sql, params=None):
        conn = self._get_conn()
        def _do():
            cur = conn.execute(sql, params) if params is not None else conn.execute(sql)
            return cur.fetchone()
        return self._retry(_do)

    def _executescript(self, script):
        conn = self._get_conn()
        self._retry(lambda: conn.executescript(script))
    def create_topic(self, title="", session_path="", mission_path="", topic_id=None):
        tid = topic_id or str(uuid.uuid4())[:12]
        now = time.time()
        self._execute(
            "INSERT INTO topics (id, title, session_path, mission_path, created_at, updated_at, last_message) VALUES (?,?,?,?,?,?,?)",
            (tid, title or "新任务", session_path, mission_path or None, now, now, "")
        )
        self._commit()
        return self.get_topic(tid)

    def get_topic(self, tid):
        row = self._fetchone("SELECT * FROM topics WHERE id=?", (tid,))
        return dict(row) if row else None

    def list_topics(self):
        rows = self._fetchall("SELECT * FROM topics ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def update_topic(self, tid, **kwargs):
        allowed = {"title", "session_path", "mission_path", "last_message", "updated_at"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [tid]
        self._execute(f"UPDATE topics SET {sets} WHERE id=?", vals)
        self._commit()

    def rename_topic(self, tid, title):
        """Rename a topic by id."""
        self._execute(
            "UPDATE topics SET title=?, updated_at=? WHERE id=?",
            (title, time.time(), tid)
        )
        self._commit()


    def add_topic_cost(self, tid, cost, tokens=0):
        """Accumulate cost (USD) and tokens for a topic."""
        self._execute(
            "UPDATE topics SET total_cost = total_cost + ?, total_tokens = total_tokens + ?, updated_at = ? WHERE id = ?",
            (cost, tokens, time.time(), tid)
        )
        self._commit()

    def delete_topic(self, tid):
        self._execute("DELETE FROM messages WHERE topic_id=?", (tid,))
        self._execute("DELETE FROM topics WHERE id=?", (tid,))
        self._commit()

    def set_active_topic_id(self, tid):
        self._execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('active_topic', ?)", (tid,))
        self._commit()

    def get_active_topic_id(self):
        """Get the active topic id, or None."""
        return self.get_meta("active_topic")

    def get_meta(self, key):
        """Get a generic meta value by key, or None."""
        row = self._fetchone("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key, value):
        """Set a generic meta key-value pair."""
        self._execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self._commit()

    def set_topic_meta(self, tid, key, value):
        """Set a per-topic meta value."""
        self._execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"{key}_{tid}", value))
        self._commit()

    def get_topic_meta(self, tid, key):
        """Get a per-topic meta value."""
        row = self._fetchone("SELECT value FROM meta WHERE key=?", (f"{key}_{tid}",))
        return row["value"] if row else None
    # ── Messages ────────────────────────────────────

    def add_message(self, topic_id, role, text, args=None, thinking="", ts=None):
        if ts is None:
            ts = time.time()
        cur = self._execute(
            "INSERT INTO messages (topic_id, role, text, args, thinking, ts) VALUES (?,?,?,?,?,?)",
            (topic_id, role, text, json.dumps(args, ensure_ascii=False) if args else None, thinking, ts)
        )
        self._commit()
        msg_id = cur.lastrowid
        # 用户消息增量进对话向量索引（Block 5c: 可能关联的对话记录）
        if role == "user" and text:
            try:
                from memory.msg_vectors import index_user_message
                index_user_message(msg_id, text, ts)
            except Exception as _e:
                print(f"[db] msg_vectors index failed: {_e}")
        return msg_id

    def add_system_message(self, topic_id, text):
        """Insert a system-level message and bump topic's updated_at."""
        conn = self._get_conn()
        def _do():
            cur = conn.execute(
                "INSERT INTO messages (topic_id, role, text, args, thinking, ts) VALUES (?,?,?,?,?,?)",
                (topic_id, "ai", text, json.dumps({"system": True}), "", time.time())
            )
            msg_id = cur.lastrowid
            conn.execute("UPDATE topics SET updated_at=? WHERE id=?", (time.time(), topic_id))
            conn.commit()
            return msg_id
        return self._retry(_do)

    def mark_message_error(self, msg_id, error_msg=""):
        """Mark a message as errored (stores error info in args field)."""
        self._execute(
            "UPDATE messages SET args=? WHERE id=?",
            (json.dumps({"error": True, "error_msg": error_msg[:300]}, ensure_ascii=False), msg_id)
        )
        self._commit()

    def update_message_text(self, msg_id, text):
        """Update a message's text field (used for persisting compressed text)."""
        self._execute("UPDATE messages SET text=? WHERE id=?", (text, msg_id))
        self._commit()

    def update_message_thinking(self, msg_id, thinking):
        """Update a message's thinking field."""
        self._execute("UPDATE messages SET thinking=? WHERE id=?", (thinking, msg_id))
        self._commit()

    def add_messages_batch(self, rows):
        """rows: list of (topic_id, role, text, args, thinking, ts)"""
        now = time.time()
        values = []
        for r in rows:
            topic_id, role, text, args, thinking, ts = r
            values.append((
                topic_id, role, text,
                json.dumps(args, ensure_ascii=False) if args else None,
                thinking, ts or now
            ))
        self._executemany(
            "INSERT INTO messages (topic_id, role, text, args, thinking, ts) VALUES (?,?,?,?,?,?)",
            values
        )
        self._commit()

    def get_messages(self, topic_id, limit=None, offset=0, skip_hidden=False, skip_process=False):
        # skip_hidden: 过滤 args 含 "hidden" 的消息（organize_context 批量整理的）
        # skip_process: 过滤 args 含 "process" 的 AI 消息（工具轮动作叙述，非最终回复）
        #   过程叙述仍可通过 read_topic_messages 工具直接 SQL 翻阅，只是不进 AI 上下文和前端历史
        filters = []
        if skip_hidden:
            filters.append("(args NOT LIKE '%\"hidden\"%' OR args IS NULL)")
        if skip_process:
            # 只过滤 AI 角色的过程叙述，tool 角色的 args 也可能含 process 字样但不影响
            filters.append("(role != 'ai' OR args NOT LIKE '%\"process\"%' OR args IS NULL)")
        extra_filter = ("AND " + " AND ".join(filters)) if filters else ""
        if limit:
            rows = self._fetchall(
                f"SELECT id, topic_id, role, text, args, thinking, ts FROM messages "
                f"WHERE topic_id=? {extra_filter} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (topic_id, limit, offset)
            )
            rows.reverse()
        else:
            rows = self._fetchall(
                f"SELECT id, topic_id, role, text, args, thinking, ts FROM messages "
                f"WHERE topic_id=? {extra_filter} ORDER BY ts, id",
                (topic_id,)
            )
        result = []
        for r in rows:
            ts = r["ts"]
            if ts and ts < 1e14:
                ts = ts * 1000
            m = {"role": r["role"], "text": r["text"], "timestamp": ts, "id": r["id"]}
            if r["role"] == "tool":
                m["type"] = "tool"
                m["name"] = r["text"]
            try:
                m["args"] = json.loads(r["args"]) if r["args"] else None
            except json.JSONDecodeError:
                m["args"] = None
            if r["thinking"]:
                m["thinking"] = r["thinking"]
            result.append(m)
        return result

    def message_count(self, topic_id):
        row = self._fetchone("SELECT COUNT(*) as cnt FROM messages WHERE topic_id=?", (topic_id,))
        return row["cnt"]

    def set_working_state(self, topic_id, state):
        self._execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"working_{topic_id}", json.dumps(state, ensure_ascii=False))
        )
        self._commit()
    def get_working_state(self, topic_id):
        """Get persisted working state for a topic, or None."""
        row = self._fetchone("SELECT value FROM meta WHERE key=?", (f"working_{topic_id}",))
        if row:
            try:
                return json.loads(row["value"])
            except json.JSONDecodeError:
                return None
        return None

    def clear_working_state(self, topic_id):
        """Clear persisted working state for a topic."""
        self._execute("DELETE FROM meta WHERE key=?", (f"working_{topic_id}",))
        self._commit()

    # ── Migration ───────────────────────────────────

    @staticmethod
    def migrate_from_json(json_path, db, bridge=None):
        """Import topics from topics.json into DB. Returns count of migrated topics."""
        if not os.path.exists(json_path):
            return 0

        with open(json_path, "r", encoding="utf-8") as f:
            reg = json.load(f)

        migrated = 0
        topics_data = reg.get("topics", {})
        for tid, t in topics_data.items():
            if db.get_topic(tid):
                continue  # already in DB
            db.create_topic(
                title=t.get("title", "新任务"),
                session_path=t.get("session_path", ""),
                topic_id=tid,
            )
            # Preserve original timestamps
            db._execute(
                "UPDATE topics SET created_at=?, updated_at=? WHERE id=?",
                (t.get("created_at", time.time()), t.get("updated_at", time.time()), tid)
            )
            # Preserve last_message
            if t.get("last_message"):
                db._execute(
                    "UPDATE topics SET last_message=? WHERE id=?",
                    (t["last_message"], tid)
                )
            db._commit()
            migrated += 1

        # Restore active topic
        active = reg.get("active")
        if active and db.get_topic(active):
            db.set_active_topic_id(active)
        # Backfill messages from session files if bridge is available
        if bridge:
            for tid in topics_data:
                t = topics_data[tid]
                sp = t.get("session_path", "")
                if sp and os.path.exists(sp):
                    Database._sync_session_to_db(db, tid, sp)

        return migrated

    @staticmethod
    def _sync_session_to_db(db, tid, session_path):
        """Sync messages from OMP session file into DB. Position-based: only appends new messages beyond what DB already has."""
        msgs = Database._read_session_file(session_path)
        if not msgs:
            return
        # Count existing DB messages for this topic — use as offset into session file
        db_count = db.message_count(tid)
        if db_count >= len(msgs):
            return  # DB already has all messages
        new_msgs = msgs[db_count:]  # only take messages DB doesn't have yet
        rows = []
        for m in new_msgs:
            role = m.get("role", "")
            ts = m.get("timestamp", time.time())
            if role == "user":
                rows.append((tid, "user", m.get("text", ""), None, "", ts))
            elif role == "ai":
                rows.append((tid, "ai", m.get("text", ""), None, m.get("thinking", ""), ts))
        if rows:
            db.add_messages_batch(rows)

    @staticmethod
    def _read_session_file(session_path):
        """Parse OMP session .jsonl file into message list."""
        msgs = []
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "message":
                        continue
                    msg = entry.get("message", {})
                    role = msg.get("role", "")
                    if role not in ("user", "assistant"):
                        continue
                    content = msg.get("content", [])
                    text = ""
                    thinking = ""
                    for c in content:
                        if c.get("type") == "text":
                            text += c.get("text", "")
                        elif c.get("type") == "thinking":
                            thinking += c.get("thinking", "")
                    if not text and not thinking:
                        continue
                    ts = msg.get("timestamp") or entry.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            from datetime import datetime
                            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = time.time()
                    msgs.append({
                        "role": "user" if role == "user" else "ai",
                        "text": text,
                        "thinking": thinking,
                        "timestamp": ts or time.time(),
                    })
        except Exception:
            pass
        return msgs


# ── Global singleton ────────────────────────────────
_db = None


def get_db(path=DB_PATH):
    global _db
    if _db is None:
        _db = Database(path)
    return _db
