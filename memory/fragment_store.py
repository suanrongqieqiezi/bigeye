#!/usr/bin/env python3
"""
Fragment memory store — autobiographical memory fragments with:

  v4.0 L3 enhanced features:
  - 5-factor recall: recency × relevance × importance × weight × entity_match
  - MMR deduplication (λ=0.7)
  - Core ↔ archival bidirectional management (no hard cap)
  - importance ≥ 7 fragments are pinned (never decay)
  - Causal neighbor propagation (one hop, confidence ≥ 0.8)
  - Epistemic classification (experience / world / opinion)
  - Entity tagging for filtered recall
  - sqlite-vec KNN acceleration if available

Design matches: 自传体记忆系统完整实现计划.md §3.1, §5.1, §5.4
"""
import json
import math
import os
import sys
import sqlite3
import time

from .embedder import embed, cosine_sim, EMBEDDING_DIM
from .vec_index import VecIndex

# ── Constants ──────────────────────────────────────
WEIGHT_DEFAULT = 1.0
WEIGHT_REINFORCE = 0.5    # sim > 0.85
WEIGHT_BOOST = 0.2        # sim > 0.70
WEIGHT_CAP = 3.0
DECAY_HALF_LIFE_DAYS = 30
DECAY_FLOOR = 0.3

RECALL_TOP_K = 5
RECALL_THRESHOLD = 0.5
# relevance 硬门槛：cosine 相似度低于此值直接淘汰，不被 importance/weight 救回
RELEVANCE_HARD_FLOOR = 0.30
MMR_LAMBDA = 0.7

# v4.0 enhanced constants
IMPORTANCE_PIN_THRESHOLD = 7.0
ACTIVE_RECENCY_DAYS = 30
ENTITY_BONUS = 1.5
RECALL_ARCHIVE_TOP_K = 5
RECALL_ARCHIVE_THRESHOLD = 0.4
CAUSAL_PROPAGATION_MIN_CONFIDENCE = 0.8

FRAGMENT_MIN_CHARS = 1


def _get_db_path():
    MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(MEMORY_DIR)
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


SCHEMA_FRAGMENTS = """
CREATE TABLE IF NOT EXISTS memory_fragments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    embedding       TEXT NOT NULL DEFAULT '[]',
    weight          REAL NOT NULL DEFAULT 1.0,
    recall_count    INTEGER NOT NULL DEFAULT 0,
    last_recalled   REAL,
    created_at      REAL NOT NULL,
    source          TEXT DEFAULT 'reflection',
    topic_id        TEXT DEFAULT NULL,
    user_tag        TEXT DEFAULT NULL,
    parent_id       INTEGER DEFAULT NULL,
    link_type       TEXT DEFAULT NULL,
    tags            TEXT DEFAULT '',
    dirty           INTEGER NOT NULL DEFAULT 1,
    -- v4.0 L3 enhanced fields
    importance      REAL NOT NULL DEFAULT 5.0,
    layer           TEXT NOT NULL DEFAULT 'core',
    entity_ids      TEXT DEFAULT NULL,
    summary_hash    TEXT DEFAULT NULL,
    raw_count       INTEGER NOT NULL DEFAULT 1,
    epistemic       TEXT NOT NULL DEFAULT 'experience',
    -- CMN fields (P0): crystal memory network support
    crystal_parent_id   TEXT DEFAULT NULL,      -- 派生它的上层晶体 id（思维血统）
    raw_source_id       TEXT DEFAULT NULL,      -- 原始素材 id（文件切片/工具输出原文）
    authority_level     INTEGER NOT NULL DEFAULT 0,  -- 0=普通, 1=权威
    confidence_decay    REAL NOT NULL DEFAULT 1.0,  -- 置信度衰减信号 0.0~1.0
    last_hash_verified_at REAL DEFAULT NULL,    -- 最后一次 hash 验证时间戳
    node_type           TEXT NOT NULL DEFAULT 'self'  -- 'self'=自传晶体, 'file'=文件晶体
);
CREATE INDEX IF NOT EXISTS idx_fragments_weight ON memory_fragments(weight DESC);
CREATE INDEX IF NOT EXISTS idx_fragments_recalled ON memory_fragments(last_recalled);
CREATE INDEX IF NOT EXISTS idx_fragments_dirty ON memory_fragments(dirty);
CREATE INDEX IF NOT EXISTS idx_fragments_layer ON memory_fragments(layer);
CREATE INDEX IF NOT EXISTS idx_fragments_importance ON memory_fragments(importance DESC);
CREATE INDEX IF NOT EXISTS idx_fragments_epistemic ON memory_fragments(epistemic);
CREATE INDEX IF NOT EXISTS idx_fragments_node_type ON memory_fragments(node_type);
CREATE INDEX IF NOT EXISTS idx_fragments_authority ON memory_fragments(authority_level);
CREATE INDEX IF NOT EXISTS idx_fragments_crystal_parent ON memory_fragments(crystal_parent_id);
"""

SCHEMA_ARCHIVE = """
CREATE TABLE IF NOT EXISTS memory_archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fragment_id     INTEGER NOT NULL,
    text            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    embedding       TEXT NOT NULL DEFAULT '[]',
    importance      REAL NOT NULL DEFAULT 5.0,
    entity_ids      TEXT DEFAULT NULL,
    archived_at     REAL NOT NULL,
    source          TEXT DEFAULT 'decay'
);
CREATE INDEX IF NOT EXISTS idx_archive_entity ON memory_archive(entity_ids);
CREATE INDEX IF NOT EXISTS idx_archive_ts ON memory_archive(ts);
CREATE INDEX IF NOT EXISTS idx_archive_importance ON memory_archive(importance DESC);
CREATE INDEX IF NOT EXISTS idx_archive_fragment ON memory_archive(fragment_id);
"""

SCHEMA_LINKS = """
CREATE TABLE IF NOT EXISTS memory_links (
    from_id     INTEGER NOT NULL,
    to_id       INTEGER NOT NULL,
    link_type   TEXT NOT NULL DEFAULT 'similar',
    weight      REAL NOT NULL DEFAULT 1.0,
    created_at  REAL NOT NULL,
    PRIMARY KEY (from_id, to_id),
    FOREIGN KEY (from_id) REFERENCES memory_fragments(id),
    FOREIGN KEY (to_id) REFERENCES memory_fragments(id)
);
CREATE INDEX IF NOT EXISTS idx_links_from ON memory_links(from_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON memory_links(to_id);
"""

SCHEMA_REFLECTION_LOG = """
CREATE TABLE IF NOT EXISTS memory_reflection_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at    REAL NOT NULL,
    topic_ids       TEXT,
    input_chars     INTEGER DEFAULT 0,
    output_fragments INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0
);
"""

FULL_SCHEMA = SCHEMA_FRAGMENTS + SCHEMA_ARCHIVE + SCHEMA_LINKS + SCHEMA_REFLECTION_LOG


# ── FragmentStore ──────────────────────────────────

class FragmentStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or _get_db_path()
        self._ensure_schema()
        # v4.0: sqlite-vec KNN acceleration (graceful fallback to brute-force scan)
        self.vec_index = VecIndex(self.db_path)
        self._vec_table = "idx_fragments"
        self.vec_index.ensure_table(self._vec_table, EMBEDDING_DIM)

    def _ensure_schema(self):
        conn = sqlite3.connect(self.db_path)
        try:
            # Step 1: Create tables (IF NOT EXISTS so existing tables are left alone)
            # Separate table creation from index creation, because old tables may
            # not have v4.0 columns that indexes reference.
            _table_schemas = [
                "CREATE TABLE IF NOT EXISTS memory_fragments ("
                "    id              INTEGER PRIMARY KEY AUTOINCREMENT,"
                "    text            TEXT NOT NULL,"
                "    ts              TEXT NOT NULL,"
                "    embedding       TEXT NOT NULL DEFAULT '[]',"
                "    weight          REAL NOT NULL DEFAULT 1.0,"
                "    recall_count    INTEGER NOT NULL DEFAULT 0,"
                "    last_recalled   REAL,"
                "    created_at      REAL NOT NULL,"
                "    source          TEXT DEFAULT 'reflection',"
                "    topic_id        TEXT DEFAULT NULL,"
                "    user_tag        TEXT DEFAULT NULL,"
                "    parent_id       INTEGER DEFAULT NULL,"
                "    link_type       TEXT DEFAULT NULL,"
                "    tags            TEXT DEFAULT '',"
                "    dirty           INTEGER NOT NULL DEFAULT 1,"

                "    importance      REAL NOT NULL DEFAULT 5.0,"
                "    layer           TEXT NOT NULL DEFAULT 'core',"
                "    entity_ids      TEXT DEFAULT NULL,"
                "    summary_hash    TEXT DEFAULT NULL,"
                "    raw_count       INTEGER NOT NULL DEFAULT 1,"
                "    epistemic       TEXT NOT NULL DEFAULT 'experience',"

                "    crystal_parent_id   TEXT DEFAULT NULL,"
                "    raw_source_id       TEXT DEFAULT NULL,"
                "    authority_level     INTEGER NOT NULL DEFAULT 0,"
                "    confidence_decay    REAL NOT NULL DEFAULT 1.0,"
                "    last_hash_verified_at REAL DEFAULT NULL,"
                "    node_type           TEXT NOT NULL DEFAULT 'self'"
                ");",
            ]
            for tbl_sql in _table_schemas:
                try:
                    conn.execute(tbl_sql)
                except sqlite3.OperationalError as e:
                    print(f"[memory] Table creation warning (may be harmless): {e}")

            # Step 2: Create archive/links/reflection_log tables the same way
            for tbl_sql in SCHEMA_ARCHIVE.split(";"), SCHEMA_LINKS.split(";"), SCHEMA_REFLECTION_LOG.split(";"):
                for stmt in tbl_sql:
                    stmt = stmt.strip()
                    if stmt and stmt.upper().startswith("CREATE"):
                        try:
                            conn.execute(stmt)
                        except sqlite3.OperationalError as e:
                            print(f"[memory] Table/index creation warning: {e}")

            # Step 3: Migration — add v4.0 columns if missing (old table)
            _add_col_if_missing(conn, "memory_fragments", "importance", "REAL NOT NULL DEFAULT 5.0")
            _add_col_if_missing(conn, "memory_fragments", "layer", "TEXT NOT NULL DEFAULT 'core'")
            _add_col_if_missing(conn, "memory_fragments", "entity_ids", "TEXT DEFAULT NULL")
            _add_col_if_missing(conn, "memory_fragments", "summary_hash", "TEXT DEFAULT NULL")
            _add_col_if_missing(conn, "memory_fragments", "raw_count", "INTEGER NOT NULL DEFAULT 1")
            _add_col_if_missing(conn, "memory_fragments", "epistemic", "TEXT NOT NULL DEFAULT 'experience'")
            # Migration: old v3 fields (parent_id, link_type, tags)
            try:
                conn.execute("SELECT parent_id FROM memory_fragments LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE memory_fragments ADD COLUMN parent_id INTEGER DEFAULT NULL")
            try:
                conn.execute("SELECT link_type FROM memory_fragments LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE memory_fragments ADD COLUMN link_type TEXT DEFAULT NULL")
            try:
                conn.execute("SELECT tags FROM memory_fragments LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE memory_fragments ADD COLUMN tags TEXT DEFAULT ''")

            # CMN (P0) Migration — add 6 new fields for crystal memory network
            _add_col_if_missing(conn, "memory_fragments", "crystal_parent_id", "TEXT DEFAULT NULL")
            _add_col_if_missing(conn, "memory_fragments", "raw_source_id", "TEXT DEFAULT NULL")
            _add_col_if_missing(conn, "memory_fragments", "authority_level", "INTEGER NOT NULL DEFAULT 0")
            _add_col_if_missing(conn, "memory_fragments", "confidence_decay", "REAL NOT NULL DEFAULT 1.0")
            _add_col_if_missing(conn, "memory_fragments", "last_hash_verified_at", "REAL DEFAULT NULL")
            _add_col_if_missing(conn, "memory_fragments", "node_type", "TEXT NOT NULL DEFAULT 'self'")

            # CMN (P6) Migration — story 层：叙事记忆的三时间字段 + 素材来源
            _add_col_if_missing(conn, "memory_fragments", "event_time", "TEXT DEFAULT NULL")
            _add_col_if_missing(conn, "memory_fragments", "event_span", "TEXT DEFAULT NULL")
            _add_col_if_missing(conn, "memory_fragments", "source_ids", "TEXT DEFAULT NULL")

            # Step 4: Create indexes (after migration ensures columns exist)
            _index_schemas = [
                "CREATE INDEX IF NOT EXISTS idx_fragments_weight ON memory_fragments(weight DESC)",
                "CREATE INDEX IF NOT EXISTS idx_fragments_recalled ON memory_fragments(last_recalled)",
                "CREATE INDEX IF NOT EXISTS idx_fragments_dirty ON memory_fragments(dirty)",
                "CREATE INDEX IF NOT EXISTS idx_fragments_layer ON memory_fragments(layer)",
                "CREATE INDEX IF NOT EXISTS idx_fragments_importance ON memory_fragments(importance DESC)",
                "CREATE INDEX IF NOT EXISTS idx_fragments_epistemic ON memory_fragments(epistemic)",
                # CMN (P0) indexes
                "CREATE INDEX IF NOT EXISTS idx_fragments_node_type ON memory_fragments(node_type)",
                "CREATE INDEX IF NOT EXISTS idx_fragments_authority ON memory_fragments(authority_level)",
                "CREATE INDEX IF NOT EXISTS idx_fragments_crystal_parent ON memory_fragments(crystal_parent_id)",
            ]
            for idx_sql in _index_schemas:
                try:
                    conn.execute(idx_sql)
                except sqlite3.OperationalError as e:
                    print(f"[memory] Index creation warning: {e}")

            conn.commit()
        finally:
            conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    def add(self, text, ts=None, source="reflection", topic_id=None, user_tag=None, tags="",
            parent_id=None, link_type=None,
            importance=5.0, epistemic="experience", entity_ids=None,
            crystal_parent_id=None, raw_source_id=None,
            authority_level=0, confidence_decay=1.0,
            last_hash_verified_at=None, node_type="self", layer="core",
            event_time=None, event_span=None, source_ids=None):
        """Add a fragment. Returns fragment id.

        v4.0: supports importance, epistemic, entity_ids.
        CMN P3: supports crystal_parent_id, raw_source_id, authority_level, confidence_decay,
                last_hash_verified_at, node_type.
        CMN P5: supports layer="knowledge" — 成体系知识晶体（AI 主动存的整体理解）。
                knowledge 层默认 importance≥7.0，算 summary_hash 供查重。
        CMN P6: supports layer="story" — 叙事记忆（情景记忆，带时间+因果）。
                story 层默认 importance≥6.5，支持 event_time/event_span/source_ids 三字段。
        Dedup: exact text → reinforce weight; near-duplicate (sim>0.85) → reinforce.
        """
        text = text.strip()
        if len(text) < FRAGMENT_MIN_CHARS:
            return None
        if ts is None:
            ts = time.strftime("%Y%m%d%H%M%S")
        now = time.time()

        # knowledge 层：默认 importance 提到 7.0（成体系知识比碎片重要）
        # story 层：默认 importance 提到 6.5（情景叙事比碎片重要，比 knowledge 略低）
        # 调用方显式传更高值时尊重调用方
        if layer == "knowledge" and importance < 7.0:
            importance = 7.0
        if layer == "story" and importance < 6.5:
            importance = 6.5

        new_emb = embed(text)
        emb_json = json.dumps(new_emb)
        entity_ids_str = json.dumps(entity_ids or [])

        conn = self._conn()
        try:
            # ── Dedup 仅对 core 层生效 ──
            # knowledge/story 层是成体系记忆，文本天然含素材内容，
            # 不应被碎片 dedup 拦截（否则 story 会被误判为"相似碎片"而 reinforce 旧记录）
            if layer == "core":
                # ── Exact text dedup ──
                exact = conn.execute(
                    "SELECT id, weight, importance, raw_count FROM memory_fragments WHERE dirty=1 AND text=?",
                    (text,)
                ).fetchone()
                if exact:
                    new_weight = min(exact["weight"] + 0.15, WEIGHT_CAP)
                    new_importance = max(exact["importance"], importance)
                    new_raw = exact["raw_count"] + 1
                    conn.execute(
                        "UPDATE memory_fragments SET weight=?, importance=?, raw_count=?, last_recalled=? WHERE id=?",
                        (new_weight, new_importance, new_raw, now, exact["id"])
                    )
                    conn.commit()
                    return exact["id"]

                # Check for near-duplicates
                existing = conn.execute(
                    "SELECT id, text, embedding, weight, importance, entity_ids FROM memory_fragments"
                    " WHERE dirty=1 OR (dirty=0 AND created_at > ?)",
                    (now - 3600,)
                ).fetchall()

                best_id, best_sim = None, 0.0
                for row in existing:
                    old_emb = json.loads(row["embedding"])
                    if not old_emb or len(old_emb) == 0:
                        continue
                    sim = cosine_sim(new_emb, old_emb)
                    if sim > best_sim:
                        best_sim = sim
                        best_id = row["id"]

                if best_sim > 0.85:
                    best_row = conn.execute(
                        "SELECT weight, importance, raw_count FROM memory_fragments WHERE id=?", (best_id,)
                    ).fetchone()
                    if best_row:
                        new_weight = min(best_row["weight"] + WEIGHT_REINFORCE, WEIGHT_CAP)
                        new_importance = max(best_row["importance"], importance)
                        new_raw = best_row["raw_count"] + 1
                        conn.execute(
                            "UPDATE memory_fragments SET weight=?, importance=?, raw_count=?, last_recalled=? WHERE id=?",
                            (new_weight, new_importance, new_raw, now, best_id)
                        )
                    conn.commit()
                    return best_id

            # parent_id / link_type（core 层才走相似关联，knowledge/story 层直接 INSERT）
            if parent_id is None and layer == "core":
                if best_sim > 0.70:
                    row = conn.execute("SELECT weight FROM memory_fragments WHERE id=?", (best_id,)).fetchone()
                    if row:
                        new_w = min(row["weight"] + WEIGHT_BOOST, WEIGHT_CAP)
                        conn.execute("UPDATE memory_fragments SET weight=? WHERE id=?", (new_w, best_id))
                    parent_id = best_id
                    link_type = "causal" if source == "causal" else "similar"

            # Insert
            # knowledge 层算 summary_hash 供查重（hash 寻址，AI 无感）
            summary_hash = None
            if layer == "knowledge":
                import hashlib
                summary_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

            # story 层：source_ids 序列化为 JSON
            source_ids_str = json.dumps(source_ids) if source_ids else None

            cur = conn.execute(
                """INSERT INTO memory_fragments
                   (text, ts, embedding, weight, recall_count, last_recalled, created_at,
                    source, topic_id, user_tag, parent_id, link_type, tags,
                    importance, layer, entity_ids, summary_hash, raw_count, epistemic,
                    crystal_parent_id, raw_source_id, authority_level, confidence_decay,
                    last_hash_verified_at, node_type,
                    event_time, event_span, source_ids)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (text, ts, emb_json, WEIGHT_DEFAULT, 0, None, now,
                 source, topic_id, user_tag, parent_id, link_type, tags,
                 importance, layer, entity_ids_str, summary_hash, 1, epistemic,
                 crystal_parent_id, raw_source_id, authority_level, confidence_decay,
                 last_hash_verified_at, node_type,
                 event_time, event_span, source_ids_str)
            )
            new_id = cur.lastrowid

            # Create link in graph table
            if parent_id and new_id:
                conn.execute(
                    """INSERT OR IGNORE INTO memory_links (from_id, to_id, link_type, weight, created_at)
                       VALUES (?,?,?,?,?)""",
                    (parent_id, new_id, link_type, best_sim, now)
                )
                conn.execute(
                    """INSERT OR IGNORE INTO memory_links (from_id, to_id, link_type, weight, created_at)
                       VALUES (?,?,?,?,?)""",
                    (new_id, parent_id, "reverse", best_sim, now)
                )
            conn.commit()

            # v4.0: sync to vec_index for KNN acceleration
            # 注意：必须在 conn.commit() 之后写 vec 索引！vec_index 内部用独立连接，
            # 若在主事务未提交时写入 idx_fragments，两连接互相等锁 → database is locked 死锁。
            # vec 索引可随时 rebuild，失败只影响 KNN 加速，不影响主数据写入。
            if new_emb and len(new_emb) > 0:
                try:
                    self.vec_index.insert(self._vec_table, new_id, new_emb)
                except Exception as e:
                    print(f"[memory] vec_index sync skipped: {e}")

            return new_id
        finally:
            conn.close()

    def recall(self, context_text, top_k=RECALL_TOP_K, threshold=RECALL_THRESHOLD,
               query_entities=None, layer="core", topic_id=None):
        """v4.0: 5-factor recall with entity match bonus.

        Factors: recency × relevance × importance × weight × entity_match.
        MMR dedup (λ=0.7).
        When entity_ids hit, also propagates causal edges one hop (confidence ≥ 0.8).
        Returns list of fragment dicts.
        """
        if not context_text or not context_text.strip():
            return self._anchors(3, layer=layer)

        ctx_emb = embed(context_text)
        if not ctx_emb or all(v == 0 for v in ctx_emb):
            return self._anchors(top_k, layer=layer)

        conn = self._conn()
        try:
            now = time.time()
            rows = self._fetch_candidates(conn, ctx_emb, top_k, layer, topic_id)

            scored = []
            for row in rows:
                emb = json.loads(row["embedding"])
                if not emb or len(emb) == 0:
                    continue
                relevance = cosine_sim(ctx_emb, emb)
                # relevance 硬门槛：低于此值直接淘汰，importance/weight 救不回来
                # 这是防"元晶体霸榜"的关键——元晶体因结构化文本 embedding 跟任何查询都
                # 有点相似（0.1-0.3），但实际不相关，靠 importance=7/weight=3 综合分顶上来
                if relevance < RELEVANCE_HARD_FLOOR:
                    continue

                # 5-factor scoring
                score = self._five_factor_score(
                    row, relevance, now, query_entities=query_entities
                )
                if score >= threshold:
                    frag = dict(row)
                    frag["_relevance"] = relevance  # 存真 cosine，供 MMR 和下游用
                    scored.append((frag, score))

            scored.sort(key=lambda x: x[1], reverse=True)

            # MMR selection
            selected = self._mmr_select(scored, top_k, conn, now)

            # ── Causal propagation: for each entity in hits, follow causal edge one hop ──
            if query_entities:
                causal_neighbors = self._fetch_causal_neighbors(selected, query_entities, conn)
                if causal_neighbors:
                    for cn in causal_neighbors:
                        cn["_causal_propagation"] = True
                    selected.extend(causal_neighbors)
                    # Re-dedup by id
                    seen_ids = set()
                    deduped = []
                    for f in selected:
                        if f["id"] not in seen_ids:
                            seen_ids.add(f["id"])
                            deduped.append(f)
                    selected = deduped[:top_k + 2]  # causal = extra budget

            conn.commit()
            return selected
        finally:
            conn.close()

    def _fetch_candidates(self, conn, ctx_emb, top_k, layer, topic_id):
        """用 vec_index KNN 预筛候选碎片；不可用时回退全表扫描。

        KNN 拿 top_k*4 候选（多取一些留给 threshold/MMR 过滤），
        再从 memory_fragments 拉详情。五因子打分仍用 Python 端 cosine_sim
        重算 relevance，保证评分一致性。

        layer='core' 时自动包含 knowledge 层（成体系知识自然浮出，AI 无感）。
        """
        # core 层查询自动带上 knowledge + story 层（成体系知识和叙事自然浮出，AI 无感）
        layers = ("core", "knowledge", "story") if layer == "core" else (layer,)
        layer_ph = ",".join("?" for _ in layers)

        if self.vec_index.is_available():
            knn = self.vec_index.query(self._vec_table, ctx_emb, top_k=max(top_k * 4, 20))
            if knn:
                rowids = [r["rowid"] for r in knn]
                placeholders = ",".join("?" for _ in rowids)
                where = f"id IN ({placeholders}) AND dirty=1 AND layer IN ({layer_ph})"
                params = rowids + list(layers)
                if topic_id:
                    where += " AND (topic_id=? OR topic_id IS NULL)"
                    params.append(topic_id)
                rows = conn.execute(
                    f"SELECT * FROM memory_fragments WHERE {where}", params
                ).fetchall()
                if rows:
                    return rows
        # 回退：全表扫描
        params = list(layers)
        where_clauses = [f"dirty=1", f"layer IN ({layer_ph})"]
        if topic_id:
            where_clauses.append("(topic_id=? OR topic_id IS NULL)")
            params.append(topic_id)
        return conn.execute(
            "SELECT * FROM memory_fragments WHERE " + " AND ".join(where_clauses), params
        ).fetchall()

    def rebuild_index(self):
        """重建 vec_index。用于已有数据迁移或维度变更。返回入库条数。"""
        if not self.vec_index.is_available():
            return 0
        conn = self._conn()
        try:
            try:
                conn.execute(f"DELETE FROM {self._vec_table}")
            except sqlite3.OperationalError:
                pass
            conn.commit()
            rows = conn.execute(
                "SELECT id, embedding FROM memory_fragments WHERE dirty=1"
            ).fetchall()
            count = 0
            for r in rows:
                emb = json.loads(r["embedding"])
                if emb and len(emb) > 0:
                    self.vec_index.insert(self._vec_table, r["id"], emb)
                    count += 1
            return count
        finally:
            conn.close()

    def _five_factor_score(self, row, relevance, now, query_entities=None):
        """recency × relevance × importance × weight × entity_bonus."""
        # 1. Recency
        created_at = row["created_at"]
        age_days = (now - created_at) / 86400.0
        recency = max(0.5 ** (age_days / DECAY_HALF_LIFE_DAYS), DECAY_FLOOR)

        # 2. Relevance (already computed)

        # 3. Importance (1-10 → 0.1-1.0)
        importance = row["importance"] / 10.0

        # 4. Weight (with raw_count correction)
        weight = row["weight"] * (1 + 0.1 * min(row["raw_count"], 10))

        # 5. Entity match bonus
        entity_bonus = 1.0
        if query_entities:
            frag_entities = json.loads(row["entity_ids"] or "[]")
            if frag_entities and any(eid in frag_entities for eid in query_entities):
                entity_bonus = ENTITY_BONUS

        return recency * relevance * importance * weight * entity_bonus

    def _mmr_select(self, scored, top_k, conn=None, now=None):
        """MMR (Maximal Marginal Relevance) selection.

        scored: [(fragment_dict, score), ...]  fragment 内含 _relevance（真 cosine）
        """
        selected = []
        for _ in range(top_k):
            best, best_mmr = None, -1.0
            for f, score in scored:
                if f["id"] in {s["id"] for s in selected}:
                    continue
                # MMR：λ × relevance - (1-λ) × max_dup
                # 用真 relevance（cosine）而非综合分，保证 MMR 多样性基于真实相似度
                rel = f.get("_relevance", score)
                max_dup = max(
                    (cosine_sim(json.loads(s.get("embedding","[]")), json.loads(f.get("embedding","[]"))) for s in selected),
                    default=0.0
                )
                mmr = MMR_LAMBDA * rel - (1 - MMR_LAMBDA) * max_dup
                if mmr > best_mmr:
                    best_mmr = mmr
                    best = f
            if best is None:
                break
            # 字段清晰化：
            # - relevance: 真 cosine 相似度（0~1）
            # - score: 综合分（recency × relevance × importance × weight × entity）
            # - similarity: 已废弃（曾误导下游以为是 cosine）
            best["relevance"] = best.get("_relevance", 0.0)
            best["score"] = best_mmr
            best.pop("similarity", None)  # 清除旧误导字段
            best.pop("_relevance", None)  # 清理临时字段
            selected.append(best)
            if conn and now is not None:
                conn.execute(
                    "UPDATE memory_fragments SET recall_count=recall_count+1, last_recalled=? WHERE id=?",
                    (now, best["id"])
                )
        return selected

    def _fetch_causal_neighbors(self, selected, query_entities, conn):
        """Follow causal edges one hop from hit entities, confidence ≥ 0.8.

        语义：召回的碎片命中了 query_entities 里的实体，
        顺着这些实体的 causal 边找到对端实体，
        再把对端实体相关的碎片召回（最多 3 条）。
        """
        if not query_entities:
            return []
        try:
            selected_ids = {f["id"] for f in selected}
            if not selected_ids:
                return []
            # Check if memory_relations table exists
            has_relations = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_relations'"
            ).fetchone()
            if not has_relations:
                return []

            # 1. 查 causal 边，找对端实体id（subject 或 object 任一端命中均可）
            placeholders = ",".join("?" for _ in query_entities)
            rows = conn.execute(
                """SELECT subject_id, object_id FROM memory_relations
                   WHERE (subject_id IN ({}) OR object_id IN ({}))
                       AND edge_type='causal' AND confidence >= ?
                       AND status='active'
                   LIMIT 20""".format(placeholders, placeholders),
                list(query_entities) + list(query_entities) + [CAUSAL_PROPAGATION_MIN_CONFIDENCE]
            ).fetchall()

            # 收集对端实体id（排除已命中的 query_entities 自身）
            neighbor_entity_ids = set()
            for r in rows:
                obj_id = r["object_id"]
                sub_id = r["subject_id"]
                if obj_id and obj_id not in query_entities:
                    neighbor_entity_ids.add(obj_id)
                if sub_id and sub_id not in query_entities:
                    neighbor_entity_ids.add(sub_id)
            if not neighbor_entity_ids:
                return []

            # 2. 从 memory_fragments 查对端实体关联的碎片
            result = []
            for eid in neighbor_entity_ids:
                # entity_ids 是 JSON 数组，LIKE 粗筛 + Python 精筛避免误匹配
                frag_rows = conn.execute(
                    """SELECT * FROM memory_fragments
                       WHERE layer='core' AND dirty=1
                           AND entity_ids LIKE ?
                       LIMIT 5""",
                    (f'%{eid}%',)
                ).fetchall()
                for r in frag_rows:
                    d = dict(r)
                    if d["id"] in selected_ids:
                        continue
                    frag_entities = json.loads(d.get("entity_ids") or "[]")
                    if eid not in frag_entities:
                        continue
                    d["_causal_score"] = d["importance"] / 10.0
                    result.append(d)
                    selected_ids.add(d["id"])
                    if len(result) >= 3:
                        return result
            return result
        except Exception:
            return []

    def recall_archive(self, context_text, top_k=RECALL_ARCHIVE_TOP_K,
                       threshold=RECALL_ARCHIVE_THRESHOLD):
        """Recall from archive layer. If hit with importance ≥ 7, reactivate to core."""
        if not context_text or not context_text.strip():
            return []

        ctx_emb = embed(context_text)
        if not ctx_emb or all(v == 0 for v in ctx_emb):
            return []

        conn = self._conn()
        try:
            now = time.time()
            rows = conn.execute(
                "SELECT * FROM memory_archive"
            ).fetchall()

            scored = []
            for row in rows:
                emb = json.loads(row["embedding"])
                if not emb or len(emb) == 0:
                    continue
                sim = cosine_sim(ctx_emb, emb)
                score = sim * (row["importance"] / 10.0)
                if score >= threshold:
                    scored.append((dict(row), score))

            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:top_k]

            # Activate back to core if importance ≥ pin threshold
            activated = []
            for entry, score in top:
                if entry["importance"] >= IMPORTANCE_PIN_THRESHOLD:
                    # Check not already in core
                    existing = conn.execute(
                        "SELECT id FROM memory_fragments WHERE dirty=1 AND text=? AND layer='core'",
                        (entry["text"],)
                    ).fetchone()
                    if not existing:
                        cur = conn.execute(
                            """INSERT INTO memory_fragments
                               (text, ts, embedding, weight, importance, layer, entity_ids,
                                raw_count, epistemic, created_at, source)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (entry["text"], entry["ts"], entry["embedding"],
                             WEIGHT_DEFAULT, entry["importance"], "core",
                             entry["entity_ids"], 1, "experience", now, "archive_reactivated")
                        )
                        activated.append(cur.lastrowid)
            conn.commit()
            return [dict(e[0]) for e in top]
        finally:
            conn.close()

    def move_to_archive(self, fragment_id):
        """Move a fragment from core layer to archive layer."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM memory_fragments WHERE id=? AND dirty=1",
                (fragment_id,)
            ).fetchone()
            if not row:
                return False
            now = time.time()
            conn.execute(
                """INSERT INTO memory_archive
                   (fragment_id, text, ts, embedding, importance, entity_ids, archived_at, source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row["id"], row["text"], row["ts"], row["embedding"],
                 row["importance"], row["entity_ids"], now, "decay")
            )
            # Mark as archival layer (not deleted)
            conn.execute(
                "UPDATE memory_fragments SET layer='archival', last_recalled=? WHERE id=?",
                (now, fragment_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def move_to_core(self, fragment_id):
        """Reactivate an archival fragment back to core."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM memory_fragments WHERE id=? AND dirty=1",
                (fragment_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE memory_fragments SET layer='core', weight=? WHERE id=?",
                (WEIGHT_DEFAULT, fragment_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def prune(self):
        """v4.0: No hard cap. Bidirectional core↔archival migration.

        - Core fragments: 30 days no recall + importance < 7 → archive
        - Archive fragments: never deleted
        - Entities: deactivation handled by entity_store separately
        Returns count of migrated fragments.
        """
        conn = self._conn()
        try:
            now = time.time()
            migrated = 0

            # Core → archival: 30 days no recall + importance < pin threshold
            stale_core = conn.execute(
                """SELECT id, importance, last_recalled, created_at
                   FROM memory_fragments
                   WHERE dirty=1 AND layer='core'"""
            ).fetchall()

            for row in stale_core:
                last_touch = max(row["last_recalled"] or 0, row["created_at"])
                age_days = (now - last_touch) / 86400.0
                if age_days > ACTIVE_RECENCY_DAYS and row["importance"] < IMPORTANCE_PIN_THRESHOLD:
                    self.move_to_archive(row["id"])
                    migrated += 1

            conn.commit()
            return migrated
        finally:
            conn.close()

    def _anchors(self, n=3, layer="core"):
        """Get top-n highest-weight fragments (identity anchors)."""
        # core 层包含 knowledge + story 层（同 _fetch_candidates 逻辑）
        layers = ("core", "knowledge", "story") if layer == "core" else (layer,)
        layer_ph = ",".join("?" for _ in layers)
        conn = self._conn()
        try:
            now = time.time()
            cutoff = now - 30 * 86400
            rows = conn.execute(
                f"""SELECT * FROM memory_fragments
                   WHERE dirty=1 AND layer IN ({layer_ph}) AND created_at > ?
                   ORDER BY weight DESC LIMIT ?""",
                list(layers) + [cutoff, n]
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Backward compat: v3 methods ─────────────────

    def recall_by_topic(self, topic_id, limit=10):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_fragments WHERE dirty=1 AND topic_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (topic_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_fragment_id(self, topic_id):
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id FROM memory_fragments WHERE dirty=1 AND topic_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (topic_id,)
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def _effective_weight(self, weight, created_at, now):
        age_days = (now - created_at) / 86400.0
        decay = 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
        return max(weight * decay, DECAY_FLOOR * weight)

    def reinforce(self, keyword, boost=0.5, cap=5.0):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, text, weight FROM memory_fragments WHERE dirty=1 AND text LIKE ?",
                (f"%{keyword}%",)
            ).fetchall()
            count = 0
            for row in rows:
                new_w = min(row["weight"] + boost, cap)
                conn.execute("UPDATE memory_fragments SET weight=? WHERE id=?", (new_w, row["id"]))
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    # ── CMN P3/P4: 晶体字段管理 ───────────────────────

    def update_cmn_fields(self, fragment_id, **kwargs):
        """更新晶体的 CMN 字段。

        供反思回路（P4）提拔权威、衰减置信度等使用。
        支持的字段：authority_level, confidence_decay, last_hash_verified_at,
                   crystal_parent_id, raw_source_id, node_type。

        安全约束：AI 工具不能直接调此方法设 authority_level=1，
        只能通过反思回路（P4）的提拔逻辑间接设。
        """
        allowed = {"authority_level", "confidence_decay", "last_hash_verified_at",
                   "crystal_parent_id", "raw_source_id", "node_type"}
        updates = []
        vals = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k}=?")
                vals.append(v)
        if not updates:
            return False
        vals.append(time.time())  # updated_at
        updates.append("last_recalled=?")
        # last_recalled 不该被这里改，改回 created_at 逻辑——其实用 last_recalled 作"最后访问"也行
        # 修正：只更新 updated_at 风格，加个 updated_at 字段更新
        updates[-1] = "last_recalled=?"
        # 简化：用 last_recalled 兼容（表里没 updated_at）
        vals[-1] = time.time()
        vals.append(fragment_id)
        conn = self._conn()
        try:
            conn.execute(
                f"UPDATE memory_fragments SET {', '.join(updates)} WHERE id=?",
                vals
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def promote_to_authority(self, fragment_id) -> bool:
        """将普通晶体提拔为权威晶体（P4 反思回路调用）。"""
        return self.update_cmn_fields(fragment_id, authority_level=1)

    def link_cores_to_knowledge(self, knowledge_id: int, topic_tag: str = None,
                                 limit: int = 15) -> int:
        """把同主题的 core 碎片关联到 knowledge 晶体（建立血统）。

        匹配策略（OR，取并集）：
        1. entity_ids 有重叠（强信号，实体共现）
        2. embedding 语义相似度 ≥ 0.72（强信号，主题真正相关）
        3. tags 精确包含（兜底）

        注：中文 embedding 基线相似度高（无关内容也常 0.55+），
        阈值必须够高（0.72）才能筛出真相关的，否则全是噪声。
        tags 匹配也常失效——AI 存 knowledge 填"主题"（长词），
        存 core 填"标签"（短词），语义不对齐。

        只关联 layer='core' 且 crystal_parent_id 为空的碎片（不覆盖已有血统）。
        返回关联条数。
        """
        conn = self._conn()
        try:
            now = time.time()
            count = 0

            # 取 knowledge 晶体
            krow = conn.execute(
                "SELECT id, text, embedding, entity_ids, tags FROM memory_fragments "
                "WHERE id=? AND layer='knowledge'",
                (knowledge_id,)
            ).fetchone()
            if not krow:
                return 0

            matched_ids = set()

            # 策略1：entity_ids 重叠（最强信号）
            try:
                k_entities = set(json.loads(krow["entity_ids"] or "[]"))
            except Exception:
                k_entities = set()
            if k_entities:
                rows = conn.execute(
                    """SELECT id, entity_ids FROM memory_fragments
                        WHERE layer='core' AND dirty=1
                        AND crystal_parent_id IS NULL
                        AND entity_ids IS NOT NULL"""
                ).fetchall()
                for r in rows:
                    try:
                        r_entities = set(json.loads(r["entity_ids"] or "[]"))
                    except Exception:
                        continue
                    if r_entities & k_entities:
                        matched_ids.add(r["id"])

            # 策略2：embedding 语义相似（主路径）
            try:
                from .embedder import cosine_sim
                k_emb = json.loads(krow["embedding"] or "[]")
                if k_emb and len(k_emb) > 0:
                    rows = conn.execute(
                        """SELECT id, embedding FROM memory_fragments
                           WHERE layer='core' AND dirty=1
                           AND crystal_parent_id IS NULL
                           AND embedding IS NOT NULL
                           AND embedding != '[]'"""
                    ).fetchall()
                    for r in rows:
                        if r["id"] in matched_ids:
                            continue
                        try:
                            r_emb = json.loads(r["embedding"] or "[]")
                        except Exception:
                            continue
                        if not r_emb or len(r_emb) == 0:
                            continue
                        sim = cosine_sim(k_emb, r_emb)
                        if sim >= 0.72:
                            matched_ids.add(r["id"])
            except Exception as e:
                print(f"[memory] link_cores embedding match skipped: {e}")

            # 策略3：tags 精确匹配（兜底）
            if topic_tag:
                rows = conn.execute(
                    """SELECT id FROM memory_fragments
                       WHERE layer='core' AND dirty=1
                       AND crystal_parent_id IS NULL
                       AND (tags LIKE ? OR tags LIKE ? OR tags LIKE ?)""",
                    (topic_tag, f"{topic_tag},%", f"%,{topic_tag}")
                ).fetchall()
                matched_ids.update(r["id"] for r in rows)

            # 限制条数（避免一个 knowledge 关联太多）
            matched_ids = list(matched_ids)[:limit]

            # 批量更新
            for fid in matched_ids:
                conn.execute(
                    "UPDATE memory_fragments SET crystal_parent_id=?, last_recalled=? WHERE id=?",
                    (str(knowledge_id), now, fid)
                )
                count += 1

            conn.commit()
            return count
        finally:
            conn.close()

    def link_materials(self, story_id: int, source_ids: list) -> int:
        """把素材碎片关联到 story 并归档（P6 叙事沉淀用）。

        更新碎片的 crystal_parent_id 指向 story，同时 dirty=0 归档——
        碎片不再参与常规召回（recall/search_by_text/get_chain 默认 dirty=1），
        但仍保留在 memory_fragments 表里，trace_memory 主动追溯时可调出。
        返回归档条数。
        """
        if not source_ids:
            return 0
        conn = self._conn()
        try:
            now = time.time()
            count = 0
            for fid in source_ids:
                cur = conn.execute(
                    "UPDATE memory_fragments SET crystal_parent_id=?, dirty=0, last_recalled=? "
                    "WHERE id=? AND layer='core' AND dirty=1",
                    (str(story_id), now, fid)
                )
                count += cur.rowcount
            conn.commit()
            return count
        finally:
            conn.close()

    def find_story_by_topic(self, topic: str, topic_id: str = None,
                            topic_emb: list = None,
                            similarity_threshold: float = 0.72) -> dict:
        """找同主题已存在的 story（P6 故事唯一化用）。

        匹配优先级：
        1. topic_id 完全相同（同任务下的故事）
        2. tags 字段完全相同（同主题标签）
        3. embedding 相似度 ≥ threshold（语义同主题）

        threshold 默认 0.72——和碎片聚类阈值（STORY_CONSOLIDATION_THRESHOLD）
        一致：碎片间 0.72 算同主题，story 间也用同标准判断"同主题"。

        topic_emb 参数（推荐）：直接传入碎片组的 embedding 均值。
        比用 topic 文本重新 embed 更准——因为 topic 通常是碎片前20字，
        短文本和 story 长叙事文本的 embedding 相似度天然偏低。
        用碎片组 embedding 均值匹配 story embedding，是同维度对同维度。

        返回最相关的一条 story dict，没有则返回 None。
        """
        conn = self._conn()
        try:
            # 1. topic_id 精确匹配
            if topic_id:
                row = conn.execute(
                    "SELECT * FROM memory_fragments WHERE layer='story' AND dirty=1 "
                    "AND topic_id=? ORDER BY created_at DESC LIMIT 1",
                    (topic_id,)
                ).fetchone()
                if row:
                    return dict(row)

            # 2. tags 精确匹配
            if topic:
                row = conn.execute(
                    "SELECT * FROM memory_fragments WHERE layer='story' AND dirty=1 "
                    "AND tags=? ORDER BY created_at DESC LIMIT 1",
                    (topic,)
                ).fetchone()
                if row:
                    return dict(row)

            # 3. embedding 相似度匹配
            # 优先用传入的 topic_emb（碎片组均值），否则用 topic 文本 embed
            from .embedder import embed, cosine_sim
            query_emb = topic_emb
            if not query_emb and topic:
                query_emb = embed(topic)
            if query_emb:
                rows = conn.execute(
                    "SELECT * FROM memory_fragments WHERE layer='story' AND dirty=1 "
                    "ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
                best, best_sim = None, 0.0
                for r in rows:
                    try:
                        emb = json.loads(r["embedding"])
                        if not emb:
                            continue
                        sim = cosine_sim(query_emb, emb)
                        if sim > best_sim:
                            best_sim, best = sim, dict(r)
                    except Exception:
                        continue
                if best and best_sim >= similarity_threshold:
                    return best
            return None
        finally:
            conn.close()

    def update_story(self, story_id: int, text: str, source_ids: list,
                     event_time: str = None, event_span: str = None,
                     tags: str = None) -> bool:
        """更新已有 story 的内容和素材来源（P6 故事唯一化用）。

        同主题新碎片进来时，重新叙事后用这个方法更新 story：
        - text/event_time/event_span/tags 更新为合并后的新值
        - source_ids 追加新碎片 id（去重）
        - 重新计算 embedding
        - importance 微涨（持续积累的话题更重要）
        """
        from .embedder import embed
        conn = self._conn()
        try:
            # 取现有 source_ids，追加新的
            row = conn.execute(
                "SELECT source_ids, importance FROM memory_fragments WHERE id=? AND layer='story'",
                (story_id,)
            ).fetchone()
            if not row:
                return False

            existing_ids = []
            try:
                existing_ids = json.loads(row["source_ids"]) if row["source_ids"] else []
            except Exception:
                existing_ids = []
            merged_ids = list(dict.fromkeys(existing_ids + source_ids))  # 去重保序

            new_emb = embed(text)
            emb_json = json.dumps(new_emb) if new_emb else "[]"
            new_importance = min(10.0, (row["importance"] or 6.5) + 0.2)

            conn.execute(
                """UPDATE memory_fragments
                   SET text=?, embedding=?, source_ids=?, event_time=?, event_span=?,
                       tags=?, importance=?, last_recalled=?, recall_count=recall_count+1
                   WHERE id=?""",
                (text, emb_json, json.dumps(merged_ids), event_time, event_span,
                 tags, new_importance, time.time(), story_id)
            )
            conn.commit()

            # 同步 vec_index（先删后插）
            if new_emb:
                try:
                    self.vec_index.delete(self._vec_table, story_id)
                except Exception:
                    pass
                try:
                    self.vec_index.insert(self._vec_table, story_id, new_emb)
                except Exception as e:
                    print(f"[memory] vec_index sync skipped on update: {e}")
            return True
        finally:
            conn.close()

    # ── 观察整合（吸取 hindsight consolidation） ────────────────

    def find_similar_knowledge(self, text: str, threshold: float = 0.72,
                               limit: int = 3) -> list:
        """找与文本相似的 observation（knowledge 层）。

        hindsight 的 consolidator 用向量检索候选观察，这里等价实现：
        对 knowledge 层碎片做 cosine 相似度匹配，返回 ≥threshold 的观察。
        """
        from .embedder import embed
        query_emb = embed(text)
        if not query_emb:
            return []
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_fragments WHERE layer='knowledge' AND dirty=1 "
                "ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            results = []
            for r in rows:
                try:
                    emb = json.loads(r["embedding"]) if r["embedding"] else []
                    if not emb:
                        continue
                    sim = cosine_sim(query_emb, emb)
                    if sim >= threshold:
                        results.append({
                            "id": r["id"],
                            "text": r["text"],
                            "raw_count": r["raw_count"] or 1,
                            "sim": round(sim, 4),
                        })
                except Exception:
                    continue
            results.sort(key=lambda x: -x["sim"])
            return results[:limit]
        finally:
            conn.close()

    def update_observation(self, obs_id: int, new_text: str,
                           new_source_id: int = None) -> bool:
        """更新观察内容并累加证据数（hindsight update 动作）。

        更新前把旧文本归档到 memory_archive（可逆），
        source_ids 追加新碎片 id，raw_count+1，重新算 embedding。
        """
        from .embedder import embed
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT text, source_ids, raw_count FROM memory_fragments "
                "WHERE id=? AND layer='knowledge'",
                (obs_id,)
            ).fetchone()
            if not row:
                return False

            # 旧文本进 archive（可逆兜底）
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_archive (fragment_id, text, ts, source, archived_at) "
                    "VALUES (?,?,?,?,?)",
                    (obs_id, row["text"], time.strftime("%Y%m%d%H%M%S"),
                     "observation_update", time.time())
                )
            except Exception:
                pass

            existing_ids = []
            try:
                existing_ids = json.loads(row["source_ids"]) if row["source_ids"] else []
            except Exception:
                existing_ids = []
            if new_source_id:
                existing_ids.append(new_source_id)
            merged_ids = list(dict.fromkeys(existing_ids))

            new_emb = embed(new_text)
            emb_json = json.dumps(new_emb) if new_emb else "[]"
            new_raw = (row["raw_count"] or 1) + 1

            conn.execute(
                """UPDATE memory_fragments
                   SET text=?, embedding=?, source_ids=?, raw_count=?,
                       importance=MIN(10.0, importance+0.1), last_recalled=?
                   WHERE id=?""",
                (new_text, emb_json, json.dumps(merged_ids), new_raw,
                 time.time(), obs_id)
            )
            conn.commit()

            # 同步 vec_index
            if new_emb:
                try:
                    self.vec_index.delete(self._vec_table, obs_id)
                except Exception:
                    pass
                try:
                    self.vec_index.insert(self._vec_table, obs_id, new_emb)
                except Exception:
                    pass
            return True
        finally:
            conn.close()

    def bump_observation(self, obs_id: int, new_source_id: int = None) -> bool:
        """观察证据数+1，不改变内容（hindsight merge 动作：重复碎片只加证据）。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT source_ids FROM memory_fragments WHERE id=? AND layer='knowledge'",
                (obs_id,)
            ).fetchone()
            if not row:
                return False
            existing_ids = []
            try:
                existing_ids = json.loads(row["source_ids"]) if row["source_ids"] else []
            except Exception:
                existing_ids = []
            if new_source_id:
                existing_ids.append(new_source_id)
            merged_ids = list(dict.fromkeys(existing_ids))
            conn.execute(
                "UPDATE memory_fragments SET raw_count=raw_count+1, "
                "source_ids=?, last_recalled=? WHERE id=?",
                (json.dumps(merged_ids), time.time(), obs_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def mark_contradiction(self, obs_id: int, fragment_id: int) -> bool:
        """标记观察与碎片矛盾（hindsight contradict 动作）。

        在 memory_links 建双向 contradicts 边：观察 ↔ 碎片都保留，
        矛盾关系显性化，召回时双方都可见（不做删除裁决）。
        """
        conn = self._conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO memory_links
                   (from_id, to_id, link_type, weight, created_at)
                   VALUES (?,?,?,?,?)""",
                (obs_id, fragment_id, "contradicts", 0.8, time.time())
            )
            conn.execute(
                """INSERT OR IGNORE INTO memory_links
                   (from_id, to_id, link_type, weight, created_at)
                   VALUES (?,?,?,?,?)""",
                (fragment_id, obs_id, "contradicts", 0.8, time.time())
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def link_stories(self, from_story_id: int, to_story_id: int,
                     link_type: str = "next_in_topic", weight: float = 0.9) -> bool:
        """在两条 story 之间建边（P6 故事网络用）。

        link_type:
        - next_in_topic: 同主题时间先后（前一条 → 后一条）
        - similar_to: 跨主题语义相似
        - cause: 跨主题因果（前一条导致后一条）
        """
        if from_story_id == to_story_id:
            return False
        conn = self._conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO memory_links
                   (from_id, to_id, link_type, weight, created_at)
                   VALUES (?,?,?,?,?)""",
                (from_story_id, to_story_id, link_type, weight, time.time())
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_story_chain(self, topic_id: str = None, tags: str = None,
                        limit: int = 20) -> list:
        """取一个主题下的故事链（按 event_time 或 created_at 排序）。

        用于 recall_by_topic 时返回同主题的故事序列，
        AI 一次能拿到完整的项目经历线。
        """
        conn = self._conn()
        try:
            if topic_id:
                rows = conn.execute(
                    "SELECT * FROM memory_fragments WHERE layer='story' AND dirty=1 "
                    "AND topic_id=? ORDER BY created_at ASC LIMIT ?",
                    (topic_id, limit)
                ).fetchall()
            elif tags:
                rows = conn.execute(
                    "SELECT * FROM memory_fragments WHERE layer='story' AND dirty=1 "
                    "AND tags=? ORDER BY created_at ASC LIMIT ?",
                    (tags, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_fragments WHERE layer='story' AND dirty=1 "
                    "ORDER BY created_at ASC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_unconsolidated_cores(self, min_group_size: int = 3,
                                  limit: int = 100) -> list:
        """获取未被 story 整理过的 core 碎片（P6 叙事沉淀用）。

        返回 [{id, text, embedding, created_at, tags}]，按 created_at ASC。
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT id, text, embedding, created_at, tags
                   FROM memory_fragments
                   WHERE layer='core' AND dirty=1
                   AND crystal_parent_id IS NULL
                   AND embedding IS NOT NULL AND embedding != '[]'
                   ORDER BY created_at ASC LIMIT ?""",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_by_hash(self, content_hash: str) -> list:
        """按 summary_hash 查自传晶体（hash 寻址，P3）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_fragments WHERE summary_hash=? ORDER BY created_at DESC",
                (content_hash,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def forget(self, fragment_id):
        return self.delete(fragment_id)

    def edit(self, fragment_id, text=None, tags=None):
        conn = self._conn()
        try:
            updates = []
            vals = []
            if text is not None:
                updates.append("text=?")
                vals.append(text)
                new_emb = embed(text)
                if new_emb:
                    updates.append("embedding=?")
                    vals.append(json.dumps(new_emb))
            if tags is not None:
                updates.append("tags=?")
                vals.append(tags)
            if not updates:
                return False
            vals.append(fragment_id)
            conn.execute(
                f"UPDATE memory_fragments SET {', '.join(updates)} WHERE id=?",
                vals
            )
            conn.commit()
            return True
        finally:
            conn.close()

    # ── Stats & listing ─────────────────────────────

    def stats(self):
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) as c FROM memory_fragments WHERE dirty=1").fetchone()["c"]
            core = conn.execute("SELECT COUNT(*) as c FROM memory_fragments WHERE dirty=1 AND layer='core'").fetchone()["c"]
            archival = conn.execute("SELECT COUNT(*) as c FROM memory_fragments WHERE dirty=1 AND layer='archival'").fetchone()["c"]
            archive_total = conn.execute("SELECT COUNT(*) as c FROM memory_archive").fetchone()["c"]
            avg_w = conn.execute("SELECT AVG(weight) as a FROM memory_fragments WHERE dirty=1 AND layer='core'").fetchone()["a"] or 0
            max_w = conn.execute("SELECT MAX(weight) as a FROM memory_fragments WHERE dirty=1 AND layer='core'").fetchone()["a"] or 0
            recent = conn.execute(
                "SELECT COUNT(*) as c FROM memory_fragments WHERE dirty=1 AND created_at > ?",
                (time.time() - 86400,)
            ).fetchone()["c"]
            return {
                "total_fragments": total,
                "core": core,
                "archival": archival,
                "archive_table": archive_total,
                "avg_weight_core": round(avg_w, 2),
                "max_weight_core": round(max_w, 2),
                "recent_24h": recent,
            }
        finally:
            conn.close()

    def list_all(self, limit=50, offset=0):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_fragments WHERE dirty=1 ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def search_by_text(self, keyword, limit=10, include_archived=False):
        conn = self._conn()
        try:
            dirty_clause = "" if include_archived else "dirty=1 AND "
            rows = conn.execute(
                f"SELECT * FROM memory_fragments "
                f"WHERE {dirty_clause}text LIKE ? ORDER BY weight DESC, created_at DESC LIMIT ?",
                (f"%{keyword}%", limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_chain(self, fragment_id, include_archived=False):
        conn = self._conn()
        try:
            dirty_clause = "AND dirty=1" if not include_archived else ""
            root_id = fragment_id
            while True:
                row = conn.execute(
                    f"SELECT parent_id FROM memory_fragments WHERE id=? {dirty_clause}", (root_id,)
                ).fetchone()
                if not row or not row["parent_id"]:
                    break
                root_id = row["parent_id"]
            chain = []
            frontier = [root_id]
            seen = set()
            while frontier:
                fid = frontier.pop(0)
                if fid in seen:
                    continue
                seen.add(fid)
                row = conn.execute(
                    f"SELECT * FROM memory_fragments WHERE id=? {dirty_clause}", (fid,)
                ).fetchone()
                if row:
                    chain.append(dict(row))
                    children = conn.execute(
                        f"SELECT id FROM memory_fragments WHERE parent_id=? {dirty_clause}", (fid,)
                    ).fetchall()
                    for c in children:
                        frontier.append(c["id"])
            chain.sort(key=lambda f: f.get("ts", ""))
            return chain
        finally:
            conn.close()

    def get_all(self, limit=200):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memory_fragments WHERE dirty=1 ORDER BY ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_graph(self, limit=100):
        conn = self._conn()
        try:
            nodes = conn.execute(
                "SELECT id, text, ts, weight, source, importance, layer, epistemic "
                "FROM memory_fragments WHERE dirty=1 ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            edges = conn.execute(
                """SELECT from_id, to_id, link_type, weight FROM memory_links
                   WHERE from_id IN (SELECT id FROM memory_fragments WHERE dirty=1 ORDER BY created_at DESC LIMIT ?)
                   AND to_id IN (SELECT id FROM memory_fragments WHERE dirty=1 ORDER BY created_at DESC LIMIT ?)""",
                (limit, limit)
            ).fetchall()
            return {
                "nodes": [dict(n) for n in nodes],
                "edges": [dict(e) for e in edges],
            }
        finally:
            conn.close()

    def get_task_roots(self, topic_id, limit=5):
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, text, ts, topic_id, source FROM memory_fragments "
                "WHERE topic_id=? AND source='task_root' AND dirty=1 "
                "ORDER BY created_at DESC LIMIT ?",
                (topic_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def delete(self, fragment_id):
        conn = self._conn()
        try:
            conn.execute("UPDATE memory_fragments SET dirty=0 WHERE id=?", (fragment_id,))
            conn.commit()
            return True
        finally:
            conn.close()

    def delete_all(self):
        conn = self._conn()
        try:
            conn.execute("UPDATE memory_fragments SET dirty=0")
            conn.commit()
            return True
        finally:
            conn.close()


# ── Helper ──────────────────────────────────────────

def _add_col_if_missing(conn, table, col, col_def):
    """Add a column if it doesn't already exist."""
    try:
        conn.execute(f"SELECT {col} FROM {table} LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")


# ── Singleton ──────────────────────────────────────
_store = None


def get_store():
    global _store
    if _store is None:
        _store = FragmentStore()
    return _store
