"""
FileCrystalStore — 文件晶体存储（CMN P1）

实现：
- build_file_crystals: 切片→hash→查重→三问压缩→入库
- detect_hash_changes: 检测文件 hash 变化
- recall: 向量召回
- get_by_path / get_by_hash: hash 寻址

设计参考：[CMN实施方案.md] 第四章 P1
"""
import os
import sys
import json
import time
import uuid
import hashlib
import sqlite3

from memory.file_slicer import slice_file, get_slice_strategy
from memory.embedder import embed
from memory.vec_index import VecIndex, EMBEDDING_DIM
from prompts.file_crystal_prompts import format_crystal_prompt, format_pyramid_prompt

# ── DB path helper ──────────────────────────────────────

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_db_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


# ── Schema ─────────────────────────────────────────────

SCHEMA_FILE_CRYSTALS = """
CREATE TABLE IF NOT EXISTS file_crystals (
    id              TEXT PRIMARY KEY,
    source_path     TEXT NOT NULL,              -- 物理路径或 URL
    source_type     TEXT NOT NULL,              -- 'knowledge_base' / 'ai_downloads' / 'url'
    slice_index     INTEGER NOT NULL,           -- 切片序号
    slice_range     TEXT,                       -- "start:end" 字符偏移
    slice_hash      TEXT NOT NULL,              -- 切片内容 SHA256
    content         TEXT NOT NULL,              -- 切片原文（小文件留底）
    summary         TEXT,                       -- 三问压缩后的晶体
    embedding       TEXT NOT NULL DEFAULT '[]', -- 晶体摘要的向量
    layer           INTEGER NOT NULL DEFAULT 0, -- 金字塔层级（0=原始切片，1=第一层摘要...）
    crystal_parent_id TEXT,                     -- 上层晶体 id（金字塔血统）
    raw_source_id   TEXT,                       -- 下层原始素材 id（raw_source 链）
    entity_ids      TEXT,                       -- JSON 数组
    authority_level INTEGER NOT NULL DEFAULT 0, -- 0=普通, 1=权威
    epistemic       TEXT NOT NULL DEFAULT 'world',  -- 文件晶体默认 world
    confidence_decay REAL NOT NULL DEFAULT 1.0, -- 置信度衰减信号 0.0~1.0
    last_hash_verified_at REAL,                 -- 最后一次 hash 验证时间戳
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active' / 'stale'
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fc_path ON file_crystals(source_path);
CREATE INDEX IF NOT EXISTS idx_fc_hash ON file_crystals(slice_hash);
CREATE INDEX IF NOT EXISTS idx_fc_layer ON file_crystals(layer);
CREATE INDEX IF NOT EXISTS idx_fc_parent ON file_crystals(crystal_parent_id);
CREATE INDEX IF NOT EXISTS idx_fc_status ON file_crystals(status);
CREATE INDEX IF NOT EXISTS idx_fc_authority ON file_crystals(authority_level);
CREATE INDEX IF NOT EXISTS idx_fc_source_type ON file_crystals(source_type);
"""


# ── LLM 调用 ───────────────────────────────────────────

def _call_llm_for_summary(prompt: str) -> str:
    """调 LLM 做三问压缩。失败返回空串。

    参考 tools/memory_tools.py._generate_compress_summary 的模式。
    """
    try:
        from server import get_model_config
        import urllib.request

        llm_config = get_model_config()
        if not llm_config or not llm_config.api_key or not llm_config.base_url:
            return ""

        url = f"{llm_config.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_config.api_key}",
        }
        body = json.dumps({
            "model": llm_config.model,
            "messages": [
                {"role": "system", "content": "你是文件晶体压缩助手，严格按格式输出。"},
                {"role": "user", "content": prompt[:24000]},
            ],
            "stream": False,
            "max_tokens": 600,
            "temperature": 0.3,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return result.strip() if result and len(result) > 5 else ""
    except Exception as e:
        print(f"[file_crystal] LLM 调用失败: {e}")
        return ""


def _fallback_summary(content: str, label: str) -> str:
    """LLM 不可用时的机械降级摘要。"""
    preview = content[:300].replace("\n", " ")
    return (
        f"<<<结论>>>\n（未生成，标签：{label}，原文{len(content)}字）\n\n"
        f"<<<为什么>>>\n{preview}…\n\n"
        f"<<<下一步>>>\n无\n\n"
        f"<<<关键实体>>>\n无"
    )


def _fallback_pyramid_summary(pack_rows, layer: int) -> str:
    """金字塔上层摘要的机械降级：拼接下层摘要前 N 字。"""
    parts = []
    for r in pack_rows:
        s = (r["summary"] or "")[:200].replace("\n", " ")
        parts.append(f"- [{r['id']}] {s}")
    combined = "\n".join(parts)
    return (
        f"<<<结论>>>\n（未生成，{len(pack_rows)} 个 Layer {layer} 晶体合并）\n\n"
        f"<<<为什么>>>\n{combined[:600]}…\n\n"
        f"<<<下一步>>>\n无\n\n"
        f"<<<覆盖范围>>>\n{','.join(r['id'] for r in pack_rows)}"
    )


def _compute_hash(content: str) -> str:
    """SHA256 切片内容，返回前 16 位。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ── FileCrystalStore ───────────────────────────────────

class FileCrystalStore:
    """文件晶体存储。"""

    # 权威库前缀（AI 只读），下载库前缀（AI 读写）
    KNOWLEDGE_BASE_DIR = os.path.join(ROOT_DIR, "knowledge_base")
    AI_DOWNLOADS_DIR = os.path.join(ROOT_DIR, "ai_downloads")

    def __init__(self, db_path=None):
        self.db_path = db_path or _get_db_path()
        self._ensure_schema()
        self._ensure_physical_dirs()
        # 复用 vec_index 加速召回
        self.vec_index = VecIndex(self.db_path)
        self._vec_table = "idx_file_crystals"
        self.vec_index.ensure_table(self._vec_table, EMBEDDING_DIM)

    def _ensure_schema(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA_FILE_CRYSTALS)
            # P5 迁移：补 confidence_decay 字段（P0 建表时漏了）
            try:
                conn.execute("SELECT confidence_decay FROM file_crystals LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE file_crystals ADD COLUMN confidence_decay REAL NOT NULL DEFAULT 1.0")
            conn.commit()
        finally:
            conn.close()

    def _ensure_physical_dirs(self):
        """创建权威库/下载库物理文件夹。"""
        for d in ("knowledge_base", "ai_downloads"):
            path = os.path.join(ROOT_DIR, d)
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
                gitkeep = os.path.join(path, ".gitkeep")
                if not os.path.exists(gitkeep):
                    try:
                        with open(gitkeep, "w", encoding="utf-8") as f:
                            f.write("")
                    except Exception:
                        pass

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _resolve_source_type(self, path: str, source_type: str = None) -> str:
        """推断来源类型：knowledge_base / ai_downloads / url。"""
        if source_type:
            return source_type
        norm = os.path.normpath(path)
        if norm.startswith("http://") or norm.startswith("https://"):
            return "url"
        if os.path.isabs(norm):
            try:
                if os.path.commonpath([norm, self.KNOWLEDGE_BASE_DIR]) == self.KNOWLEDGE_BASE_DIR:
                    return "knowledge_base"
                if os.path.commonpath([norm, self.AI_DOWNLOADS_DIR]) == self.AI_DOWNLOADS_DIR:
                    return "ai_downloads"
            except ValueError:
                pass
        # 相对路径默认按知识库处理
        if path.startswith("knowledge_base/") or path.startswith("knowledge_base\\"):
            return "knowledge_base"
        if path.startswith("ai_downloads/") or path.startswith("ai_downloads\\"):
            return "ai_downloads"
        return "knowledge_base"

    def _is_authority(self, source_type: str) -> int:
        """权威库文件自动 authority=1。"""
        return 1 if source_type == "knowledge_base" else 0

    # ── 核心方法：建晶体 ────────────────────────────────

    def build_file_crystals(self, path: str, source_type: str = None,
                            force_rebuild: bool = False) -> dict:
        """对文件建晶体：切片→hash→查重→三问压缩→入库。

        Args:
            path: 文件路径（绝对或相对项目根）
            source_type: 强制指定来源类型，None 自动推断
            force_rebuild: True 时忽略已有 hash，强制重建

        Returns:
            {"total_slices": N, "new_crystals": M, "skipped": K, "path": path}
        """
        if not os.path.isfile(path):
            return {"error": f"文件不存在: {path}", "path": path}

        source_type = self._resolve_source_type(path, source_type)
        authority = self._is_authority(source_type)

        # 切片
        slices = slice_file(path)
        if not slices:
            return {"error": "切片结果为空", "path": path}

        now = time.time()
        new_count = 0
        skipped = 0

        conn = self._conn()
        try:
            for sl in slices:
                # 算 hash
                sl.hash = _compute_hash(sl.content)

                # 查重（同 source_path + slice_hash 视为已建）
                if not force_rebuild:
                    existing = conn.execute(
                        "SELECT id FROM file_crystals WHERE source_path=? AND slice_hash=? AND status='active'",
                        (path, sl.hash)
                    ).fetchone()
                    if existing:
                        skipped += 1
                        continue

                # 三问压缩
                prompt = format_crystal_prompt(path, sl.label, sl.content)
                summary = _call_llm_for_summary(prompt)
                if not summary:
                    summary = _fallback_summary(sl.content, sl.label)

                # embedding（基于 summary；为空则用 content 前 500 字）
                emb_text = summary if summary else sl.content[:500]
                emb = embed(emb_text)
                emb_json = json.dumps(emb)

                crystal_id = f"fc_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """INSERT INTO file_crystals
                       (id, source_path, source_type, slice_index, slice_range, slice_hash,
                        content, summary, embedding, layer, crystal_parent_id, raw_source_id,
                        entity_ids, authority_level, epistemic, confidence_decay,
                        last_hash_verified_at, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (crystal_id, path, source_type, sl.index, f"{sl.start}:{sl.end}",
                     sl.hash, sl.content, summary, emb_json, 0, None, None,
                     "[]", authority, "world", 1.0, now, "active", now, now)
                )

                # 同步 vec_index（vec0 rowid 必须 INTEGER，用 file_crystals 隐式 rowid）
                try:
                    rw = conn.execute("SELECT rowid FROM file_crystals WHERE id=?", (crystal_id,)).fetchone()
                    if rw:
                        self.vec_index.insert(self._vec_table, rw["rowid"], emb)
                except Exception as e:
                    print(f"[file_crystal] vec_index insert 警告: {e}")

                new_count += 1

            conn.commit()
        finally:
            conn.close()

        return {
            "total_slices": len(slices),
            "new_crystals": new_count,
            "skipped": skipped,
            "path": path,
            "source_type": source_type,
        }

    # ── 纯切片建库（无 LLM，透明基础设施） ─────────────

    def build_slices_only(self, path: str, source_type: str = None,
                          force_rebuild: bool = False) -> dict:
        """对文件建纯切片：切片→hash→查重→入库（不调 LLM）。

        符合"晶体必须经过 AI 脑子"哲学：
        - 切片+hash 是基础设施（定位+变化检测），不是晶体
        - 真正的晶体（理解/结论）由 AI 思考产生，存在 memory_fragments 表
        - raw_source_id 桥将自传晶体指向切片

        Returns:
            {"total_slices": N, "new_slices": M, "skipped": K, "path": path}
        """
        if not os.path.isfile(path):
            return {"error": f"文件不存在: {path}", "path": path}

        source_type = self._resolve_source_type(path, source_type)
        authority = self._is_authority(source_type)

        slices = slice_file(path)
        if not slices:
            return {"error": "切片结果为空", "path": path}

        now = time.time()

        # ── 阶段 1：查重 + 算 embedding（不持写锁，避免 embed 耗时阻塞其他写入）──
        to_insert = []  # [{crystal_id, hash, emb, emb_json, sl}]
        skipped = 0
        conn = self._conn()
        try:
            for sl in slices:
                sl.hash = _compute_hash(sl.content)

                if not force_rebuild:
                    existing = conn.execute(
                        "SELECT id FROM file_crystals WHERE source_path=? AND slice_hash=? AND status='active'",
                        (path, sl.hash)
                    ).fetchone()
                    if existing:
                        skipped += 1
                        continue

                # embedding 基于切片原文前 500 字（不调 LLM 压缩）
                emb_text = sl.content[:500]
                emb = embed(emb_text)
                emb_json = json.dumps(emb)

                crystal_id = f"fc_{uuid.uuid4().hex[:12]}"
                to_insert.append({
                    "crystal_id": crystal_id,
                    "hash": sl.hash,
                    "emb": emb,
                    "emb_json": emb_json,
                    "sl": sl,
                })
        finally:
            conn.close()  # 查完就关，不持锁调 embed

        if not to_insert:
            return {
                "total_slices": len(slices),
                "new_slices": 0,
                "skipped": skipped,
                "path": path,
                "source_type": source_type,
            }

        # ── 阶段 2：批量 INSERT（短事务，快速提交）──
        new_count = 0
        conn = self._conn()
        try:
            for item in to_insert:
                sl = item["sl"]
                conn.execute(
                    """INSERT INTO file_crystals
                       (id, source_path, source_type, slice_index, slice_range, slice_hash,
                        content, summary, embedding, layer, crystal_parent_id, raw_source_id,
                        entity_ids, authority_level, epistemic, confidence_decay,
                        last_hash_verified_at, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["crystal_id"], path, source_type, sl.index, f"{sl.start}:{sl.end}",
                     item["hash"], sl.content, None, item["emb_json"], 0, None, None,
                     "[]", authority, "world", 1.0, now, "active", now, now)
                )
                new_count += 1
            conn.commit()  # 先提交主事务，释放写锁
        finally:
            conn.close()

        # ── 阶段 3：更新 vec_index（commit 之后，避免跨连接死锁）──
        conn2 = self._conn()
        try:
            for item in to_insert:
                rw = conn2.execute(
                    "SELECT rowid FROM file_crystals WHERE id=?", (item["crystal_id"],)
                ).fetchone()
                if not rw:
                    continue
                try:
                    self.vec_index.insert(self._vec_table, rw["rowid"], item["emb"])
                except Exception as e:
                    print(f"[file_crystal] vec_index insert 警告: {e}")
        finally:
            conn2.close()

        return {
            "total_slices": len(slices),
            "new_slices": new_count,
            "skipped": skipped,
            "path": path,
            "source_type": source_type,
        }

    # ── 已阅状态查询（read_file 透明接入用） ───────────

    def get_file_status(self, path: str) -> dict:
        """查询文件的已阅状态 + hash 变化 + 关联思考。

        read_file 调用此方法决定返回什么：
        - 未阅：返回 {"seen_before": false}
        - 已阅未变：返回 seen_before + thoughts（AI 之前在这文件上的思考）
        - 已阅有变：返回 seen_before + changed_slices + stale thoughts

        Returns:
            {
                "seen_before": bool,
                "last_verified": float,
                "hash_changed": bool,
                "changed_slices": [{"slice_index": i, "old_hash": x, "new_hash": y, "line_range": "a:b"}],
                "stale_slice_ids": [id, ...],
                "thoughts": [{"slice_id": "fc_xxx", "slice_index": i, "line_range": "a:b",
                              "thought_id": 42, "thought_text": "...", "stale": bool}],
                "total_slices": N
            }
        """
        if not os.path.isfile(path):
            return {"seen_before": False, "error": f"文件不存在: {path}"}

        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM file_crystals WHERE source_path=? AND layer=0 "
                "AND status='active' ORDER BY slice_index",
                (path,)
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"seen_before": False, "total_slices": 0}

        # 文件已阅，检测 hash 变化
        slices = slice_file(path)
        new_hashes = {sl.index: _compute_hash(sl.content) for sl in slices}
        old_by_index = {r["slice_index"]: r for r in rows}

        changed_slices = []
        stale_ids = []
        for idx, new_hash in new_hashes.items():
            if idx in old_by_index:
                old_hash = old_by_index[idx]["slice_hash"]
                if old_hash != new_hash:
                    sr = old_by_index[idx]["slice_range"] or ""
                    changed_slices.append({
                        "slice_index": idx,
                        "old_hash": old_hash,
                        "new_hash": new_hash,
                        "line_range": sr,
                    })
                    stale_ids.append(old_by_index[idx]["id"])

        # 反查每个切片关联的自传晶体（AI 的真实思考）
        thoughts = self.get_thoughts_for_path(path, conn=None)

        # 标记 stale 的思考
        stale_set = set(stale_ids)
        for t in thoughts:
            if t.get("slice_id") in stale_set:
                t["stale"] = True

        last_verified = max(
            (r["last_hash_verified_at"] or r["created_at"]) for r in rows
        )

        return {
            "seen_before": True,
            "last_verified": last_verified,
            "hash_changed": len(changed_slices) > 0,
            "changed_slices": changed_slices,
            "stale_slice_ids": stale_ids,
            "thoughts": thoughts,
            "total_slices": len(rows),
        }

    # ── 反查：文件切片 → 自传晶体（AI 的真实思考） ──────

    def get_thoughts_for_path(self, path: str, conn=None) -> list:
        """反查某文件所有切片关联的自传晶体。

        桥：memory_fragments.raw_source_id = file_crystals.id

        Returns:
            [{"slice_id": "fc_xxx", "slice_index": i, "line_range": "a:b",
              "thought_id": 42, "thought_text": "...", "epistemic": "experience",
              "importance": 7.0, "ts": "20260801..."}]
        """
        own_conn = conn is None
        if own_conn:
            conn = self._conn()
        try:
            # 先拿该文件所有切片
            slices = conn.execute(
                "SELECT id, slice_index, slice_range FROM file_crystals "
                "WHERE source_path=? AND layer=0 AND status='active' ORDER BY slice_index",
                (path,)
            ).fetchall()
            if not slices:
                return []

            slice_ids = [s["id"] for s in slices]
            placeholders = ",".join("?" * len(slice_ids))

            # 反查 memory_fragments 表
            thoughts = []
            frags = conn.execute(
                f"""SELECT id, text, raw_source_id, epistemic, importance, ts, tags
                    FROM memory_fragments
                    WHERE raw_source_id IN ({placeholders})
                    AND node_type='self' AND dirty=1
                    ORDER BY importance DESC, id DESC""",
                slice_ids
            ).fetchall()

            # 建立 slice_id → slice_info 映射
            slice_map = {s["id"]: s for s in slices}
            for f in frags:
                sid = f["raw_source_id"]
                sl = slice_map.get(sid)
                if sl:
                    thoughts.append({
                        "slice_id": sid,
                        "slice_index": sl["slice_index"],
                        "line_range": sl["slice_range"] or "",
                        "thought_id": f["id"],
                        "thought_text": f["text"],
                        "epistemic": f["epistemic"],
                        "importance": f["importance"],
                        "ts": f["ts"],
                        "tags": f["tags"],
                    })
            return thoughts
        finally:
            if own_conn:
                conn.close()

    def get_thoughts_for_slice(self, crystal_id: str) -> list:
        """反查某具体切片关联的自传晶体。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT id, text, epistemic, importance, ts, tags
                   FROM memory_fragments
                   WHERE raw_source_id=? AND node_type='self' AND dirty=1
                   ORDER BY importance DESC, id DESC""",
                (crystal_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── hash 变化检测 ───────────────────────────────────

    def detect_hash_changes(self, path: str) -> dict:
        """检测文件 hash 变化，返回变化的切片列表。

        Returns:
            {"total": N, "unchanged": M, "changed": [{"slice_index": i, "old_hash": x, "new_hash": y}],
             "new_slices": K, "missing_slices": L, "stale_crystal_ids": [id, ...]}
        """
        if not os.path.isfile(path):
            return {"error": f"文件不存在: {path}"}

        slices = slice_file(path)
        new_hashes = {sl.index: _compute_hash(sl.content) for sl in slices}
        new_index_set = set(new_hashes.keys())

        conn = self._conn()
        try:
            # 拉取该文件所有 active 晶体
            rows = conn.execute(
                "SELECT id, slice_index, slice_hash FROM file_crystals "
                "WHERE source_path=? AND status='active' AND layer=0",
                (path,)
            ).fetchall()
        finally:
            conn.close()

        old_by_index = {r["slice_index"]: r for r in rows}
        old_index_set = set(old_by_index.keys())

        changed = []
        stale_ids = []
        for idx, new_hash in new_hashes.items():
            if idx in old_by_index:
                old_hash = old_by_index[idx]["slice_hash"]
                if old_hash != new_hash:
                    changed.append({
                        "slice_index": idx,
                        "old_hash": old_hash,
                        "new_hash": new_hash,
                    })
                    stale_ids.append(old_by_index[idx]["id"])
        new_slices = len(new_index_set - old_index_set)
        missing = len(old_index_set - new_index_set)
        unchanged = len(new_index_set & old_index_set) - len(changed)

        # 标记变化的晶体为 stale
        if stale_ids:
            conn = self._conn()
            try:
                placeholders = ",".join("?" * len(stale_ids))
                conn.execute(
                    f"UPDATE file_crystals SET status='stale', updated_at=? WHERE id IN ({placeholders})",
                    [time.time()] + stale_ids
                )
                conn.commit()
            finally:
                conn.close()

        return {
            "total": len(slices),
            "unchanged": unchanged,
            "changed": len(changed),
            "new_slices": new_slices,
            "missing_slices": missing,
            "stale_crystal_ids": stale_ids,
            "path": path,
        }

    def mark_stale(self, crystal_id: str):
        """标记单个晶体为 stale。"""
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE file_crystals SET status='stale', updated_at=? WHERE id=?",
                (time.time(), crystal_id)
            )
            conn.commit()
        finally:
            conn.close()

    # ── 召回 ───────────────────────────────────────────

    def recall(self, query: str, top_k: int = 5, include_stale: bool = False) -> list:
        """向量召回文件晶体。"""
        q_emb = embed(query)
        if not q_emb:
            return []

        conn = self._conn()
        try:
            # vec_index KNN 预筛
            candidates = []
            if self.vec_index.is_available():
                try:
                    knn = self.vec_index.query(self._vec_table, q_emb, top_k * 4)
                    if knn:
                        # query 返回 [{rowid, distance}]，rowid 是 file_crystals 隐式 rowid
                        rowids = [r["rowid"] for r in knn]
                        placeholders = ",".join("?" * len(rowids))
                        rows = conn.execute(
                            f"SELECT rowid, * FROM file_crystals WHERE rowid IN ({placeholders})",
                            rowids
                        ).fetchall()
                        # 保持 KNN 顺序
                        rowid_to_row = {r["rowid"]: r for r in rows}
                        candidates = [rowid_to_row[i] for i in rowids if i in rowid_to_row]
                except Exception as e:
                    print(f"[file_crystal] vec_index query 警告: {e}")

            # 降级：全表扫描
            if not candidates:
                rows = conn.execute(
                    "SELECT * FROM file_crystals WHERE layer=0"
                    + (" AND status='active'" if not include_stale else "")
                ).fetchall()
                # 简单 cosine 相似度
                scored = []
                for r in rows:
                    emb = json.loads(r["embedding"]) if r["embedding"] else []
                    sim = _cosine(q_emb, emb)
                    if sim > 0.1:
                        scored.append((sim, r))
                scored.sort(key=lambda x: -x[0])
                candidates = [r for _, r in scored[:top_k * 4]]

            # 过滤 stale（除非要求包含）
            if not include_stale:
                candidates = [r for r in candidates if r["status"] == "active"]

            # 截断
            return [_row_to_dict(r) for r in candidates[:top_k]]
        finally:
            conn.close()

    # ── hash 寻址 ──────────────────────────────────────

    def get_by_path(self, path: str, layer: int = None, include_stale: bool = False) -> list:
        """按路径查所有晶体。"""
        conn = self._conn()
        try:
            sql = "SELECT * FROM file_crystals WHERE source_path=?"
            params = [path]
            if layer is not None:
                sql += " AND layer=?"
                params.append(layer)
            if not include_stale:
                sql += " AND status='active'"
            sql += " ORDER BY slice_index"
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def get_by_hash(self, hash_value: str) -> list:
        """按 hash 查晶体（跨文件）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM file_crystals WHERE slice_hash=? ORDER BY created_at DESC",
                (hash_value,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def get_by_id(self, crystal_id: str) -> dict:
        """按 id 查单个晶体。"""
        conn = self._conn()
        try:
            r = conn.execute(
                "SELECT * FROM file_crystals WHERE id=?", (crystal_id,)
            ).fetchone()
            return _row_to_dict(r) if r else None
        finally:
            conn.close()

    # ── 读取切片原文（下钻用） ─────────────────────────

    def read_file_slice(self, path: str, slice_index: int) -> dict:
        """读取某切片的原文（从 file_crystals 表的 content 字段拿）。"""
        conn = self._conn()
        try:
            r = conn.execute(
                "SELECT * FROM file_crystals WHERE source_path=? AND slice_index=? AND layer=0",
                (path, slice_index)
            ).fetchone()
            return _row_to_dict(r) if r else None
        finally:
            conn.close()

    # ── 统计 ───────────────────────────────────────────

    def stats(self) -> dict:
        """返回文件晶体的统计信息。"""
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) as c FROM file_crystals").fetchone()["c"]
            active = conn.execute("SELECT COUNT(*) as c FROM file_crystals WHERE status='active'").fetchone()["c"]
            stale = conn.execute("SELECT COUNT(*) as c FROM file_crystals WHERE status='stale'").fetchone()["c"]
            authority = conn.execute("SELECT COUNT(*) as c FROM file_crystals WHERE authority_level=1").fetchone()["c"]
            files = conn.execute(
                "SELECT COUNT(DISTINCT source_path) as c FROM file_crystals"
            ).fetchone()["c"]
            by_type = {}
            for r in conn.execute(
                "SELECT source_type, COUNT(*) as c FROM file_crystals GROUP BY source_type"
            ).fetchall():
                by_type[r["source_type"]] = r["c"]
            by_layer = {}
            for r in conn.execute(
                "SELECT layer, COUNT(*) as c FROM file_crystals GROUP BY layer"
            ).fetchall():
                by_layer[str(r["layer"])] = r["c"]
            return {
                "total_crystals": total,
                "active": active,
                "stale": stale,
                "authority": authority,
                "files_indexed": files,
                "by_source_type": by_type,
                "by_layer": by_layer,
            }
        finally:
            conn.close()

    # ── 金字塔多层（P2） ────────────────────────────────

    MAX_PYRAMID_LAYERS = 5         # 最多 5 层，超出强制扩大打包数
    DEFAULT_PACK_SIZE = 4          # 每层打包多少下层晶体
    TOP_LAYER_TOKEN_BUDGET = 5000  # 顶层总长预算（字符数近似）

    def build_pyramid(self, path: str, pack_size: int = None) -> dict:
        """对某文件的所有 layer=0 晶体建金字塔。

        策略：每 pack_size 个 layer=N 晶体打包，调 LLM 摘要成 1 个 layer=N+1 晶体。
        持续直到顶层晶体总长 < TOP_LAYER_TOKEN_BUDGET 或达到 MAX_PYRAMID_LAYERS。

        Returns:
            {"layers_built": N, "top_layer": L, "top_total_chars": C, "path": path}
        """
        if pack_size is None:
            pack_size = self.DEFAULT_PACK_SIZE

        conn = self._conn()
        try:
            source_type_row = conn.execute(
                "SELECT source_type, authority_level FROM file_crystals WHERE source_path=? LIMIT 1",
                (path,)
            ).fetchone()
            if not source_type_row:
                return {"error": f"文件未建晶体: {path}", "path": path}
            source_type = source_type_row["source_type"]
            authority = source_type_row["authority_level"]

            layers_built = 0
            current_layer = 0

            while current_layer < self.MAX_PYRAMID_LAYERS:
                # 取当前层所有 active 晶体
                rows = conn.execute(
                    "SELECT * FROM file_crystals WHERE source_path=? AND layer=? AND status='active' "
                    "ORDER BY slice_index",
                    (path, current_layer)
                ).fetchall()
                if not rows:
                    break

                # 如果当前层总长已经够小，停止
                total_chars = sum(len(r["summary"] or "") for r in rows)
                if total_chars <= self.TOP_LAYER_TOKEN_BUDGET:
                    break

                # 如果当前层只有 1 个晶体，无法再合并
                if len(rows) <= 1:
                    break

                # 打包合并
                next_layer = current_layer + 1
                # 动态扩大 pack_size 如果层数过多
                effective_pack = pack_size
                if current_layer >= self.MAX_PYRAMID_LAYERS - 1:
                    effective_pack = len(rows)  # 最后一层全打包

                new_count = 0
                for i in range(0, len(rows), effective_pack):
                    pack = rows[i:i + effective_pack]
                    if len(pack) < 2:
                        # 不足 2 个不合并，保留原样
                        continue

                    # 调 LLM 生成上层摘要
                    crystals_json = json.dumps([{
                        "id": p["id"],
                        "summary": (p["summary"] or "")[:500],
                        "slice_index": p["slice_index"],
                    } for p in pack], ensure_ascii=False)
                    prompt = format_pyramid_prompt(path, current_layer, crystals_json)
                    summary = _call_llm_for_summary(prompt)
                    if not summary:
                        summary = _fallback_pyramid_summary(pack, current_layer)

                    emb = embed(summary)
                    emb_json = json.dumps(emb)
                    crystal_id = f"fc_py_{uuid.uuid4().hex[:10]}"
                    now = time.time()

                    # 上层晶体的 crystal_parent_id = NULL（它是最上层）
                    # raw_source_id 指向第一个下层晶体（作为下钻入口）
                    conn.execute(
                        """INSERT INTO file_crystals
                           (id, source_path, source_type, slice_index, slice_range, slice_hash,
                            content, summary, embedding, layer, crystal_parent_id, raw_source_id,
                            entity_ids, authority_level, epistemic, confidence_decay,
                            last_hash_verified_at, status, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (crystal_id, path, source_type, i // effective_pack,
                         f"pack:{i}:{i+len(pack)}", _compute_hash(summary),
                         crystals_json[:2000], summary, emb_json, next_layer,
                         None, pack[0]["id"],  # raw_source 指向第一个下层
                         "[]", authority, "world", 1.0, now, "active", now, now)
                    )

                    # 更新下层晶体的 crystal_parent_id 指向上层
                    for p in pack:
                        conn.execute(
                            "UPDATE file_crystals SET crystal_parent_id=? WHERE id=?",
                            (crystal_id, p["id"])
                        )

                    # 同步 vec_index（vec0 rowid 必须 INTEGER，用隐式 rowid）
                    try:
                        rw = conn.execute("SELECT rowid FROM file_crystals WHERE id=?", (crystal_id,)).fetchone()
                        if rw:
                            self.vec_index.insert(self._vec_table, rw["rowid"], emb)
                    except Exception as e:
                        print(f"[file_crystal] vec_index insert 警告: {e}")

                    new_count += 1

                conn.commit()
                layers_built += new_count
                current_layer = next_layer

            # 统计顶层
            top_rows = conn.execute(
                "SELECT summary FROM file_crystals WHERE source_path=? AND layer=? AND status='active'",
                (path, current_layer)
            ).fetchall()
            top_total = sum(len(r["summary"] or "") for r in top_rows)

            return {
                "layers_built": layers_built,
                "top_layer": current_layer,
                "top_total_chars": top_total,
                "path": path,
            }
        finally:
            conn.close()

    def drill_down(self, crystal_id: str) -> list:
        """沿 raw_source_id 下钻到下层晶体。

        返回该晶体派生的所有下层晶体（layer-1）。
        """
        conn = self._conn()
        try:
            # 找所有 crystal_parent_id 指向 crystal_id 的下层晶体
            rows = conn.execute(
                "SELECT * FROM file_crystals WHERE crystal_parent_id=? AND status='active' "
                "ORDER BY slice_index",
                (crystal_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def drill_up(self, crystal_id: str) -> dict:
        """沿 crystal_parent_id 上钻到上层晶体。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM file_crystals WHERE id=?", (crystal_id,)
            ).fetchone()
            if not row or not row["crystal_parent_id"]:
                return None
            parent = conn.execute(
                "SELECT * FROM file_crystals WHERE id=?",
                (row["crystal_parent_id"],)
            ).fetchone()
            return _row_to_dict(parent) if parent else None
        finally:
            conn.close()

    def get_pyramid(self, path: str) -> dict:
        """返回某文件的完整金字塔结构（树形）。

        Returns:
            {"path": path, "top_layer": L, "tree": [{crystal, children: [...]}]}
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM file_crystals WHERE source_path=? AND status='active' "
                "ORDER BY layer, slice_index",
                (path,)
            ).fetchall()
            if not rows:
                return {"path": path, "top_layer": 0, "tree": []}

            top_layer = max(r["layer"] for r in rows)
            by_id = {r["id"]: dict(r) for r in rows}

            # 构建树：从顶层开始递归
            top_rows = [r for r in rows if r["layer"] == top_layer]

            def build_node(r):
                d = _row_to_dict(r)
                children = [build_node(by_id[cid]) for cid in by_id
                            if by_id[cid].get("crystal_parent_id") == r["id"]]
                d["children"] = children
                return d

            tree = [build_node(r) for r in top_rows]
            return {
                "path": path,
                "top_layer": top_layer,
                "total_crystals": len(rows),
                "tree": tree,
            }
        finally:
            conn.close()

    def read_top_layer(self, path: str) -> dict:
        """读取顶层晶体塞入上下文。

        Returns:
            {"path": path, "top_layer": L, "content": "合并的顶层摘要", "total_chars": N}
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM file_crystals WHERE source_path=? AND status='active' "
                "ORDER BY layer DESC, slice_index LIMIT 1",
                (path,)
            ).fetchall()
            if not rows:
                return {"error": f"文件未建晶体: {path}"}

            top_layer = rows[0]["layer"]
            top_rows = conn.execute(
                "SELECT * FROM file_crystals WHERE source_path=? AND layer=? AND status='active' "
                "ORDER BY slice_index",
                (path, top_layer)
            ).fetchall()

            parts = []
            for r in top_rows:
                parts.append(f"【晶体 {r['id']} | Layer {top_layer} | 切片 {r['slice_index']}】\n{r['summary']}")
            content = "\n\n---\n\n".join(parts)

            return {
                "path": path,
                "top_layer": top_layer,
                "content": content,
                "total_chars": len(content),
                "crystals_count": len(top_rows),
            }
        finally:
            conn.close()

    def propagate_stale_upward(self, crystal_id: str):
        """下层晶体 stale 后，向上传播标记上层晶体 stale（按需验证的辅助）。

        P5 阶段会接入 confidence_decay，这里先提供基础传播。
        """
        conn = self._conn()
        try:
            now = time.time()
            current_id = crystal_id
            while current_id:
                row = conn.execute(
                    "SELECT crystal_parent_id FROM file_crystals WHERE id=?",
                    (current_id,)
                ).fetchone()
                if not row or not row["crystal_parent_id"]:
                    break
                parent_id = row["crystal_parent_id"]
                conn.execute(
                    "UPDATE file_crystals SET status='stale', updated_at=? WHERE id=?",
                    (now, parent_id)
                )
                current_id = parent_id
            conn.commit()
        finally:
            conn.close()


# ── 辅助函数 ───────────────────────────────────────────

def _cosine(a: list, b: list) -> float:
    """余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _row_to_dict(row) -> dict:
    """sqlite Row → dict（embedding 解析为 list）。"""
    if row is None:
        return None
    d = dict(row)
    try:
        d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else []
    except Exception:
        d["embedding"] = []
    try:
        d["entity_ids"] = json.loads(d["entity_ids"]) if d.get("entity_ids") else []
    except Exception:
        d["entity_ids"] = []
    return d


# ── Singleton ──────────────────────────────────────────

_store = None


def get_store():
    global _store
    if _store is None:
        _store = FileCrystalStore()
    return _store

