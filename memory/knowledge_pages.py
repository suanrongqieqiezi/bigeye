"""知识页层：版本化知识库。

把平铺的 observation（knowledge 层碎片）按主题聚合成「知识页」，
每页有版本链：最新版可读，旧版留档可回溯。

核心概念：
- knowledge_pages：页主表（page_key 主题标识、centroid 质心向量、current_version）
- knowledge_page_versions：版本表（每页 N 个版本，content 成文知识 + source_obs_ids）

生成链路（与 hindsight consolidate 衔接）：
    consolidate 攒 observation → absorb(obs_id) 归组到最相似页
    → 未成文页攒够 MIN_OBS_PER_PAGE(3) 条 → LLM 成文写 v1
    → 已成文页收到新证据 → LLM 重写 → vN+1（旧版留档）

读取：
    get_page(page_key)          → 最新版
    get_page(page_key, v=N)     → 回溯第 N 版

独立于 memory_fragments，不污染碎片向量召回。
"""

import json
import os
import sqlite3
import time

try:
    from .embedder import embed, cosine_sim
except Exception:  # pragma: no cover
    embed = None
    cosine_sim = None


def _get_db_path():
    MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(MEMORY_DIR)
    if getattr(__import__("sys"), "frozen", False):
        return os.path.join(os.path.dirname(__import__("sys").executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


# 归组相似度阈值：与页内任一成员的相似度（single-linkage）
# 0.60 比 consolidate 的 0.72 宽——页是更高层聚合，同主题碎片应尽量归拢
PAGE_MATCH_THRESHOLD = 0.60
# 每页最少观察数才成文
MIN_OBS_PER_PAGE = 3
# 每页最多观察数（超过则成文不再无限累积）
MAX_OBS_PER_PAGE = 12

SCHEMA_PAGES = """
CREATE TABLE IF NOT EXISTS knowledge_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_key        TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL DEFAULT 0,
    centroid        TEXT NOT NULL DEFAULT '[]',
    obs_ids         TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
)
"""

SCHEMA_PAGE_VERSIONS = """
CREATE TABLE IF NOT EXISTS knowledge_page_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id         INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    content         TEXT NOT NULL,
    source_obs_ids  TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL,
    UNIQUE(page_id, version)
)
"""


def _norm(v):
    """向量归一化。"""
    if not v:
        return None
    try:
        n = sum(x * x for x in v) ** 0.5
        if n == 0:
            return None
        return [x / n for x in v]
    except Exception:
        return None


def _vec_mean(vecs):
    """向量平均（归一化后平均再归一化）。"""
    valid = [v for v in vecs if v]
    if not valid:
        return None
    dim = len(valid[0])
    acc = [0.0] * dim
    for v in valid:
        for i in range(dim):
            acc[i] += v[i]
    return _norm([x / len(valid) for x in acc])


class KnowledgePageStore:
    """知识页存储。"""

    def __init__(self, db_path=None, llm_call_fn=None):
        self.db_path = db_path or _get_db_path()
        self.llm_call_fn = llm_call_fn  # (prompt, user) -> str
        self._ensure_schema()

    # ── 建表 ────────────────────────────────

    def _ensure_schema(self):
        conn = self._conn()
        try:
            conn.execute(SCHEMA_PAGES)
            conn.execute(SCHEMA_PAGE_VERSIONS)
            conn.commit()
        finally:
            conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ── 归组 / 吸收 ─────────────────────────

    def absorb(self, obs_id: int, obs_text: str = None, compose: bool = True) -> dict:
        """把一条 observation 归入最相似的知识页（或新建草稿页）。

        compose=False：只归组/更新成员，不触发 LLM 成文（用于存量 backfill，
        最后统一 compose 一次，避免每条都调 LLM）。

        返回 {"page_id", "page_key", "version", "action"}：
        action: created=新建页（未成文）| versioned=升版 | drafted=归入未成文草稿
        """
        if embed is None:
            return {"action": "none", "reason": "no_embedder"}
        emb = embed(obs_text or self._get_obs_text(obs_id))
        if not emb:
            return {"action": "none", "reason": "no_embedding"}

        conn = self._conn()
        try:
            page = self._find_best_page(conn, emb)
            if page:
                return self._absorb_into(conn, page, obs_id, emb, compose=compose)
            # 无匹配页 → 建草稿页（current_version=0，未成文）
            page_key = self._gen_page_key(conn, obs_id)
            conn.execute(
                "INSERT INTO knowledge_pages (page_key, title, centroid, obs_ids, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (page_key, "", json.dumps(emb), json.dumps([obs_id]), time.time(), time.time()),
            )
            conn.commit()
            return {"action": "created", "page_id": conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                    "page_key": page_key, "version": 0}
        finally:
            conn.close()

    def _absorb_into(self, conn, page, obs_id: int, emb: list, compose: bool = True) -> dict:
        """归入已有页：更新质心 + obs_ids，触发成文/升版。"""
        obs_ids = json.loads(page["obs_ids"]) if page["obs_ids"] else []
        if obs_id not in obs_ids:
            obs_ids.append(obs_id)
        # 不截断：成员全量保留（截断会丢证据，且让 backfill 永不幂等）

        # 更新质心：所有成员向量的均值
        member_embs = self._get_member_embeddings(conn, obs_ids)
        new_centroid = _vec_mean(member_embs) or emb
        conn.execute(
            "UPDATE knowledge_pages SET centroid=?, obs_ids=?, updated_at=? WHERE id=?",
            (json.dumps(new_centroid), json.dumps(obs_ids), time.time(), page["id"]),
        )
        conn.commit()

        if page["current_version"] == 0:
            # 草稿页：攒够 MIN 条 → 成文 v1
            if compose and len(obs_ids) >= MIN_OBS_PER_PAGE:
                return self._materialize(conn, page["id"], page["page_key"], obs_ids, first=True)
            return {"action": "drafted", "page_id": page["id"],
                    "page_key": page["page_key"], "version": 0}
        else:
            # 已成文页：新证据 → 重写成文 vN+1
            if not compose:
                return {"action": "drafted", "page_id": page["id"],
                        "page_key": page["page_key"], "version": page["current_version"]}
            return self._materialize(conn, page["id"], page["page_key"], obs_ids, first=False)

    def _materialize(self, conn, page_id: int, page_key: str, obs_ids: list, first: bool) -> dict:
        """LLM 成文写版本。first=True 写 v1，否则 vN+1。"""
        version = 1 if first else None
        if not first:
            row = conn.execute(
                "SELECT MAX(version) m FROM knowledge_page_versions WHERE page_id=?", (page_id,)
            ).fetchone()
            version = (row["m"] or 0) + 1

        obs_texts = self._get_obs_texts(conn, obs_ids)
        content = obs_texts  # 兜底：LLM 不可用时用原文拼接
        title = page_key
        if self.llm_call_fn and len(obs_texts) >= MIN_OBS_PER_PAGE:
            try:
                from prompts.consolidation_prompts import format_page_compose_prompt
                prompt = format_page_compose_prompt(page_key, obs_texts, previous=None if first else
                                                    self._get_latest_content(conn, page_id))
                resp = self.llm_call_fn(prompt, "你正在把自己的长期观察沉淀成体系知识。")
                parsed = self._parse_compose(resp)
                content = parsed.get("content") or content
                title = parsed.get("title") or title
            except Exception as e:
                print(f"[knowledge_pages] compose LLM failed: {e}")

        conn.execute(
            "INSERT INTO knowledge_page_versions (page_id, version, content, source_obs_ids, created_at) "
            "VALUES (?,?,?,?,?)",
            (page_id, version, content, json.dumps(obs_ids), time.time()),
        )
        conn.execute(
            "UPDATE knowledge_pages SET current_version=?, title=?, updated_at=? WHERE id=?",
            (version, title, time.time(), page_id),
        )
        conn.commit()
        return {"action": "versioned", "page_id": page_id, "page_key": page_key, "version": version}

    # ── 读取 ────────────────────────────────

    def get_page(self, page_key: str, version: int = None) -> dict:
        """读知识页。默认最新版；传 version 回溯旧版。"""
        conn = self._conn()
        try:
            page = conn.execute(
                "SELECT * FROM knowledge_pages WHERE page_key=?", (page_key,)
            ).fetchone()
            if not page:
                return None
            if version is None:
                version = page["current_version"]
            ver = conn.execute(
                "SELECT * FROM knowledge_page_versions WHERE page_id=? AND version=?",
                (page["id"], version),
            ).fetchone()
            if not ver:
                return None
            return {
                "page_key": page["page_key"],
                "title": page["title"],
                "version": ver["version"],
                "content": ver["content"],
                "source_obs_ids": json.loads(ver["source_obs_ids"]) if ver["source_obs_ids"] else [],
                "current_version": page["current_version"],
                "updated_at": ver["created_at"],
            }
        finally:
            conn.close()

    def list_pages(self, include_drafts: bool = False) -> list:
        """列出所有页（默认只列已成文的）。"""
        conn = self._conn()
        try:
            if include_drafts:
                rows = conn.execute(
                    "SELECT id, page_key, title, current_version, obs_ids, updated_at "
                    "FROM knowledge_pages ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, page_key, title, current_version, obs_ids, updated_at "
                    "FROM knowledge_pages WHERE current_version>0 ORDER BY updated_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_page_versions(self, page_key: str) -> list:
        """页的版本链（旧→新）。"""
        conn = self._conn()
        try:
            page = conn.execute("SELECT id FROM knowledge_pages WHERE page_key=?", (page_key,)).fetchone()
            if not page:
                return []
            rows = conn.execute(
                "SELECT version, substr(content,1,120) preview, created_at FROM knowledge_page_versions "
                "WHERE page_id=? ORDER BY version ASC",
                (page["id"],),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── 存量 backfill ───────────────────────

    def backfill(self, limit: int = None) -> dict:
        """把已有 observation 全部归组生成知识页（存量迁移）。

        扫描 knowledge 层所有 source='reflection_observation' 且未归组的碎片，
        逐个 absorb。返回 {"absorbed": N, "pages_created": M, "pages_versioned": K}
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, text FROM memory_fragments "
                "WHERE layer='knowledge' AND source='reflection_observation' "
                "ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

        done_ids = self._all_grouped_obs_ids()
        pending = [r for r in rows if r["id"] not in done_ids]
        if limit:
            pending = pending[:limit]

        stats = {"absorbed": 0, "pages_created": 0, "pages_versioned": 0, "drafted": 0}
        for r in pending:
            res = self.absorb(r["id"], r["text"], compose=False)
            if res.get("action") == "created":
                stats["pages_created"] += 1
            elif res.get("action") == "versioned":
                stats["pages_versioned"] += 1
            elif res.get("action") == "drafted":
                stats["drafted"] += 1
            stats["absorbed"] += 1

        # 归组完成后统一成文（每页一次，避免每条 obs 都调 LLM）
        composed = self.compose_all()
        stats["composed"] = composed
        return stats

    def compose_all(self) -> int:
        """对未成文草稿页（obs≥MIN）成文 v1；已成文页不重写（增量吸收时才升版）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM knowledge_pages WHERE current_version=0"
            ).fetchall()
        finally:
            conn.close()
        n = 0
        for r in rows:
            try:
                obs_ids = json.loads(r["obs_ids"]) if r["obs_ids"] else []
            except Exception:
                obs_ids = []
            if len(obs_ids) < MIN_OBS_PER_PAGE:
                continue
            conn = self._conn()
            try:
                res = self._materialize(conn, r["id"], r["page_key"], obs_ids, first=True)
            finally:
                conn.close()
            if res and res.get("action") == "versioned":
                n += 1
        return n

    def _all_grouped_obs_ids(self) -> set:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT obs_ids FROM knowledge_pages").fetchall()
            ids = set()
            for r in rows:
                try:
                    ids.update(json.loads(r["obs_ids"]) if r["obs_ids"] else [])
                except Exception:
                    pass
            return ids
        finally:
            conn.close()

    # ── 内部辅助 ────────────────────────────

    def _find_best_page(self, conn, emb: list) -> dict:
        """找与 emb 最相似的页。

        两段式：先质心粗筛（快，1 次比对/页），质心相似度落在临界区
        （PAGE_MATCH_THRESHOLD ~ 0.75）才逐成员细查（single-linkage，
        成员比质心更贴近"同主题"直觉）。质心 ≥0.75 直接归入。
        """
        emb_n = _norm(emb)
        if not emb_n:
            return None
        rows = conn.execute("SELECT * FROM knowledge_pages").fetchall()
        best = None
        best_sim = PAGE_MATCH_THRESHOLD
        for r in rows:
            try:
                c = json.loads(r["centroid"]) if r["centroid"] else []
            except Exception:
                c = []
            if not c:
                continue
            csim = cosine_sim(emb_n, _norm(c) or c)
            if not csim:
                continue
            if csim >= 0.75:
                # 质心足够近，直接归入
                if csim > best_sim:
                    best_sim = csim
                    best = r
                continue
            if csim < PAGE_MATCH_THRESHOLD:
                continue
            # 临界区：逐成员细查（single-linkage）
            try:
                obs_ids = json.loads(r["obs_ids"]) if r["obs_ids"] else []
            except Exception:
                obs_ids = []
            for m in self._get_member_embeddings(conn, obs_ids):
                sim = cosine_sim(emb_n, m)
                if sim and sim > best_sim:
                    best_sim = sim
                    best = r
        return best

    def _get_obs_text(self, obs_id: int) -> str:
        conn = self._conn()
        try:
            r = conn.execute("SELECT text FROM memory_fragments WHERE id=?", (obs_id,)).fetchone()
            return r["text"] if r else ""
        finally:
            conn.close()

    def _get_obs_texts(self, conn, obs_ids: list) -> list:
        if not obs_ids:
            return []
        marks = ",".join("?" * len(obs_ids))
        rows = conn.execute(
            f"SELECT id, text FROM memory_fragments WHERE id IN ({marks})", obs_ids
        ).fetchall()
        by_id = {r["id"]: r["text"] for r in rows}
        return [by_id.get(i, "") for i in obs_ids if by_id.get(i)]

    def _get_member_embeddings(self, conn, obs_ids: list) -> list:
        if not obs_ids:
            return []
        marks = ",".join("?" * len(obs_ids))
        rows = conn.execute(
            f"SELECT id, text, embedding FROM memory_fragments WHERE id IN ({marks})", obs_ids
        ).fetchall()
        embs = []
        for r in rows:
            try:
                e = json.loads(r["embedding"]) if r["embedding"] else []
            except Exception:
                e = []
            if not e and embed is not None:
                # 碎片表没存向量（如外部插入）→ 现算并回填
                e = embed(r["text"])
                if e:
                    try:
                        conn.execute("UPDATE memory_fragments SET embedding=? WHERE id=?",
                                     (json.dumps(e), r["id"]))
                        conn.commit()
                    except Exception:
                        pass
            if e:
                embs.append(_norm(e) or e)
        return embs

    def _get_latest_content(self, conn, page_id: int) -> str:
        r = conn.execute(
            "SELECT content FROM knowledge_page_versions WHERE page_id=? ORDER BY version DESC LIMIT 1",
            (page_id,),
        ).fetchone()
        return r["content"] if r else ""

    def _gen_page_key(self, conn, obs_id: int) -> str:
        """草稿页起名：取观察文本前 12 字（后续成文时 LLM 可改 title）。
        存量观察文本开头雷同（多为「我…」句式），key 可能撞唯一约束，
        冲突时追加 obs_id 后缀保证唯一。"""
        text = self._get_obs_text(obs_id)
        key = text.strip()[:12].replace(" ", "_").replace("\n", "_")
        if not key:
            key = f"obs_{obs_id}"
        exists = conn.execute(
            "SELECT 1 FROM knowledge_pages WHERE page_key=?", (key,)
        ).fetchone()
        if exists:
            key = f"{key}_{obs_id}"
        return key

    def _parse_compose(self, resp: str) -> dict:
        """解析成文输出（容错：找 JSON / 直接用原文）。"""
        import re
        if not resp:
            return {}
        # 找 ```json ... ``` 或纯 JSON
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", resp, re.S)
        target = m.group(1) if m else resp
        try:
            obj = json.loads(target)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        # 找不到 JSON：尝试从文本里找 {"content":
        try:
            start = resp.find("{")
            end = resp.rfind("}")
            if start >= 0 and end > start:
                obj = json.loads(resp[start:end + 1])
                if isinstance(obj, dict):
                    return obj
        except Exception:
            pass
        # 兜底：整段文本当 content
        return {"content": resp.strip(), "title": ""}


# ── 模块级便捷函数（供领域书等外部调用） ─────────────


def _get_authority_count(obs_ids: list) -> int:
    """统计知识页成员中 authority_level=1 的条数。"""
    if not obs_ids:
        return 0
    try:
        import sqlite3
        conn = sqlite3.connect(_get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            ph = ",".join("?" * len(obs_ids))
            row = conn.execute(
                f"SELECT COUNT(*) c FROM memory_fragments WHERE id IN ({ph}) AND authority_level=1",
                obs_ids,
            ).fetchone()
            return row["c"] if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def get_page_meta(page_key: str) -> dict:
    """展示用元信息：{page_key, title, version, authority, obs_count}。查不到返回 None。"""
    try:
        import sqlite3
        conn = sqlite3.connect(_get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            page = conn.execute(
                "SELECT id, page_key, title, current_version, obs_ids FROM knowledge_pages WHERE page_key=?",
                (page_key,),
            ).fetchone()
        finally:
            conn.close()
        if not page:
            return None
        obs_ids = json.loads(page["obs_ids"]) if page["obs_ids"] else []
        return {
            "page_key": page_key,
            "title": page["title"] or page_key,
            "version": page["current_version"],
            "authority": _get_authority_count(obs_ids),
            "obs_count": len(obs_ids),
        }
    except Exception:
        return None


def get_related_books(page_key: str) -> list:
    """反查：该知识页挂载在哪些领域书页上。返回 [{page_id, title, version}]。"""
    try:
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        book_path = os.path.join(root, "data", "domain_book.json")
        if not os.path.exists(book_path):
            return []
        with open(book_path, "r", encoding="utf-8") as f:
            book = json.load(f)
        result = []
        for pid, pg in book.get("pages", {}).items():
            refs = pg.get("knowledge_refs", []) or []
            if page_key in refs:
                result.append({
                    "page_id": pid,
                    "title": pg.get("title", pid),
                    "version": pg.get("version", 1),
                })
        return result
    except Exception:
        return []
