#!/usr/bin/env python3
"""用户对话消息向量索引（Block 5c 数据源）。

给 messages 表 role='user' 的消息建向量缓存，用于跨任务召回
"可能关联的对话记录"——当 AI 处理当前任务时，能想起用户在其他
对话里说过相关的原话。

表结构:
    user_msg_vectors(message_id INTEGER PRIMARY KEY, vec BLOB, ts REAL)

用法:
    index_user_message(msg_id, text, ts)   # 新消息入库时增量维护
    recall_related_dialogs(query, exclude_topic_id, top_k=5)  # 向量召回
"""
import os
import sqlite3
import struct
import threading
import sys

MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(MEMORY_DIR)
DB_PATH = os.path.join(ROOT_DIR, "data", "chat.db")

_lock = threading.Lock()
_vec_cache = None  # [(message_id, ts, topic_id, text, vec), ...] 内存缓存
_cache_ts = 0.0

# numpy 加速（可选，不可用时回退纯 python）
try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:
    _np = None
    _HAS_NUMPY = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_msg_vectors (
    message_id INTEGER PRIMARY KEY,
    vec BLOB NOT NULL,
    ts REAL
)
"""


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(SCHEMA)
    return conn


def _vec_to_blob(v):
    return struct.pack(f"{len(v)}f", *v)


def _blob_to_vec(b):
    return list(struct.unpack(f"{len(b)//4}f", b))


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _cosine_batch(qv, vecs):
    """批量 cosine：qv 单个向量，vecs 向量列表。返回分数列表。"""
    if _HAS_NUMPY:
        import numpy as np
        A = np.asarray(vecs, dtype=np.float32)
        q = np.asarray(qv, dtype=np.float32)
        dots = A @ q
        norms = np.linalg.norm(A, axis=1)
        qn = np.linalg.norm(q)
        denom = norms * qn
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where(denom > 0, dots / denom, 0.0)
        return scores.tolist()
    return [_cosine(qv, v) for v in vecs]


def index_user_message(msg_id, text, ts=None):
    """新 user 消息入库时调用：embed 并写入向量表。失败静默（不阻塞主流程）。"""
    if not text or len(text.strip()) < 2:
        return
    try:
        from memory.embedder import embed
        v = embed(text[:300])
        if not v:
            return
        with _lock:
            conn = _conn()
            conn.execute(
                "INSERT OR REPLACE INTO user_msg_vectors(message_id, vec, ts) VALUES(?,?,?)",
                (msg_id, _vec_to_blob(v), ts),
            )
            conn.commit()
            conn.close()
            global _cache_ts
            _cache_ts = 0.0  # 使缓存失效
    except Exception as e:
        print(f"[msg_vectors] index failed: {e}")


def _load_cache():
    """全量加载向量缓存（2039 条 × 512 dim，内存约 4MB，可接受）。"""
    global _vec_cache, _cache_ts
    with _lock:
        if _vec_cache is not None and time.time() - _cache_ts < 60:
            return _vec_cache
        conn = _conn()
        rows = conn.execute(
            "SELECT v.message_id, v.ts, m.topic_id, m.text, v.vec, t.title "
            "FROM user_msg_vectors v JOIN messages m ON m.id = v.message_id "
            "LEFT JOIN topics t ON t.id = m.topic_id "
            "WHERE m.role='user'"
        ).fetchall()
        conn.close()
        _vec_cache = [
            (mid, ts, topic_id, text, _blob_to_vec(blob), title)
            for mid, ts, topic_id, text, blob, title in rows
        ]
        _cache_ts = time.time()
        return _vec_cache


def recall_related_dialogs(query, exclude_topic_id=None, top_k=5, threshold=0.30):
    """向量召回与 query 相关的历史用户消息。

    返回 list[dict]: {message_id, topic_id, topic_title, text, ts, score}
    排除 exclude_topic_id 的话题（通常是当前任务），避免与上下文重复。
    同一 topic 最多 2 条，保证多样性。
    """
    import time as _time
    if not query or len(query.strip()) < 2:
        return []
    try:
        from memory.embedder import embed
        qv = embed(query[:300])
        if not qv or all(x == 0 for x in qv):
            return []
        cache = _load_cache()
        if not cache:
            return []
        # 批量算分数（numpy 加速）
        vecs = [row[4] for row in cache]
        scores = _cosine_batch(qv, vecs)
        scored = []
        for idx, (mid, ts, topic_id, text, vec, title) in enumerate(cache):
            if exclude_topic_id:
                if topic_id == exclude_topic_id:
                    continue
                if topic_id and exclude_topic_id and topic_id.startswith(exclude_topic_id[:8]):
                    continue
            if not text or len(text.strip()) < 2:
                continue
            s = scores[idx]
            if s >= threshold:
                scored.append({"message_id": mid, "topic_id": topic_id,
                               "topic_title": title or "", "text": text,
                               "ts": ts, "score": s})
        scored.sort(key=lambda x: x["score"], reverse=True)
        # 同 topic 最多 2 条
        picked, topic_count = [], {}
        for item in scored:
            tid = item["topic_id"] or "_none_"
            if topic_count.get(tid, 0) >= 2:
                continue
            topic_count[tid] = topic_count.get(tid, 0) + 1
            picked.append(item)
            if len(picked) >= top_k:
                break
        return picked
    except Exception as e:
        print(f"[msg_vectors] recall failed: {e}")
        return []


def recall_with_context(query, exclude_topic_id=None, top_k=4,
                        ctx_before=3, ctx_after=2, threshold=0.30):
    """向量召回相关历史用户消息，每条附所在话题的前后文（"念头"模式）。

    在 recall_related_dialogs 基础上，对每条命中消息从 messages 表
    取它所在话题的上下文（按 id 序，命中前 ctx_before 条 + 后 ctx_after 条），
    让召回结果能"看到"对话现场，而不是只有孤立一句。

    返回 list[dict]:
        {message_id, topic_id, topic_title, text, ts, score,
         context: [{role, text, ts}, ...]}  # 命中消息前后文（不含命中本身）
    """
    hits = recall_related_dialogs(query, exclude_topic_id, top_k, threshold)
    if not hits:
        return []
    conn = _conn()
    try:
        for h in hits:
            mid = h["message_id"]
            tid = h.get("topic_id") or ""
            if not tid:
                h["context"] = []
                continue
            # 命中前 ctx_before 条（含命中自己，再剔除）
            before = conn.execute(
                "SELECT id, role, text, ts FROM messages "
                "WHERE topic_id=? AND role IN ('user','ai') AND id <= ? "
                "ORDER BY id DESC LIMIT ?",
                (tid, mid, ctx_before + 1),
            ).fetchall()
            before = list(reversed(before))
            # 命中后 ctx_after 条
            after = conn.execute(
                "SELECT id, role, text, ts FROM messages "
                "WHERE topic_id=? AND role IN ('user','ai') AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (tid, mid, ctx_after),
            ).fetchall()
            ctx = []
            for rid, rrole, rtext, rts in before + after:
                if rid == mid:
                    continue
                ctx.append({
                    "role": rrole,
                    "text": (rtext or "")[:300],
                    "ts": rts,
                })
            h["context"] = ctx
        return hits
    finally:
        conn.close()


def stats():
    try:
        conn = _conn()
        n = conn.execute("SELECT COUNT(*) FROM user_msg_vectors").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


import time  # noqa: E402  (放在 _load_cache 使用之后)
