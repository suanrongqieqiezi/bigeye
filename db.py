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
# 失败事件卡（模型侧中断）的固定开场白标记 —— 写入与读取判据共用这一个词，避免两处各写一遍。
# 写入：server.py 落 ⚠️ 卡时打 args.kind='error'；读取：下面 get_messages 给**老数据**补一个读时视图
# （2026-10-23 之前落的卡没有 kind 标记，库里存量 70+ 条，不能靠改 12GB 主库来补）。
ERR_CARD_TAG = "本轮中断"
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

-- 消息反馈（喜欢 / 不喜欢 / 收藏，2026-10-23）
--  为什么要落库：这三个按钮最初只写 localStorage —— AI 看不到、换设备就丢、
--  也没有任何回顾入口（用户原话："那个按钮有实际用途不？"）。
--  like / dislike 互斥（写入时删另一面），fav 独立。
CREATE TABLE IF NOT EXISTS message_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    TEXT NOT NULL,
    msg_id      INTEGER NOT NULL,
    kind        TEXT NOT NULL,                 -- like / dislike / fav
    snippet     TEXT DEFAULT '',               -- 消息摘要（列表展示用，不回查 12GB 的消息表）
    topic_title TEXT DEFAULT '',               -- 任务标题快照（任务删了也还看得懂）
    created_at  REAL NOT NULL,
    UNIQUE(topic_id, msg_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_msgfb_kind  ON message_feedback(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_msgfb_topic ON message_feedback(topic_id);
"""


# ─────────────────────────────────────────────────────────────
# 回收站：删除任务的快照（追加式 jsonl）
#   delete_topic 写 → 回收站面板读 → /api/recycle/restore 倒回 → /clear 清空
# 三个动作共用一个路径常量，别再各算一份（2026-09-23 之前面板读的是
# data/trash 目录，而没有任何代码往那儿写，所以回收站永远是空的）。
# ─────────────────────────────────────────────────────────────
BACKUP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                           "deleted_topics_backup.jsonl")
# 追加（delete_topic）与重写（take/clear）必须互斥：否则"边删边恢复"时
# take 的「读→重写」会把刚 append 的记录一起冲掉，那条删除就永久没有备份了。
_RECYCLE_LOCK = threading.Lock()


def _recycle_key(rec):
    """快照的稳定标识（列表给前端 → 恢复时原样带回）。不含行号，删别的行不会让它失效。"""
    try:
        ms = int(float(rec.get("deleted_at") or 0) * 1000)
    except Exception:
        ms = 0
    tid = (rec.get("topic") or {}).get("id") or "?"
    return f"{ms}-{tid}"


def _recycle_lines():
    """返回 [(原始行, rec 或 None), ...]。解析不了的行必须原样留着（重写时不能丢）。"""
    out = []
    try:
        if not os.path.isfile(BACKUP_PATH):
            return out
        with open(BACKUP_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.rstrip("\n")
                if not s.strip():
                    continue
                try:
                    rec = json.loads(s)
                except Exception:
                    rec = None
                out.append((s, rec if isinstance(rec, dict) else None))
    except Exception:
        return []
    return out


def _recycle_write(lines):
    """原子写回（临时文件 + replace）：写一半崩掉也不会把回收站弄没。"""
    os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
    tmp = BACKUP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in lines:
            f.write(s + "\n")
    os.replace(tmp, BACKUP_PATH)


def recycle_items():
    """回收站列表（新 → 旧）。dir_name 即 /api/recycle/restore 要带回来的键。"""
    items = []
    for _, rec in _recycle_lines():
        if rec is None:
            continue
        topic = rec.get("topic") or {}
        try:
            dt = float(rec.get("deleted_at") or 0)
        except Exception:
            dt = 0.0
        items.append({
            "dir_name": _recycle_key(rec),
            "topic_id": topic.get("id") or "",
            "title": topic.get("title") or "未知任务",
            "deleted_ts": dt,
            # 紧凑格式（老前端按 15 位切片格式化），新前端优先用 deleted_ts
            "deleted_at": time.strftime("%Y%m%d %H%M%S", time.localtime(dt)) if dt else "",
            "msg_count": len(rec.get("messages") or []),
        })
    items.sort(key=lambda x: -(x.get("deleted_ts") or 0))
    return items


def recycle_take(key):
    """取出并移除该快照。找不到、或写回失败 → None（宁可让用户重试，也不能
    「取走了却没从回收站删掉」导致同一条被恢复两次）。"""
    with _RECYCLE_LOCK:
        hit, keep = None, []
        for s, rec in _recycle_lines():
            if hit is None and rec is not None and _recycle_key(rec) == key:
                hit = rec
                continue
            keep.append(s)
        if hit is None:
            return None
        try:
            _recycle_write(keep)
        except Exception:
            return None
        return hit


def recycle_put(rec):
    """把一条快照追加回回收站（恢复失败时的回滚，别让用户白点一次就永久丢记录）。"""
    with _RECYCLE_LOCK:
        os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
        with open(BACKUP_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recycle_clear():
    """清空回收站，返回清掉的条数。"""
    with _RECYCLE_LOCK:
        n = sum(1 for _, rec in _recycle_lines() if rec is not None)
        _recycle_write([])
        return n


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

    def first_user_messages(self, tids, per_topic=5):
        """批量取每个话题的头几条用户消息（只用于未命名任务的标题兜底显示）。

        返回 {tid: [text, ...]}（按 id 升序，最多 per_topic 条）。调用方需要跳过
        纯附件/系统提醒这类不能当标题的文本。只读、不改库 —— 标题兜底必须是
        纯显示层行为。实测 76 个 topic 一趟 ~1ms，不会拖慢 /api/topics。
        """
        tids = [t for t in (tids or []) if t]
        if not tids:
            return {}
        out = {}
        n = max(1, int(per_topic or 5))
        # SQLite 变量上限（默认 999），分批防炸
        for i in range(0, len(tids), 400):
            chunk = tids[i:i + 400]
            ph = ",".join("?" * len(chunk))
            rows = self._fetchall(
                "SELECT topic_id, text FROM messages "
                " WHERE topic_id IN (%s) AND role='user' "
                " AND text IS NOT NULL AND text!='' ORDER BY id" % ph,
                tuple(chunk),
            )
            for r in rows:
                lst = out.setdefault(r["topic_id"], [])
                if len(lst) < n:
                    lst.append(r["text"])
        return out
    # ── 全局搜索：任务标题/摘要 + 历史对话正文 ─────────
    @staticmethod
    def _snippet(text, q, radius=46):
        """截取命中词附近的一段上下文，压掉换行，便于前端单行展示。"""
        s = " ".join(str(text or "").split())
        if not s:
            return ""
        i = s.lower().find((q or "").lower())
        if i < 0:
            return s[: radius * 2] + ("…" if len(s) > radius * 2 else "")
        a = max(0, i - radius)
        b = min(len(s), i + len(q) + radius)
        return ("…" if a > 0 else "") + s[a:b] + ("…" if b < len(s) else "")

    def search(self, query, limit=60, per_topic=4):
        """跨任务搜索：标题、摘要、user/ai 消息正文。

        返回命中任务列表（按最近活跃排序），每项含 title_match / summary_match
        标记与最多 per_topic 条消息片段。空查询返回 []。
        """
        q = (query or "").strip()
        if not q:
            return []
        like = "%" + q + "%"
        limit = max(1, min(300, int(limit or 60)))
        hits = {}
        order = []

        def _slot(tid, title, updated_at, created_at, last_message):
            h = hits.get(tid)
            if h is None:
                h = {
                    "id": tid,
                    "title": title or "新任务",
                    "last_message": last_message or "",
                    "updated_at": updated_at or 0,
                    "created_at": created_at or 0,
                    "title_match": False,
                    "summary_match": False,
                    "messages": [],
                }
                hits[tid] = h
                order.append(tid)
            return h

        # 1) 标题 / 摘要命中（任务级）
        try:
            trows = self._fetchall(
                "SELECT id, title, last_message, updated_at, created_at FROM topics "
                "WHERE title LIKE ? OR last_message LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (like, like, limit),
            )
        except Exception:
            trows = []
        for r in trows:
            h = _slot(r["id"], r["title"], r["updated_at"], r["created_at"], r["last_message"])
            if q.lower() in str(r["title"] or "").lower():
                h["title_match"] = True
            if q.lower() in str(r["last_message"] or "").lower():
                h["summary_match"] = True

        # 2) 对话正文命中（排除整理用的 hidden 与工具过程叙述 process）
        try:
            mrows = self._fetchall(
                "SELECT m.id AS mid, m.topic_id AS tid, m.role AS role, m.text AS text, "
                "       m.ts AS ts, t.title AS title, t.updated_at AS updated_at, "
                "       t.created_at AS created_at, t.last_message AS last_message "
                "FROM messages m JOIN topics t ON t.id = m.topic_id "
                "WHERE m.role IN ('user','ai') AND m.text LIKE ? "
                "  AND (m.args IS NULL OR (m.args NOT LIKE '%\"hidden\"%' AND m.args NOT LIKE '%\"process\"%')) "
                "ORDER BY m.ts DESC, m.id DESC LIMIT ?",
                (like, limit * 8),
            )
        except Exception:
            mrows = []
        for r in mrows:
            h = _slot(r["tid"], r["title"], r["updated_at"], r["created_at"], r["last_message"])
            if len(h["messages"]) >= per_topic:
                continue
            ts = r["ts"] or 0
            if ts and ts < 1e14:
                ts = ts * 1000
            h["messages"].append({
                "id": r["mid"],
                "role": r["role"],
                "ts": ts,
                "snippet": self._snippet(r["text"], q),
            })

        out = [hits[t] for t in order]
        out.sort(key=lambda h: h.get("updated_at") or 0, reverse=True)
        return out[:limit]

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


    def settle_topic_usage(self, tid, settlement_id, cost, tokens):
        """Atomically settle one usage event once, including global totals."""
        conn = self._get_conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM topics WHERE id=?', (tid,)).fetchone():
                raise ValueError('Unknown task')
            cur = conn.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                               ('usage_settlement:' + settlement_id, tid))
            if not cur.rowcount:
                conn.rollback()
                return False
            conn.execute('UPDATE topics SET total_cost=total_cost+?, total_tokens=total_tokens+? WHERE id=?',
                         (cost, tokens, tid))
            for key, amount in [('total_cost_cny', cost), ('total_tokens', tokens)]:
                conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(COALESCE(value,'0') AS REAL)+?",
                             (key, str(amount), amount))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    def add_topic_cost(self, tid, cost, tokens=0):
        """Accumulate cost (USD) and tokens for a topic."""
        self._execute(
            "UPDATE topics SET total_cost = total_cost + ?, total_tokens = total_tokens + ?, updated_at = ? WHERE id = ?",
            (cost, tokens, time.time(), tid)
        )
        self._commit()

    def delete_topic(self, tid):
        # ── 删除前备份到 jsonl 回收站（追加式）──
        # SQLite 的 DELETE 是硬删，一旦 VACUUM/页复用就再也找不回来。
        # 2026-09-19 真实事故：按标题批量删"新任务"把用户 49 个真实任务误删，
        # 靠 freelist 页挖掘才救回 2728 条消息（部分 user 消息永久丢失）。
        # 有了这层，任何误删都能原样倒回来。
        try:
            topic = self._fetchone("SELECT * FROM topics WHERE id=?", (tid,))
            msgs = self._fetchall("SELECT * FROM messages WHERE topic_id=?", (tid,))
            if topic or msgs:
                rec = {
                    "deleted_at": time.time(),
                    "topic": dict(topic) if topic else None,
                    "messages": [dict(m) for m in msgs],
                }
                os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
                with _RECYCLE_LOCK:
                    with open(BACKUP_PATH, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 备份失败不阻塞删除（与旧行为一致），但正常情况都会成功
        self._execute("DELETE FROM messages WHERE topic_id=?", (tid,))
        self._execute("DELETE FROM topics WHERE id=?", (tid,))
        self._commit()

    def restore_topic_snapshot(self, topic, messages):
        """delete_topic 的逆操作：把回收站快照原样倒回库。返回恢复出的 topic_id。

        **尽量保留原 message.id**：SQLite AUTOINCREMENT 不复用 id，所以被删的旧 id 必然空闲，
        而 user_msg_vectors 是按 message_id 建的表 → 保住 id 就等于恢复后语义检索不丢。
        若 id 区间已被占用（换过库文件之类的极端情况），整体退回自增 id 重插。
        """
        t = topic or {}
        tid = t.get("id")
        if not tid:
            return None
        if self._fetchone("SELECT id FROM topics WHERE id=?", (tid,)):
            return tid  # 已在库里（重复恢复）→ 视为成功，不再插一遍
        now = time.time()
        self._execute(
            "INSERT INTO topics (id, title, session_path, mission_path, created_at,"
            " updated_at, last_message, total_cost, total_tokens) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, t.get("title") or "新任务", t.get("session_path") or "", t.get("mission_path"),
             t.get("created_at") or now,
             now,  # updated_at 取现在：恢复后任务直接浮到列表最上面，不然沉在老位置找不到
             t.get("last_message") or "", t.get("total_cost") or 0, t.get("total_tokens") or 0))

        msgs = [m for m in (messages or []) if isinstance(m, dict)]
        rows = [(tid, m.get("role") or "ai", m.get("text") or "", m.get("args"),
                 m.get("thinking") or "", m.get("ts") or now,
                 m.get("created_at") or m.get("ts") or now) for m in msgs]
        ids = [m.get("id") for m in msgs]
        keep_ids = bool(msgs) and all(isinstance(i, int) and i > 0 for i in ids)
        if keep_ids:
            chk = self._fetchone("SELECT COUNT(*) AS n FROM messages WHERE id BETWEEN ? AND ?",
                                 (min(ids), max(ids)))
            if chk and (chk["n"] or 0) > 0:
                keep_ids = False  # 区间被占 → 别硬塞，退回自增（向量按 id 的关联会失效，仅此一种情况）
        if keep_ids:
            for mid, r in zip(ids, rows):
                self._execute(
                    "INSERT INTO messages (id, topic_id, role, text, args, thinking, ts, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", (mid,) + r)
        else:
            self._executemany(
                "INSERT INTO messages (topic_id, role, text, args, thinking, ts, created_at)"
                " VALUES (?,?,?,?,?,?,?)", rows)
        self._commit()
        return tid

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

    # ── 消息反馈：喜欢 / 不喜欢 / 收藏（2026-10-23）───────────────────
    # 语义：like / dislike 互斥（写一面就删另一面），fav 与之独立（可同时收藏+点赞）。
    # on=False 表示取消，只删自己那一面，不动另一面。
    FEEDBACK_KINDS = ("like", "dislike", "fav")

    def set_message_feedback(self, topic_id, msg_id, kind, on=True, snippet="", topic_title=""):
        kind = (kind or "").strip()
        if kind not in self.FEEDBACK_KINDS:
            raise ValueError("kind must be one of like/dislike/fav")
        if not topic_id:
            raise ValueError("topic_id required")
        try:
            mid = int(msg_id)
        except Exception:
            raise ValueError("msg_id must be an integer")
        if not mid:
            raise ValueError("msg_id required")
        if not on:
            self._execute("DELETE FROM message_feedback WHERE topic_id=? AND msg_id=? AND kind=?",
                          (topic_id, mid, kind))
            self._commit()
            return True
        if kind in ("like", "dislike"):
            self._execute("DELETE FROM message_feedback WHERE topic_id=? AND msg_id=? AND kind IN ('like','dislike')",
                          (topic_id, mid))
        self._execute(
            "INSERT INTO message_feedback (topic_id, msg_id, kind, snippet, topic_title, created_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(topic_id,msg_id,kind) DO UPDATE SET "
            "snippet=excluded.snippet, topic_title=excluded.topic_title, created_at=excluded.created_at",
            (topic_id, mid, kind, (snippet or "")[:300], (topic_title or "")[:120], time.time())
        )
        self._commit()
        return True

    def get_topic_feedback(self, topic_id):
        """{ "<msg_id>": {"like":1} | {"dislike":1} | {"fav":1} } —— 前端渲染选中态用。
        一次拿全任务，避免每条消息各发一次请求（一屏可能 100+ 条）。"""
        out = {}
        rows = self._fetchall("SELECT msg_id, kind FROM message_feedback WHERE topic_id=?", (topic_id,)) or []
        for r in rows:
            out.setdefault(str(r["msg_id"]), {})[r["kind"]] = 1
        return out

    def list_message_feedback(self, kind=None, limit=200):
        """回顾列表（新 → 旧）。带上任务当前标题，任务已删则退回快照标题。"""
        sql = ("SELECT f.topic_id, f.msg_id, f.kind, f.snippet, f.topic_title, f.created_at, "
               "COALESCE(NULLIF(t.title,''), f.topic_title, '') AS cur_title, "
               "CASE WHEN t.id IS NULL THEN 0 ELSE 1 END AS topic_alive "
               "FROM message_feedback f LEFT JOIN topics t ON t.id=f.topic_id")
        params = []
        if kind:
            sql += " WHERE f.kind=?"
            params.append(kind)
        sql += " ORDER BY f.created_at DESC LIMIT ?"
        params.append(int(limit or 200))
        rows = self._fetchall(sql, tuple(params)) or []
        return [dict(r) for r in rows]

    def message_feedback_counts(self):
        """{like:N, dislike:N, fav:N} —— 设置页 tab 上的数字。"""
        out = {"like": 0, "dislike": 0, "fav": 0}
        rows = self._fetchall("SELECT kind, COUNT(*) AS c FROM message_feedback GROUP BY kind") or []
        for r in rows:
            if r["kind"] in out:
                out[r["kind"]] = int(r["c"])
        return out

    def list_meta_keys(self, prefix=""):
        """列出 meta 键（可按前缀过滤），孤儿暂存扫描用。"""
        if prefix:
            rows = self._fetchall("SELECT key FROM meta WHERE key LIKE ?", (prefix + "%",))
        else:
            rows = self._fetchall("SELECT key FROM meta", ())
        return [r["key"] for r in rows]

    def set_topic_meta(self, tid, key, value):
        """Set a per-topic meta value."""
        self._execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"{key}_{tid}", value))
        self._commit()

    def get_topic_meta(self, tid, key):
        """Get a per-topic meta value."""
        row = self._fetchone("SELECT value FROM meta WHERE key=?", (f"{key}_{tid}",))
        return row["value"] if row else None

    def delete_topic_meta(self, tid, key):
        """Delete a per-topic meta key. 用于清掉旧机器残留的失效绝对路径。"""
        self._execute("DELETE FROM meta WHERE key=?", (f"{key}_{tid}",))
        self._commit()

    def normalize_workspace_metas(self, topic_id=None, auto_fix=True):
        """workspace 路径治理（幂等）：把 meta 里的 workspace 值规范化。

        原则：标准形态 data/missions/{tid}/workspace 不存 meta，删除键让运行时
        按 tid 现算（数据自描述，永不失效）；特殊工作区存相对路径。
        旧机器残留的失效绝对路径：若 data/missions/{tid}/workspace 实际存在，
        auto_fix=True 时删除键改走标准形态；否则跳过（保留原值由人工处理）。

        返回 {'fixed': [...], 'skipped': [...], 'kept': n}。
        """
        import os as _os
        fixed, skipped = [], []
        kept = 0
        keys = self.list_meta_keys(prefix="workspace_")
        for k in keys:
            tid = k[len("workspace_"):]
            if topic_id and tid != topic_id:
                continue
            v = self.get_topic_meta(tid, "workspace")
            if v is None:
                continue
            # 项目根 = db.py 所在目录
            root = _os.path.dirname(_os.path.abspath(__file__))
            std_rel_norm = _os.path.normpath(_os.path.join("data", "missions", tid, "workspace"))
            std_abs_cur = _os.path.normpath(_os.path.join(root, "data", "missions", tid, "workspace"))
            v_norm = _os.path.normpath(v) if v else ""
            if _os.path.isabs(v):
                if v_norm.lower() == std_abs_cur.lower():
                    # 标准绝对路径：删键，改由运行时按 tid 现算
                    if auto_fix:
                        self.delete_topic_meta(tid, "workspace")
                        fixed.append((tid, "std-abs->runtime", v))
                    else:
                        kept += 1
                elif _os.path.isdir(v):
                    kept += 1  # 存在的特殊绝对路径（用户自定）保留
                else:
                    if _os.path.isdir(std_abs_cur) and auto_fix:
                        self.delete_topic_meta(tid, "workspace")
                        fixed.append((tid, "broken-abs->runtime", v))
                    elif auto_fix:
                        skipped.append((tid, "broken-abs-no-dir", v))
                    else:
                        kept += 1
            else:
                if v_norm == std_rel_norm:
                    if auto_fix:
                        self.delete_topic_meta(tid, "workspace")
                        fixed.append((tid, "std-rel->runtime", v))
                    else:
                        kept += 1
                else:
                    resolved = _os.path.join(root, v)
                    if _os.path.isdir(resolved) or _os.path.isdir(v):
                        kept += 1  # 可解析的特殊相对路径保留
                    elif auto_fix:
                        skipped.append((tid, "unresolvable-rel", v))
                    else:
                        kept += 1
        return {"fixed": fixed, "skipped": skipped, "kept": kept}
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

    def get_messages(self, topic_id, limit=None, offset=0, skip_hidden=False, skip_process=False,
                     exclude_thinking=False, after_ts=None, conclusions_only=False):
        # skip_hidden: 过滤 args 含 "hidden" 的消息（organize_context 批量整理的）
        # skip_process: 过滤 args 含 "process" 的 AI 消息（工具轮动作叙述，非最终回复）
        #   过程叙述仍可通过 read_topic_messages 工具直接 SQL 翻阅，只是不进 AI 上下文和前端历史
        # exclude_thinking: 只回 has_thinking 标记，不回 thinking 正文（正文体积可达数 MB，
        #   前端首屏/轮询全带上会把"最终答复"拖到最后才出现；改为点开过程泡时按需拉）
        # after_ts: 只要该时间戳(秒)之后的消息 —— 前端增量刷新用，避免每次全量重传历史
        # conclusions_only: 只要"人话"——用户消息 + AI 的最终回复，剔掉工具调用。
        #   前端"结论优先"首屏用：实测同一任务 5487 条原始记录里结论仅 118 条(48KB)，
        #   其余 5369 条是工具调用与工具轮叙述(过程明细 3.4MB)，全量传会让结论迟迟不出现。
        #   被剔掉的过程由 get_message_sequence 算成"折叠占位"，点开时经 get_proc_range 按需拉。
        filters = []
        fargs = []
        if skip_hidden:
            filters.append("(args NOT LIKE '%\"hidden\"%' OR args IS NULL)")
        if skip_process:
            # 只过滤 AI 角色的过程叙述，tool 角色的 args 也可能含 process 字样但不影响
            filters.append("(role != 'ai' OR args NOT LIKE '%\"process\"%' OR args IS NULL)")
        if conclusions_only:
            filters.append("role != 'tool'")
        if after_ts is not None:
            try:
                _ts = float(after_ts)
                if _ts > 1e12:   # 前端可能传毫秒 → 归一到库内单位(秒)
                    _ts /= 1000.0
                filters.append("ts >= ?")
                fargs.append(_ts)
            except (TypeError, ValueError):
                pass
        extra_filter = ("AND " + " AND ".join(filters)) if filters else ""
        think_col = ("CASE WHEN thinking IS NOT NULL AND thinking != '' THEN 1 ELSE 0 END AS has_thinking"
                     if exclude_thinking else "thinking")
        sel = f"SELECT id, topic_id, role, text, args, {think_col}, ts FROM messages WHERE topic_id=? {extra_filter}"
        if limit:
            rows = self._fetchall(
                sel + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                tuple([topic_id] + fargs + [limit, offset])
            )
            rows.reverse()
        else:
            rows = self._fetchall(sel + " ORDER BY ts, id", tuple([topic_id] + fargs))
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
            # 老失败卡兼容：2026-10-23 前的「⚠️ 本轮中断」卡没有 kind 标记（存量 70+ 条）。
            # 只在读出时补一层视图、不写库（主库 12GB，不值得为几十行开一次写事务）：
            # 判据是这张卡固定的开场白 ERR_CARD_TAG。补上后前端折成红条、组装 AI 上下文时压成一句提示。
            if r["role"] == "ai":
                _head = (r["text"] or "")[:40].lstrip()
                if _head.startswith("⚠") and ERR_CARD_TAG in _head:
                    _a = dict(m.get("args") or {})
                    if not _a.get("kind"):
                        _a["kind"] = "error"
                        _a.setdefault("error_msg", ERR_CARD_TAG + "：模型侧持续不可用，已停止。")
                    m["args"] = _a
            if exclude_thinking:
                if r["has_thinking"]:
                    m["has_thinking"] = True
            elif r["thinking"]:
                m["thinking"] = r["thinking"]
            result.append(m)
        return result

    def get_thinking_by_ids(self, topic_id, ids):
        """按需取指定消息的 thinking 正文（前端展开过程泡时才调）。返回 {id字符串: 正文}"""
        if not ids:
            return {}
        ids = [int(i) for i in ids][:200]
        ph = ",".join("?" * len(ids))
        rows = self._fetchall(
            f"SELECT id, thinking FROM messages WHERE topic_id=? AND id IN ({ph})",
            tuple([topic_id] + ids)
        )
        return {str(r["id"]): (r["thinking"] or "") for r in rows}

    # ── 结论视图支持（前端"结论优先、过程懒加载"）────────────────────────
    # 判定必须与 get_messages(skip_process=True, conclusions_only=True) 完全一致：
    #   结论 = 非 tool 且（非 ai 或 args 不含 "process"）
    _CONCL_CASE = ("CASE WHEN role != 'tool' AND (role != 'ai' OR args IS NULL "
                   "OR args NOT LIKE '%\"process\"%') THEN 1 ELSE 0 END")

    def get_message_sequence(self, topic_id):
        """轻量全序列投影：[{id, c(是否结论), ts, tn(工具名)}]，不取正文只取标志位/工具名。
        前端结论视图据此把"两条结论之间夹着的过程"折成一个占位（见 /api/messages?conclusions=1）。
        tn 只对 role='tool' 有值。用途：占位上显示"最后一步是什么"——用户关心的是
        "他到哪一步了/卡在哪个工具上"，而不是光一个步数。

        ⚠️ 工具名必须从 args 里取（`json_extract(args,'$.name')`），不能取 text：
        tool 行的 text 是**工具返回结果**（实测单条可达数 KB 的 JSON），
        取它会让 3000 条工具行一次读出几 MB，且名字位显示成一大坨结果 JSON。
        args 的形状是 {"tool_call_id":..., "name":"bash", "turn":N}。
        """
        rows = self._fetchall(
            "SELECT id, ts, CASE WHEN role='tool' "
            "THEN json_extract(args,'$.name') ELSE NULL END AS tn, "
            + self._CONCL_CASE + " AS c FROM messages "
            "WHERE topic_id=? ORDER BY ts, id", (topic_id,))
        out = []
        for r in rows:
            ts = r["ts"]
            if ts and ts < 1e14:
                ts = ts * 1000
            out.append({"id": r["id"], "c": int(r["c"] or 0), "ts": ts, "tn": r["tn"]})
        return out

    def get_topic_tail(self, topic_id):
        """尾部探针：前端每 3 秒问一次"有没有新东西 / 最后一步是什么 / 忙不忙"。

        为什么需要它：旧实现里界面的更新全靠"发消息的那条链路"和两条粗粒度定时器
        （6s 自动同步 + 8s 任务列表）。用户原话是"他也不动，非要我切来切去才动一下"——
        因为切任务会强制走一次拉取，而原地干等没有任何东西会来刷新。

        全部走 (topic_id, ts) 索引的倒序 seek，不取正文：不返回消息内容，
        只回 4 个判据 + 最后一步工具名。判据用 id 而不是 ts：id 单调递增，
        不会因时钟回拨/同秒并列而误判"没变化"。

        返回：
          last_id / last_ts      最后一条消息（任何角色）
          count                  该任务消息总数
          last_step / last_step_ts   最后一个工具调用的名字与时刻（"他做到哪一步了"）
          last_concl_id              最后一条"结论"（人话）的 id
        """
        out = {"last_id": 0, "last_ts": 0, "count": 0,
               "last_step": "", "last_step_ts": 0, "last_concl_id": 0}
        r = self._fetchone(
            "SELECT id, ts FROM messages WHERE topic_id=? ORDER BY ts DESC, id DESC LIMIT 1",
            (topic_id,))
        if not r:
            return out
        ts = r["ts"]
        if ts and ts < 1e14:
            ts = ts * 1000
        out["last_id"] = r["id"] or 0
        out["last_ts"] = ts or 0
        cnt = self._fetchone("SELECT COUNT(*) AS n FROM messages WHERE topic_id=?", (topic_id,))
        out["count"] = (cnt["n"] if cnt else 0) or 0
        t = self._fetchone(
            "SELECT id, json_extract(args,'$.name') AS nm, ts FROM messages "
            "WHERE topic_id=? AND role='tool' ORDER BY ts DESC, id DESC LIMIT 1", (topic_id,))
        if t:
            out["last_step"] = t["nm"] or ""
            _tts = t["ts"]
            if _tts and _tts < 1e14:
                _tts = _tts * 1000
            out["last_step_ts"] = _tts or 0
        c = self._fetchone(
            "SELECT id FROM messages WHERE topic_id=? AND " + self._CONCL_CASE + "=1 "
            "ORDER BY ts DESC, id DESC LIMIT 1", (topic_id,))
        if c:
            out["last_concl_id"] = c["id"] or 0
        return out

    def get_proc_range(self, topic_id, from_id, to_id):
        """按 id 区间取"过程明细"（工具调用 + 工具轮动作叙述），供前端点开折叠占位时懒加载。
        返回结构与 get_messages 同构，前端复用同一套解析（_mapServerMsgs + groupProcess）。
        to_id=0/空 = 开到末尾（任务尾部那段"还没收尾"的过程，占位就是这么标的）。"""
        if int(to_id or 0) > 0:
            _cond = "id>? AND id<=?"
            _args = (topic_id, int(from_id or 0), int(to_id))
        else:
            _cond = "id>?"
            _args = (topic_id, int(from_id or 0))
        rows = self._fetchall(
            "SELECT id, topic_id, role, text, args, ts FROM messages "
            "WHERE topic_id=? AND " + _cond + " "
            "AND (role='tool' OR (role='ai' AND args LIKE '%\"process\"%')) "
            "ORDER BY ts, id", _args)
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
            if r["role"] == "ai":
                m["has_thinking"] = True   # 正文仍走 /api/message-thinking 点开才拉
            result.append(m)
        return result

    def last_message_id(self, topic_id):
        """该任务最后一条消息的 id（判定"有没有新东西"用，走索引倒序 seek）。"""
        r = self._fetchone(
            "SELECT id FROM messages WHERE topic_id=? ORDER BY ts DESC, id DESC LIMIT 1", (topic_id,))
        return (r["id"] or 0) if r else 0

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
        """Sync messages from a legacy session file into DB. Position-based: only appends new messages beyond what DB already has."""
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
        """Parse a legacy session .jsonl file into message list."""
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


def get_db(path=None):
    """获取 DB 单例。优先级：显式 path > 环境变量 BIGEYE_DB_PATH > 默认 DB_PATH。

    BIGEYE_DB_PATH 是测试隔离开关：脚本里设了它，所有模块（含 evstore 等
    自带 get_db 引用的模块）都会自动切到临时库，避免误写生产库。
    """
    global _db
    target = path or os.environ.get("BIGEYE_DB_PATH") or DB_PATH
    if _db is None or os.path.abspath(_db.path) != os.path.abspath(target):
        _db = Database(target)
    return _db
