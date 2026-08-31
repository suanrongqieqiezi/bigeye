#!/usr/bin/env python3
"""
Node DAG — task breakdown structure with status state machine.

Node lifecycle:
  pending → running → done
              │          ↑
              ├──→ blocked (blocker → roundtrip → running)
              ├──→ failed (retry N times → fail)
              └──→ split → children created, self → done

All nodes persisted in task_nodes table (chat.db).
"""
import hashlib
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


class NodeStatus:
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SPLIT = "split"  # dynamic split → children created, self marked done-as-split

    VALID = {PENDING, RUNNING, BLOCKED, DONE, FAILED, SPLIT}
    FINAL = {DONE, FAILED, SPLIT}
    VALID_TRANSITIONS = {
        PENDING: {RUNNING, BLOCKED, FAILED},
        RUNNING: {DONE, BLOCKED, FAILED, SPLIT},
        BLOCKED: {RUNNING, FAILED, SPLIT},
        DONE: {PENDING},   # 结构编辑/返工时允许从已完成重置重跑（改图永远允许）
        FAILED: {PENDING},   # 允许失败重试（retry_count < 3 时由 executor 重置）
        SPLIT: set(),
    }


def _make_hash(*parts):
    raw = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ── SQL helpers ──────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_instances (
    id              TEXT PRIMARY KEY,
    topic_id        TEXT,
    user_request    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planning',
    root_node_id    TEXT,
    created_at      REAL,
    updated_at      REAL,
    finished_at     REAL,
    version_hash    TEXT,
    dag_snapshot    TEXT
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
    retry_count     INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task_instances(id),
    FOREIGN KEY (parent_id) REFERENCES task_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_node_task ON task_nodes(task_id);
CREATE INDEX IF NOT EXISTS idx_node_status ON task_nodes(status);

CREATE TABLE IF NOT EXISTS task_edges (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    source          TEXT NOT NULL,
    target          TEXT NOT NULL,
    edge_type       TEXT DEFAULT 'flow',
    created_at      REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task_instances(id)
);
CREATE INDEX IF NOT EXISTS idx_edge_task ON task_edges(task_id);
CREATE INDEX IF NOT EXISTS idx_edge_source ON task_edges(source);
CREATE INDEX IF NOT EXISTS idx_edge_target ON task_edges(target);
"""


# ── DAG Class ─────────────────────────────────────────

class DAG:
    """Thread-safe DAG over task_nodes table. One DAG instance per task."""

    def __init__(self, task_id, db_path=None):
        self.task_id = task_id
        self._db_path = db_path or _db_path()
        self._lock = threading.RLock()  # 可重入：结构编辑时锁内调 update_task_status 不死锁
        self._ensure_schema()

    def _ensure_schema(self):
        """Create task tables if they don't exist, plus migrations."""
        import sqlite3
        c = sqlite3.connect(self._db_path, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        try:
            c.executescript(SCHEMA)
            # Migration: add updated_at to task_instances if missing
            try:
                c.execute("SELECT updated_at FROM task_instances LIMIT 0")
            except sqlite3.OperationalError:
                c.execute("ALTER TABLE task_instances ADD COLUMN updated_at REAL")
            # Migration: add dag_snapshot if missing
            try:
                c.execute("SELECT dag_snapshot FROM task_instances LIMIT 0")
            except sqlite3.OperationalError:
                c.execute("ALTER TABLE task_instances ADD COLUMN dag_snapshot TEXT")
            # Migration: add created_at if missing
            try:
                c.execute("SELECT created_at FROM task_instances LIMIT 0")
            except sqlite3.OperationalError:
                c.execute("ALTER TABLE task_instances ADD COLUMN created_at REAL")
            # Migration: add topic_id if missing
            try:
                c.execute("SELECT topic_id FROM task_instances LIMIT 0")
            except sqlite3.OperationalError:
                c.execute("ALTER TABLE task_instances ADD COLUMN topic_id TEXT")
                c.execute("CREATE INDEX IF NOT EXISTS idx_task_topic ON task_instances(topic_id)")
            # Migration: add retry_count to task_nodes if missing
            try:
                c.execute("SELECT retry_count FROM task_nodes LIMIT 0")
            except sqlite3.OperationalError:
                c.execute("ALTER TABLE task_nodes ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
            c.commit()
        finally:
            c.close()

    # ── Connections ────────────────────────────────

    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self._db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    # ── Task Instance ──────────────────────────────

    def create_task(self, user_request):
        """Create a new task instance. Returns the task record."""
        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                c.execute(
                    "INSERT INTO task_instances (id, user_request, status, created_at) VALUES (?,?,?,?)",
                    (self.task_id, user_request, 'planning', now)
                )
                c.commit()
                return self._get_task(c)
            finally:
                c.close()

    def _get_task(self, c=None):
        if c is None:
            c = self._conn()
            close = True
        else:
            close = False
        try:
            row = c.execute("SELECT * FROM task_instances WHERE id=?", (self.task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            if close:
                c.close()

    def get_task(self):
        return self._get_task()

    def set_topic_id(self, topic_id):
        """Bind this task to a chat topic. Returns updated task."""
        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                c.execute(
                    "UPDATE task_instances SET topic_id=?, updated_at=? WHERE id=?",
                    (topic_id, now, self.task_id)
                )
                c.commit()
                return self._get_task(c)
            finally:
                c.close()

    @staticmethod
    def get_task_by_topic(topic_id, db_path=None):
        """Find the most recent active task for a chat topic.
        Returns (task_id, task_record) or (None, None)."""
        import sqlite3
        p = db_path or _db_path()
        c = sqlite3.connect(p, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        try:
            row = c.execute(
                "SELECT * FROM task_instances WHERE topic_id=? AND status!='finished' ORDER BY updated_at DESC LIMIT 1",
                (topic_id,)
            ).fetchone()
            if not row:
                row = c.execute(
                    "SELECT * FROM task_instances WHERE topic_id=? ORDER BY updated_at DESC LIMIT 1",
                    (topic_id,)
                ).fetchone()
            if row:
                return row["id"], dict(row)
            return None, None
        finally:
            c.close()

    def save_to_file(self, topic_id):
        """Persist DAG as JSON file in mission folder. One DAG per topic."""
        import os
        base = os.path.dirname(self._db_path) if os.path.dirname(self._db_path) else os.path.dirname(_db_path())
        mission_dir = os.path.join(base, "missions", topic_id)
        os.makedirs(mission_dir, exist_ok=True)
        filepath = os.path.join(mission_dir, "dag.json")
        data = {
            "task_id": self.task_id,
            "topic_id": topic_id,
            "task": self.get_task(),
            "tree": self.get_tree(),
            "nodes": self.get_nodes(),
            "edges": self.get_edges(),
            "saved_at": time.time(),
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return filepath

    @staticmethod
    def load_from_file(topic_id, db_path=None):
        """Restore DAG from mission folder if DB has no task for this topic.
        Returns (task_id, dag_instance) or (None, None)."""
        import os
        p = db_path or _db_path()
        base = os.path.dirname(p)
        filepath = os.path.join(base, "missions", topic_id, "dag.json")
        if not os.path.isfile(filepath):
            return None, None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            task_id = data.get("task_id")
            if not task_id:
                return None, None
            dag = DAG(task_id, db_path=p)
            task_info = dag.get_task()
            if task_info:
                return task_id, dag  # already in DB
            # Restore task_instances row
            task = data.get("task", {})
            with dag._lock:
                c = dag._conn()
                try:
                    now = time.time()
                    c.execute(
                        "INSERT OR REPLACE INTO task_instances (id, topic_id, user_request, status, root_node_id, updated_at, version_hash) VALUES (?,?,?,?,?,?,?)",
                        (task_id, topic_id, task.get("user_request",""), task.get("status","done"), task.get("root_node_id"), now, task.get("version_hash"))
                    )
                    # Restore nodes
                    for n in data.get("nodes", []):
                        c.execute(
                            "INSERT OR REPLACE INTO task_nodes (id, task_id, parent_id, task, status, dependencies, result, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (n["id"], task_id, n.get("parent_id"), n.get("task",""), n.get("status","pending"),
                             json.dumps(n.get("dependencies",[])) if isinstance(n.get("dependencies"), list) else n.get("dependencies","[]"),
                             n.get("result"), n.get("created_at", now), n.get("updated_at", now))
                        )
                    # Restore edges
                    for e in data.get("edges", []):
                        c.execute(
                            "INSERT OR REPLACE INTO task_edges (id, task_id, source, target, edge_type, created_at) VALUES (?,?,?,?,?,?)",
                            (e["id"], task_id, e.get("source"), e.get("target"), e.get("edge_type","flow"), e.get("created_at", now))
                        )
                    c.commit()
                finally:
                    c.close()
            return task_id, dag
        except Exception:
            return None, None

    def _mark_structural_change(self):
        """结构编辑后调用：若任务处于终态(done/failed)，拉回 running 让流程可继续。

        语义：改图永远允许——改完旧执行结果归档（保留在 result 字段），
        任务回到执行中状态，按新图重跑。
        """
        task = self._get_task()
        if task and task.get("status") in NodeStatus.FINAL:
            self.update_task_status(NodeStatus.RUNNING)

    def update_task_status(self, status, finished_at=None):
        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                if status in NodeStatus.FINAL:
                    # 进入终态：记录完成时间 + 保存快照
                    ts = finished_at or now
                    c.execute(
                        "UPDATE task_instances SET status=?, finished_at=?, updated_at=? WHERE id=?",
                        (status, ts, now, self.task_id)
                    )
                    self._save_snapshot(c)
                else:
                    # 回到非终态（如结构编辑后拉回 running）：清除完成时间
                    c.execute(
                        "UPDATE task_instances SET status=?, finished_at=NULL, updated_at=? WHERE id=?",
                        (status, now, self.task_id)
                    )
                c.commit()
            finally:
                c.close()

    def _save_snapshot(self, cursor):
        """Snapshot current DAG state into task_instances.dag_snapshot (JSON).
        Uses the provided cursor (within lock scope).
        """
        # Read nodes directly via the locked cursor
        rows = cursor.execute(
            "SELECT id, task, status, parent_id, dependencies, result FROM task_nodes WHERE task_id=? ORDER BY created_at",
            (self.task_id,)
        ).fetchall()
        nodes = [dict(r) for r in rows]
        # Read edges
        edge_rows = cursor.execute(
            "SELECT id, source, target, edge_type FROM task_edges WHERE task_id=? ORDER BY created_at",
            (self.task_id,)
        ).fetchall()
        edges = [dict(r) for r in edge_rows]
        # If no explicit edges, derive from legacy parent_id + dependencies
        if not edges:
            edges = self._derive_edges_from_legacy(nodes)
        # Build tree in-memory (backward compat for consumers that still read tree)
        def _build_tree(parent_id):
            children = []
            for n in nodes:
                if n.get("parent_id") == parent_id:
                    child = dict(n)
                    child["children"] = _build_tree(n["id"])
                    if "dependencies" in child and isinstance(child["dependencies"], str):
                        child["dependencies"] = json.loads(child["dependencies"])
                    children.append(child)
            return children
        tree = None
        for n in nodes:
            if n.get("parent_id") is None:
                root = dict(n)
                root["children"] = _build_tree(n["id"])
                if "dependencies" in root and isinstance(root["dependencies"], str):
                    root["dependencies"] = json.loads(root["dependencies"])
                tree = root
                break
        if not tree:
            tree = {"task_id": self.task_id, "nodes": nodes}
        snapshot = json.dumps({
            "tree": tree,
            "nodes": nodes,
            "edges": edges,
        }, ensure_ascii=False, default=str)
        cursor.execute(
            "UPDATE task_instances SET dag_snapshot=? WHERE id=?",
            (snapshot, self.task_id)
        )

    def set_root_node(self, node_id):
        with self._lock:
            c = self._conn()
            try:
                c.execute("UPDATE task_instances SET root_node_id=? WHERE id=?", (node_id, self.task_id))
                c.commit()
            finally:
                c.close()

    # ── Node CRUD ─────────────────────────────────

    def create_node(self, task_text, parent_id=None, dependencies=None, node_id=None):
        """Create a new node in the DAG. Returns the node dict."""
        with self._lock:
            c = self._conn()
            try:
                nid = node_id or uuid.uuid4().hex[:12]
                now = time.time()
                deps = json.dumps(dependencies or [])
                vhash = _make_hash(self.task_id, nid, task_text, now)
                c.execute(
                    """INSERT INTO task_nodes
                       (id, task_id, parent_id, task, dependencies, exec_context, version_hash, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (nid, self.task_id, parent_id, task_text, deps, '{}', vhash, now, now)
                )
                c.commit()
                row = c.execute("SELECT * FROM task_nodes WHERE id=?", (nid,)).fetchone()
                return dict(row)
            finally:
                c.close()

    def get_node(self, node_id):
        c = self._conn()
        try:
            row = c.execute("SELECT * FROM task_nodes WHERE id=?", (node_id,)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def get_nodes(self, status=None):
        """List all nodes for this task, optionally filtered by status."""
        c = self._conn()
        try:
            if status:
                rows = c.execute(
                    "SELECT * FROM task_nodes WHERE task_id=? AND status=? ORDER BY created_at",
                    (self.task_id, status)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM task_nodes WHERE task_id=? ORDER BY created_at",
                    (self.task_id,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def get_children(self, node_id):
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT * FROM task_nodes WHERE task_id=? AND parent_id=? ORDER BY created_at",
                (self.task_id, node_id)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def get_parent(self, node_id):
        node = self.get_node(node_id)
        if not node or not node["parent_id"]:
            return None
        return self.get_node(node["parent_id"])

    def get_sibling_status(self, node_id):
        """Get status dict of siblings (same parent, excluding self)."""
        node = self.get_node(node_id)
        if not node or not node["parent_id"]:
            return {}
        sibs = self.get_children(node["parent_id"])
        return {s["id"]: s["status"] for s in sibs if s["id"] != node_id}

    def get_root(self):
        c = self._conn()
        try:
            row = c.execute(
                "SELECT * FROM task_nodes WHERE task_id=? AND parent_id IS NULL LIMIT 1",
                (self.task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def get_runnable_nodes(self):
        """Nodes in pending state whose dependencies are all done."""
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT * FROM task_nodes WHERE task_id=? AND status=? ORDER BY created_at",
                (self.task_id, NodeStatus.PENDING)
            ).fetchall()
            runnable = []
            for row in rows:
                node = dict(row)
                deps = json.loads(node.get("dependencies", "[]"))
                if not deps:
                    runnable.append(node)
                else:
                    dep_stati = c.execute(
                        "SELECT id, status FROM task_nodes WHERE id IN ({})".format(
                            ",".join("?" for _ in deps)
                        ), deps
                    ).fetchall()
                    if all(ds["status"] in (NodeStatus.DONE, NodeStatus.SPLIT) for ds in dep_stati):
                        runnable.append(node)
            return runnable
        finally:
            c.close()

    def get_blocked_nodes(self):
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT * FROM task_nodes WHERE task_id=? AND status=? ORDER BY created_at",
                (self.task_id, NodeStatus.BLOCKED)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def get_parent_summary(self, node_id):
        """1-2 sentence summary of parent node's result for slide_in block 3."""
        node = self.get_node(node_id)
        if not node or not node["parent_id"]:
            return "根节点"
        parent = self.get_node(node["parent_id"])
        if not parent:
            return "根节点"
        result = parent.get("result", "")
        task_text = parent.get("task", "")
        if result:
            return f"[{parent['id'][:8]}] {task_text} → {result[:200]}"
        return f"[{parent['id'][:8]}] {task_text}（{'完成' if parent['status'] == 'done' else parent['status']}）"

    # ── Status transitions ─────────────────────────

    def set_status(self, node_id, new_status, result=None):
        """Transition node to new status. Validates transition. Returns node dict."""
        if new_status not in NodeStatus.VALID:
            raise ValueError(f"Invalid status: {new_status}")
        with self._lock:
            c = self._conn()
            try:
                node = c.execute("SELECT * FROM task_nodes WHERE id=?", (node_id,)).fetchone()
                if not node:
                    raise ValueError(f"Node {node_id} not found")
                old = node["status"]
                if new_status not in NodeStatus.VALID_TRANSITIONS.get(old, set()):
                    raise ValueError(f"Invalid transition: {old} → {new_status}")
                now = time.time()
                update_parts = ["status=?", "updated_at=?"]
                update_vals = [new_status, now]
                if result is not None:
                    update_parts.append("result=?")
                    update_vals.append(result)
                # v4.0: failed → 累加 retry_count（超 3 次由 executor 不再重置）
                if new_status == NodeStatus.FAILED:
                    current_retry = node["retry_count"] if "retry_count" in node.keys() else 0
                    update_parts.append("retry_count=?")
                    update_vals.append(current_retry + 1)
                update_vals.append(node_id)
                c.execute(
                    f"UPDATE task_nodes SET {', '.join(update_parts)} WHERE id=?",
                    update_vals
                )
                c.commit()
                row = c.execute("SELECT * FROM task_nodes WHERE id=?", (node_id,)).fetchone()
                # 返工语义：终态节点被重置 → 任务状态拉回 running（改图永远允许）
                if old in NodeStatus.FINAL and new_status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                    self._mark_structural_change()
                return dict(row)
            finally:
                c.close()

    def update_context(self, node_id, context_dict):
        """Merge values into node's exec_context JSON."""
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT exec_context FROM task_nodes WHERE id=?", (node_id,)).fetchone()
                if not row:
                    return
                ctx = json.loads(row["exec_context"] or "{}")
                ctx.update(context_dict)
                now = time.time()
                c.execute(
                    "UPDATE task_nodes SET exec_context=?, updated_at=? WHERE id=?",
                    (json.dumps(ctx, ensure_ascii=False), now, node_id)
                )
                c.commit()
            finally:
                c.close()

    # ── Tree query ─────────────────────────────────

    def get_tree(self):
        """Return nested tree structure for visualization."""
        nodes = self.get_nodes()
        if not nodes:
            return None

        root = self.get_root()
        if not root:
            # orphan nodes - return flat list
            return {"task_id": self.task_id, "nodes": nodes}

        def _build(node):
            children = self.get_children(node["id"])
            return {
                "id": node["id"],
                "task": node["task"],
                "status": node["status"],
                "parent_id": node["parent_id"],
                "dependencies": json.loads(node.get("dependencies", "[]")),
                "children": [_build(c) for c in children],
            }

        return _build(root)

    # ── Graph modification methods (流程图即工作面板) ──

    def insert_node(self, parent_id=None, task_text="", dependencies=None):
        """Insert a new node at a specific position in the DAG.

        Automatically creates explicit edges in task_edges:
          - parent_id → new node (flow edge)
          - each dependency → new node (dependency edge)

        This keeps task_edges in sync with the node tree, so the flowchart
        always shows connections without relying on _derive_edges_from_legacy.

        Args:
            parent_id: Parent node ID (None = root level).
            task_text: Node task description.
            dependencies: List of node IDs that must complete before this node.

        Returns: New node dict.
        """
        node = self.create_node(task_text, parent_id=parent_id, dependencies=dependencies)
        # Create explicit edges for connectivity
        if parent_id:
            self.create_edge(parent_id, node["id"], "flow")
        if dependencies:
            for dep_id in dependencies:
                if dep_id != parent_id and dep_id:  # avoid dup with parent_id edge
                    self.create_edge(dep_id, node["id"], "dependency")
        # 结构编辑：终态任务拉回 running（加节点 = 改图）
        self._mark_structural_change()
        return node

    def remove_node(self, node_id, reroute=True):
        """Remove a node from the DAG. Reroute children to parent so the graph stays connected.

        The node is marked as failed (with a removal marker), not actually deleted,
        so foreign key references in work_memory remain valid.

        Args:
            node_id: Node to remove.
            reroute: If True, reconnect children to the removed node's parent.

        Returns: dict with removed node info and rerouted children.
        """
        node = self.get_node(node_id)
        if not node:
            return {"error": f"node {node_id} not found"}
        # 改图永远允许：终态节点也可删除（标记 failed-removed，旧结果留在 result 字段归档）

        children = self.get_children(node_id)
        parent_id = node.get("parent_id")

        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                if reroute and parent_id and children:
                    for child in children:
                        c.execute(
                            "UPDATE task_nodes SET parent_id=?, updated_at=? WHERE id=?",
                            (parent_id, now, child["id"])
                        )
                elif not reroute and children:
                    child_ids = [c["id"] for c in children]
                    for cid in child_ids:
                        c.execute(
                            "UPDATE task_nodes SET parent_id=NULL, updated_at=? WHERE id=?",
                            (now, cid)
                        )

                # Mark as failed-removed (don't actually delete — FK safety)
                removal = json.dumps({"removed": True, "reason": "graph_modification"})
                c.execute(
                    "UPDATE task_nodes SET status=?, result=?, updated_at=? WHERE id=?",
                    (NodeStatus.FAILED, removal, now, node_id)
                )
                c.commit()
                # 结构编辑：若任务处于终态则拉回 running（改图永远允许）
                self._mark_structural_change()

                return {
                    "removed_id": node_id,
                    "removed_task": node["task"],
                    "removed_status": node["status"],
                    "rerouted_children": [c["id"] for c in children] if reroute and parent_id else [],
                }
            finally:
                c.close()

    def update_dependencies(self, node_id, new_dependencies):
        """Change a node's dependency list mid-execution. Returns updated node."""
        node = self.get_node(node_id)
        if not node:
            return {"error": f"node {node_id} not found"}

        with self._lock:
            c = self._conn()
            try:
                now = time.time()
                c.execute(
                    "UPDATE task_nodes SET dependencies=?, updated_at=? WHERE id=?",
                    (json.dumps(new_dependencies), now, node_id)
                )
                c.commit()
                row = c.execute("SELECT * FROM task_nodes WHERE id=?", (node_id,)).fetchone()
                # 结构编辑：改依赖 = 改图，终态任务拉回 running
                self._mark_structural_change()
                return dict(row) if row else {"error": "node lost after update"}
            finally:
                c.close()

    def get_execution_trace(self):
        """Get execution trace for all nodes — for review/rework (step ⑤).

        Returns: list of node execution summaries with timing and results.
        """
        nodes = self.get_nodes()
        return [
            {
                "id": n["id"],
                "task": n["task"],
                "status": n["status"],
                "retry_count": n.get("retry_count", 0),
                "result_preview": (n.get("result") or "")[:200] if n.get("result") else None,
                "created_at": n.get("created_at"),
                "updated_at": n.get("updated_at"),
            }
            for n in sorted(nodes, key=lambda x: x.get("created_at", 0))
        ]

    def has_failed_nodes(self):
        """Check if any nodes are in failed state — for rework detection."""
        nodes = self.get_nodes(status=NodeStatus.FAILED)
        return len(nodes) > 0

    def get_failed_nodes(self):
        """Get all failed nodes for rework planning."""
        return self.get_nodes(status=NodeStatus.FAILED)

    # ── Edge CRUD ─────────────────────────────────

    def create_edge(self, source, target, edge_type="flow", edge_id=None):
        """Create an edge between two nodes. Returns edge dict."""
        with self._lock:
            c = self._conn()
            try:
                eid = edge_id or uuid.uuid4().hex[:12]
                now = time.time()
                c.execute(
                    "INSERT INTO task_edges (id, task_id, source, target, edge_type, created_at) VALUES (?,?,?,?,?,?)",
                    (eid, self.task_id, source, target, edge_type, now)
                )
                c.commit()
                return {"id": eid, "task_id": self.task_id, "source": source, "target": target, "edge_type": edge_type}
            finally:
                c.close()

    def get_edges(self):
        """List all edges for this task."""
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT * FROM task_edges WHERE task_id=? ORDER BY created_at",
                (self.task_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def remove_edge(self, edge_id):
        """Remove an edge by id."""
        with self._lock:
            c = self._conn()
            try:
                c.execute("DELETE FROM task_edges WHERE id=?", (edge_id,))
                c.commit()
            finally:
                c.close()

    def clear_edges(self):
        """Remove all edges for this task."""
        with self._lock:
            c = self._conn()
            try:
                c.execute("DELETE FROM task_edges WHERE task_id=?", (self.task_id,))
                c.commit()
            finally:
                c.close()

    def clear_all(self):
        """DANGEROUS: Hard-delete all nodes + edges + snapshot.

        Only for internal use or when you really want to wipe everything.
        AI-facing tools should use archive_all() instead (soft-delete preserves
        execution history for traceability).
        """
        with self._lock:
            c = self._conn()
            try:
                c.execute("DELETE FROM task_edges WHERE task_id=?", (self.task_id,))
                c.execute("DELETE FROM task_nodes WHERE task_id=?", (self.task_id,))
                c.execute(
                    "UPDATE task_instances SET status='planning', root_node_id=NULL, "
                    "dag_snapshot=NULL, finished_at=NULL, updated_at=? WHERE id=?",
                    (time.time(), self.task_id)
                )
                c.commit()
            finally:
                c.close()

    def archive_all(self, reason="rebuild"):
        """Soft-archive all nodes + clear edges. Safe for rebuild_dag.

        - Nodes: marked as failed with result={"removed":true, "archived_at":...,
          "archive_reason":reason}. get_x6_json() auto-filters these, so renders
          only show the new DAG. Original node data (task text, results, etc.)
          is preserved in the row for audit.
        - Edges: hard-deleted (edges are planning data, no execution history).
        - Task: reset to 'planning', root_node_id cleared, snapshot cleared.

        Unlike clear_all, this preserves the node rows for traceability.
        """
        now = time.time()
        removal = json.dumps({
            "removed": True,
            "archived_at": now,
            "archive_reason": reason,
        })
        with self._lock:
            c = self._conn()
            try:
                # Soft-delete nodes (preserve rows, mark as removed)
                c.execute(
                    "UPDATE task_nodes SET status=?, result=?, updated_at=? "
                    "WHERE task_id=? AND status NOT IN (?, ?)",
                    (NodeStatus.FAILED, removal, now, self.task_id,
                     NodeStatus.FAILED, NodeStatus.SPLIT)
                )
                # Hard-delete edges (planning data, no history value)
                c.execute("DELETE FROM task_edges WHERE task_id=?", (self.task_id,))
                # Reset task state
                c.execute(
                    "UPDATE task_instances SET status='planning', root_node_id=NULL, "
                    "dag_snapshot=NULL, finished_at=NULL, updated_at=? WHERE id=?",
                    (now, self.task_id)
                )
                c.commit()
            finally:
                c.close()

    # ── X6 JSON export ─────────────────────────────

    def get_x6_json(self):
        """Return X6-standard {nodes, edges} JSON for direct graph.fromJSON().
        Filters out soft-deleted nodes and deduplicates edges."""
        all_nodes = self.get_nodes()
        # Filter out soft-deleted nodes (failed with {"removed":true} result)
        nodes = []
        for n in all_nodes:
            if n["status"] == NodeStatus.FAILED:
                result = n.get("result", "")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        result = {}
                if isinstance(result, dict) and result.get("removed"):
                    continue  # skip soft-deleted nodes
            nodes.append(n)

        edges_explicit = self.get_edges()
        edges_derived = self._derive_edges_from_legacy(nodes)
        valid_ids = {n["id"] for n in nodes}

        # Two-phase merge: explicit edges first (take precedence), then derived
        # edges only for nodes that still have NO incoming edge (true orphans).
        # This prevents redundant "skip" edges (e.g. root→节点2 when 节点1→节点2 exists).
        edge_map = {}  # key: "source|target" → edge dict
        nodes_with_incoming = set()

        # Phase 1: explicit edges
        for e in edges_explicit:
            src, tgt = e["source"], e["target"]
            if src not in valid_ids or tgt not in valid_ids:
                continue
            key = f"{src}|{tgt}"
            etype = e.get("edge_type", "flow")
            if key not in edge_map:
                edge_map[key] = {"id": e["id"], "source": src, "target": tgt, "edge_type": etype}
            else:
                if etype == "flow" and edge_map[key]["edge_type"] != "flow":
                    edge_map[key]["edge_type"] = "flow"
            nodes_with_incoming.add(tgt)

        # Phase 2: derived edges — only for nodes with no incoming edge yet
        for e in edges_derived:
            src, tgt = e["source"], e["target"]
            if src not in valid_ids or tgt not in valid_ids:
                continue
            if tgt in nodes_with_incoming:
                continue  # already has incoming edge, skip derived
            key = f"{src}|{tgt}"
            etype = e.get("edge_type", "flow")
            if key not in edge_map:
                edge_map[key] = {"id": e["id"], "source": src, "target": tgt, "edge_type": etype}
            nodes_with_incoming.add(tgt)

        x6_nodes = []
        for n in nodes:
            deps = n.get("dependencies", "[]")
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            x6_nodes.append({
                "id": n["id"],
                "shape": "rect",
                "task": n["task"],
                "status": n["status"],
                "parentId": n.get("parent_id"),
                "dependencies": deps,
                "result": n.get("result"),
                "data": {
                    "task": n["task"],
                    "status": n["status"],
                    "result": n.get("result"),
                },
            })
        x6_edges = []
        for e in edge_map.values():
            x6_edges.append({
                "id": e["id"],
                "source": e["source"],
                "target": e["target"],
                "edgeType": e["edge_type"],
            })
        return {"nodes": x6_nodes, "edges": x6_edges}

    def save_x6_json(self, x6_data, allow_structural_change=False):
        """Save user-edited X6 data back to the database.

        Two modes:
          - allow_structural_change=False (default): only update node.task text
            for nodes whose id matches an existing node. Edges are NOT modified.
            Safe for "rename only" edits.
          - allow_structural_change=True: also rebuild task_edges from the x6
            edges list (after validation). Use when the user added/removed
            connections in the flowchart UI.

        Args:
            x6_data: {"nodes":[{"id","task",...}], "edges":[{"source","target","edgeType"},...]}
            allow_structural_change: whether to rebuild edges

        Returns: dict with update counts.
        """
        import sqlite3
        from .executor import TaskExecutor

        x6_nodes = x6_data.get("nodes", []) if x6_data else []
        x6_edges = x6_data.get("edges", []) if x6_data else []

        # Map of id -> task for existing nodes (only non-soft-deleted)
        existing = self.get_nodes()
        existing_map = {}
        for n in existing:
            # Skip soft-deleted
            if n["status"] == NodeStatus.FAILED:
                result = n.get("result", "")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        result = {}
                if isinstance(result, dict) and result.get("removed"):
                    continue
            existing_map[n["id"]] = n

        updated = 0
        created = 0
        not_found = []
        now = time.time()
        id_map = {}  # front-end id → DB id (for new nodes, old id → new id)

        with self._lock:
            c = self._conn()
            try:
                # Phase 1: update existing nodes' text OR create new nodes
                for xn in x6_nodes:
                    nid = xn.get("id", "")
                    new_task = xn.get("task", "")
                    if not nid or not new_task:
                        continue
                    if nid in existing_map:
                        old_task = existing_map[nid].get("task", "")
                        if old_task != new_task:
                            c.execute(
                                "UPDATE task_nodes SET task=?, updated_at=? WHERE id=?",
                                (new_task, now, nid)
                            )
                            updated += 1
                        id_map[nid] = nid
                    else:
                        # Create new node (user added via flowchart UI)
                        db_id = uuid.uuid4().hex[:12]
                        deps = json.dumps([])
                        vhash = _make_hash(self.task_id, db_id, new_task, now)
                        c.execute(
                            """INSERT INTO task_nodes
                               (id, task_id, parent_id, task, status, dependencies,
                                exec_context, version_hash, created_at, updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (db_id, self.task_id, None, new_task, NodeStatus.PENDING,
                             deps, '{}', vhash, now, now)
                        )
                        created += 1
                        id_map[nid] = db_id
                        existing_map[db_id] = {
                            "id": db_id, "task": new_task, "status": NodeStatus.PENDING
                        }

                # Phase 1.5: handle nodes removed by user in the flowchart UI
                # (only when structural change allowed; present in DB but absent
                #  from x6_nodes → mark soft-deleted, old result preserved for archive)
                removed = 0
                if allow_structural_change:
                    x6_ids = {xn.get("id", "") for xn in x6_nodes if xn.get("id")}
                    # Map db_id → front-end id for new nodes created above
                    fe_to_db = {fe: db for fe, db in id_map.items()}
                    for db_id, n in list(existing_map.items()):
                        if db_id in fe_to_db.values():
                            continue  # present in this round's submission
                        # find front-end id that mapped to this db_id
                        fe_ids = [fe for fe, db in fe_to_db.items() if db == db_id]
                        present = db_id in x6_ids or any(fe in x6_ids for fe in fe_ids)
                        if not present:
                            removal = json.dumps({"removed": True, "reason": "user_edit",
                                                 "archived_task": n.get("task", "")})
                            c.execute(
                                "UPDATE task_nodes SET status=?, result=?, updated_at=? WHERE id=?",
                                (NodeStatus.FAILED, removal, now, db_id)
                            )
                            removed += 1

                # Phase 2: optionally rebuild edges
                edges_rebuilt = 0
                if allow_structural_change:
                    # Map front-end edge ids to DB ids (new nodes got new ids)
                    mapped_edges = []
                    for e in x6_edges:
                        src = id_map.get(e.get("source", ""), e.get("source", ""))
                        tgt = id_map.get(e.get("target", ""), e.get("target", ""))
                        etype = e.get("edgeType", e.get("edge_type", "flow"))
                        mapped_edges.append({"source": src, "target": tgt, "edge_type": etype})

                    # Validate edges (lenient: skip orphan/starburst checks for user edits)
                    valid_ids = set(existing_map.keys())
                    node_order = list(existing_map.keys())
                    errors = TaskExecutor.validate_edges(mapped_edges, valid_ids, node_order, lenient=True)
                    if errors:
                        c.close()
                        return {"error": "edges validation failed", "errors": errors}

                    # Wipe existing edges and rebuild
                    c.execute("DELETE FROM task_edges WHERE task_id=?", (self.task_id,))
                    seen = set()
                    for e in mapped_edges:
                        src = e["source"]
                        tgt = e["target"]
                        if not src or not tgt or src == tgt:
                            continue
                        key = f"{src}|{tgt}"
                        if key in seen:
                            continue
                        seen.add(key)
                        etype = e.get("edge_type", "flow")
                        eid = _make_hash(src, tgt, self.task_id, now)
                        c.execute(
                            "INSERT INTO task_edges (id, task_id, source, target, edge_type, created_at) "
                            "VALUES (?,?,?,?,?,?)",
                            (eid, self.task_id, src, tgt, etype, now)
                        )
                        edges_rebuilt += 1

                # Update task_instance timestamp
                c.execute(
                    "UPDATE task_instances SET updated_at=? WHERE id=?",
                    (now, self.task_id)
                )
                c.commit()
            finally:
                c.close()

        # 结构编辑（增/删节点或重建边）：终态任务拉回 running
        structural = (created > 0) or (removed > 0) or (edges_rebuilt > 0)
        if structural:
            self._mark_structural_change()

        # Persist to mission folder if topic bound
        task = self.get_task()
        if task and task.get("topic_id"):
            try:
                self.save_to_file(task["topic_id"])
            except Exception:
                pass

        return {
            "updated_tasks": updated,
            "created_nodes": created,
            "removed_nodes": removed,
            "not_found": not_found,
            "edges_rebuilt": edges_rebuilt if allow_structural_change else None,
            "structural_change": structural,
            "saved_at": now,
        }

    @staticmethod
    def _derive_edges_from_legacy(nodes):
        """Backward compat: derive edges from parent_id + dependencies when no explicit edges.

        Strategy: dependencies are the real DAG edges. parent_id is only used
        as a flow edge when the node has NO dependencies (to avoid disconnecting it).
        This prevents the 'starburst' where root connects to every child via flow
        while dependencies create a separate parallel network.
        """
        node_ids = {n["id"] for n in nodes}
        edges = []
        for n in nodes:
            pid = n.get("parent_id")
            deps = n.get("dependencies", "[]")
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            # Filter deps to valid node IDs only
            valid_deps = [d for d in deps if d in node_ids]

            if valid_deps:
                # Node has real dependencies → use them as the edges
                for dep_id in valid_deps:
                    edges.append({
                        "id": uuid.uuid4().hex[:12],
                        "source": dep_id,
                        "target": n["id"],
                        "edge_type": "flow",
                    })
            elif pid:
                # No dependencies but has parent → use parent as flow edge
                edges.append({
                    "id": uuid.uuid4().hex[:12],
                    "source": pid,
                    "target": n["id"],
                    "edge_type": "flow",
                })
        return edges
