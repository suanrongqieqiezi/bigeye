# -*- coding: utf-8 -*-
"""
Order Memory (OM) 模块 —— 秩学习规则接入大眼 (Phase 1)
规则来源: Yang & Maass (Nat Commun 2026, DOI 10.1038/s41467-026-76102-5)
  w <- w + eta * (a_j - a_i)   仅当 surprise(违反 margin) 时写入
  surprise 门控 = 记忆写入的麦克斯韦妖: 无信息量不写入

四关系域设计(Phase 1 先启用两个):
  timeline    时序域: a 在 b 之前发生
  preference  优先级域: a 比 b 更优/更重要

与其他记忆存储正交: RAG 答"谁像a", OM 答"a、b 谁先/谁优"。
存储: <bigeyeZero根>/data/om_memory.db (独立SQLite, 不碰 chat.db; 实际路径由模块位置动态推导, 当前为 bigeyeZero (2)/data/om_memory.db)
消融开关: OrderMemory(gate_on=..., margin_on=...) 供真实数据消融实验复用。
"""
import hashlib
import json
import os
import sqlite3
import threading
import time

import numpy as np

# ── 配置 ────────────────────────────────────────────
DIM = 1000          # 编码维度
K_SPARSE = 30       # 每条目哈希编码中 1 的个数 (3% 稀疏)
ETA = 0.1           # 学习率 (论文默认)
DELTA = 1.0         # margin (论文默认)
DOMAINS = ("timeline", "preference")

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "om_memory.db",
)

_lock = threading.Lock()
_instance = {}  # 域级单例: {domain: OrderMemory}（bug修复：原不分域单例会让双域互相污染）


def item_vector(item_id: str, dim: int = DIM, k: int = K_SPARSE) -> np.ndarray:
    """条目 -> 稳定稀疏二进制向量。md5 种子保证跨重启/跨进程一致。"""
    h = hashlib.md5(str(item_id).encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    v = np.zeros(dim, dtype=np.float32)
    idx = rng.choice(dim, size=k, replace=False)
    v[idx] = 1.0
    return v


class OrderMemory:
    """单域秩学习器 + 持久化。r(a) = w·a，surprise 门控写入。"""

    def __init__(self, domain: str, gate_on: bool = True, margin_on: bool = True,
                 db_path: str = None):
        assert domain in DOMAINS, f"domain must be one of {DOMAINS}"
        self.domain = domain
        self.gate_on = gate_on
        self.margin_on = margin_on
        self.delta = DELTA if margin_on else 0.0
        self.eta = ETA
        self.db_path = db_path or _DB_PATH
        self.w = np.zeros(DIM, dtype=np.float32)  # 零初始化(比随机更稳: 全序从小开始)
        self.n_updates = 0
        self.n_observed = 0
        self._load()

    # ── 持久化 ──
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS om_weights(
                        domain TEXT PRIMARY KEY, w BLOB, n_updates INTEGER,
                        n_observed INTEGER, updated_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS om_events(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT,
                        item_a TEXT, item_b TEXT, wrote INTEGER,
                        surprise INTEGER, ts REAL)""")

    def _load(self):
        self._init_db()
        with self._conn() as c:
            row = c.execute("SELECT w,n_updates,n_observed FROM om_weights WHERE domain=?",
                            (self.domain,)).fetchone()
        if row and row[0]:
            self.w = np.frombuffer(row[0], dtype=np.float32).copy()
            self.n_updates = row[1] or 0
            self.n_observed = row[2] or 0

    def save(self):
        with self._conn() as c:
            c.execute("""INSERT INTO om_weights(domain,w,n_updates,n_observed,updated_at)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(domain) DO UPDATE SET w=excluded.w,
                        n_updates=excluded.n_updates, n_observed=excluded.n_observed,
                        updated_at=excluded.updated_at""",
                      (self.domain, self.w.tobytes(), self.n_updates,
                       self.n_observed, time.time()))

    # ── 核心规则 ──
    def predict_ok(self, a: str, b: str) -> bool:
        """当前打分是否已满足 a < b。
        margin_on: gap >= delta (带缓冲)；margin_off: gap > 0 (严格为正，gap=0是未决策)。"""
        d = float(self.w @ item_vector(b) - self.w @ item_vector(a))
        return d >= self.delta if self.margin_on else d > 0.0

    def observe(self, a: str, b: str, force: bool = False) -> dict:
        """收到事实 a < b (a 在 b 前 / a 优于 b)。
        surprise = 当前打分违反该事实(或无门控模式)。
        返回 {wrote, surprise, gap}: gap=打分差(b-a), 负值=违反幅度。"""
        va, vb = item_vector(a), item_vector(b)
        gap = float(self.w @ vb - self.w @ va)
        # surprise = 违反要求。margin_on 要求 gap>=delta(缓冲)；margin_off 要求 gap>0(严格)
        surprise = not (gap >= self.delta) if self.margin_on else not (gap > 0.0)
        self.n_observed += 1
        wrote = False
        if surprise or force or not self.gate_on:
            self.w += self.eta * (vb - va)
            self.n_updates += 1
            wrote = True
        with self._conn() as c:
            c.execute("INSERT INTO om_events(domain,item_a,item_b,wrote,surprise,ts)"
                      " VALUES(?,?,?,?,?,?)",
                      (self.domain, a, b, int(wrote), int(surprise), time.time()))
        return {"wrote": wrote, "surprise": surprise, "gap": round(gap, 4)}

    def rank(self, a: str) -> float:
        return float(self.w @ item_vector(a))

    def compare(self, a: str, b: str) -> dict:
        """返回谁在先/谁优 + margin 置信度。margin 越大越确信。"""
        ra, rb = self.rank(a), self.rank(b)
        if rb - ra >= 0:
            return {"first": a, "second": b, "margin": round(rb - ra, 4)}
        return {"first": b, "second": a, "margin": round(ra - rb, 4)}

    def weight_norm(self) -> float:
        return round(float(np.linalg.norm(self.w)), 4)

    def event_stats(self) -> dict:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*),SUM(wrote),SUM(surprise) FROM om_events"
                          " WHERE domain=?", (self.domain,)).fetchone()
        return {"events": n[0] or 0, "writes": n[1] or 0, "surprises": n[2] or 0}


def get_om(domain: str, gate_on: bool = True, margin_on: bool = True) -> OrderMemory:
    """模块级单例入口(仅正式配置缓存; 消融实验用独立实例+独立db_path)。"""
    if gate_on and margin_on:
        with _lock:
            if _instance.get(domain) is None:
                _instance[domain] = OrderMemory(domain, gate_on=True, margin_on=True)
            return _instance[domain]
    return OrderMemory(domain, gate_on=gate_on, margin_on=margin_on)
