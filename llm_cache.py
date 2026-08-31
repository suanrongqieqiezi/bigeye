#!/usr/bin/env python3
"""
LLM 响应本地缓存 —— 全链路命中即秒回，避免重复消费。

- 表挂在已有的 data/api_logs.db（避免新增数据库文件）
- key = sha256(model + clean_messages + tools + max_tokens)，与发给 API 的 body 字段对齐
- 不缓存带 tool_calls 的响应（工具必须真执行）和 ERROR 响应
- TTL 7 天 + LRU 容量上限 1000 条
- 线程安全：单把锁覆盖读写
"""
import hashlib
import json
import os
import sqlite3
import threading
import time


# ── 默认参数 ──
_TTL_SECONDS = 7 * 24 * 3600   # 7 天
_MAX_ENTRIES = 1000            # LRU 容量上限


def _db_path():
    """复用 api_logs.db，避免新增文件。"""
    base = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.dirname(os.path.abspath(__file__))
    # 与 api_logger 同样规则：打包后用 exe 所在目录
    import sys
    if getattr(sys, 'frozen', False):
        data_dir = os.path.dirname(sys.executable)
    return os.path.join(data_dir, "data", "api_logs.db")


class LLMCache:
    """Thread-safe LLM response cache backed by SQLite."""

    def __init__(self, db_path=None, ttl=_TTL_SECONDS, max_entries=_MAX_ENTRIES):
        self._db_path = db_path or _db_path()
        self._ttl = ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._ensure_schema()

    def _conn(self):
        c = sqlite3.connect(self._db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _ensure_schema(self):
        c = self._conn()
        try:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS llm_response_cache (
                    cache_key    TEXT PRIMARY KEY,
                    model        TEXT,
                    full_text    TEXT NOT NULL DEFAULT '',
                    full_thinking TEXT NOT NULL DEFAULT '',
                    usage_json   TEXT NOT NULL DEFAULT '{}',
                    has_tool_calls INTEGER NOT NULL DEFAULT 0,
                    hit_count    INTEGER NOT NULL DEFAULT 0,
                    created_at   REAL NOT NULL,
                    last_hit_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_llm_cache_last_hit
                    ON llm_response_cache(last_hit_at ASC);
            """)
            c.commit()
        finally:
            c.close()

    # ── key 计算 ──
    @staticmethod
    def compute_key(config, messages, tools, max_tokens):
        """key 必须与发给 API 的 body 字段完全对齐，否则会误命中。"""
        clean = []
        for m in messages:
            clean.append({k: v for k, v in m.items() if not k.startswith("_")})
        payload = {
            "model": config.model,
            "messages": clean,
            "tools": tools or [],
            "max_tokens": max_tokens,
        }
        s = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    # ── 读 ──
    def get(self, cache_key):
        """命中返回 dict，否则返回 None。同时更新 hit_count / last_hit_at。"""
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                row = c.execute(
                    "SELECT * FROM llm_response_cache WHERE cache_key=?",
                    (cache_key,)
                ).fetchone()
                if not row:
                    return None
                # TTL 过期：删掉并视为 miss
                if now - row["created_at"] > self._ttl:
                    c.execute("DELETE FROM llm_response_cache WHERE cache_key=?",
                              (cache_key,))
                    c.commit()
                    return None
                # 命中：刷新 hit_count / last_hit_at
                c.execute(
                    "UPDATE llm_response_cache SET hit_count=hit_count+1, last_hit_at=? "
                    "WHERE cache_key=?",
                    (now, cache_key)
                )
                c.commit()
                return {
                    "full_text": row["full_text"],
                    "full_thinking": row["full_thinking"],
                    "usage": json.loads(row["usage_json"] or "{}"),
                    "hit_count": row["hit_count"] + 1,
                    "created_at": row["created_at"],
                }
            finally:
                c.close()

    # ── 写 ──
    def put(self, cache_key, model, full_text, full_thinking, usage, has_tool_calls):
        """落库一条缓存。带 tool_calls 的响应不写入（调用方应自行跳过）。"""
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                c.execute(
                    """INSERT OR REPLACE INTO llm_response_cache
                       (cache_key, model, full_text, full_thinking, usage_json,
                        has_tool_calls, hit_count, created_at, last_hit_at)
                       VALUES (?,?,?,?,?,0,0,?,?)""",
                    (cache_key, model, full_text, full_thinking,
                     json.dumps(usage or {}, ensure_ascii=False), now, now)
                )
                c.commit()
                self._evict(c)
            finally:
                c.close()

    def _evict(self, c):
        """LRU 淘汰：超过容量按 last_hit_at 升序删除最旧条目。"""
        row = c.execute("SELECT COUNT(*) AS cnt FROM llm_response_cache").fetchone()
        cnt = row["cnt"] if row else 0
        if cnt <= self._max_entries:
            return
        excess = cnt - self._max_entries
        c.execute(
            "DELETE FROM llm_response_cache WHERE cache_key IN ("
            "  SELECT cache_key FROM llm_response_cache "
            "  ORDER BY last_hit_at ASC LIMIT ?"
            ")",
            (excess,)
        )
        c.commit()

    # ── 维护 ──
    def clear(self):
        """清空所有缓存条目。"""
        with self._lock:
            c = self._conn()
            try:
                c.execute("DELETE FROM llm_response_cache")
                c.commit()
            finally:
                c.close()

    def stats(self):
        """返回统计信息：条数、总命中次数、平均命中率等。"""
        with self._lock:
            c = self._conn()
            try:
                row = c.execute(
                    "SELECT COUNT(*) AS cnt, COALESCE(SUM(hit_count),0) AS total_hits "
                    "FROM llm_response_cache"
                ).fetchone()
                cnt = row["cnt"] if row else 0
                total_hits = row["total_hits"] if row else 0
                # 估算大小（粗略）
                size_row = c.execute(
                    "SELECT COALESCE(SUM(LENGTH(full_text)+LENGTH(full_thinking)+LENGTH(usage_json)),0) AS sz "
                    "FROM llm_response_cache"
                ).fetchone()
                size_bytes = size_row["sz"] if size_row else 0
                return {
                    "entries": cnt,
                    "total_hits": total_hits,
                    "size_bytes": size_bytes,
                    "max_entries": self._max_entries,
                    "ttl_seconds": self._ttl,
                }
            finally:
                c.close()


# ── 全局单例 ──
_cache = None


def get_cache():
    global _cache
    if _cache is None:
        _cache = LLMCache()
    return _cache


def compute_key(config, messages, tools, max_tokens):
    return LLMCache.compute_key(config, messages, tools, max_tokens)
