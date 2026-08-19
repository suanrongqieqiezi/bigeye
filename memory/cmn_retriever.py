"""
CMNRetriever — CMN P5 统一检索器

双路检索：
- hash 寻址：知道 id/hash 直接拿（给孤立节点用）
- 网络走边：沿关系边跳（derive/support/negate/weak_assoc）

confidence_decay 信号：
- 文件晶体：hash 最后验证时间越久，decay 越低
- 自传晶体：被多少新晶体引用/否定过

设计参考：[CMN实施方案.md] 第七章 P5
"""
import os
import sys
import json
import time
import sqlite3

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_db_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


# ── 阈值常量 ───────────────────────────────────────────

DECAY_HALF_LIFE_DAYS = 30      # confidence_decay 半衰期：30 天未验证 decay 减半
DEFAULT_TOP_K = 10
NETWORK_HOP_LIMIT = 2          # 网络走边最多跳 2 层


class CMNRetriever:
    """CMN 统一检索器。

    职责：
    1. 双路检索：hash 寻址 + 网络走边
    2. confidence_decay 计算：按需验证信号
    3. 统一自传晶体 + 文件晶体的召回

    用法：
        retriever = CMNRetriever()
        # 双路检索
        results = retriever.retrieve("SQLite KNN 加速", top_k=10)
        # hash 寻址
        crystal = retriever.get_by_hash("abc123")
        # 网络走边（从某晶体出发找关联）
        neighbors = retriever.walk_edges(crystal_id, hops=2)
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or _get_db_path()
        self._import_stores()

    def _import_stores(self):
        """懒加载 stores。"""
        try:
            from memory.fragment_store import FragmentStore
            self.fragment_store = FragmentStore()
        except Exception:
            self.fragment_store = None
        try:
            from memory.file_crystal_store import FileCrystalStore
            self.file_crystal_store = FileCrystalStore()
        except Exception:
            self.file_crystal_store = None
        try:
            from memory.relation_store import RelationStore
            self.relation_store = RelationStore()
        except Exception:
            self.relation_store = None

    # ── 主检索入口：双路检索 ───────────────────────────

    def retrieve(self, query: str, top_k: int = DEFAULT_TOP_K,
                 include_files: bool = True, include_self: bool = True,
                 walk_network: bool = True) -> dict:
        """双路检索：向量召回（hash 寻址兜底）+ 网络走边扩展。

        Args:
            query: 查询文本
            top_k: 返回数量
            include_files: 是否包含文件晶体
            include_self: 是否包含自传晶体
            walk_network: 是否走边扩展（关掉则纯向量召回）

        Returns:
            {
                "query": query,
                "direct_hits": [...],     # 向量召回的直接命中
                "network_hits": [...],    # 走边扩展的关联晶体
                "total": N,
                "decay_warnings": [...]   # decay 过低的晶体警告
            }
        """
        direct_hits = []
        network_hits = []

        # 路径1：向量召回（hash 寻址兜底）
        if include_self and self.fragment_store:
            try:
                frags = self.fragment_store.recall(query, top_k=top_k, threshold=0.3)
                direct_hits.extend([self._enrich_with_decay(f) for f in frags])
            except Exception as e:
                print(f"[cmn_retriever] 自传晶体召回失败: {e}")

        if include_files and self.file_crystal_store:
            try:
                files = self.file_crystal_store.recall(query, top_k=top_k)
                direct_hits.extend([self._enrich_with_decay(f) for f in files])
            except Exception as e:
                print(f"[cmn_retriever] 文件晶体召回失败: {e}")

        # 去重（按 id）
        seen_ids = set()
        deduped_direct = []
        for h in direct_hits:
            hid = h.get("id")
            if hid and hid not in seen_ids:
                seen_ids.add(hid)
                deduped_direct.append(h)
        direct_hits = deduped_direct[:top_k]

        # 路径2：网络走边扩展
        if walk_network and self.relation_store:
            network_hits = self._expand_via_edges(direct_hits, top_k=top_k)
            # 去重（排除已在 direct_hits 里的）
            network_hits = [h for h in network_hits if h.get("id") not in seen_ids]
            network_hits = network_hits[:top_k]

        # decay 警告
        all_hits = direct_hits + network_hits
        decay_warnings = [
            {"id": h.get("id"), "decay": h.get("confidence_decay", 1.0),
             "reason": h.get("_decay_reason", "")}
            for h in all_hits
            if h.get("confidence_decay", 1.0) < 0.5
        ]

        return {
            "query": query,
            "direct_hits": direct_hits,
            "network_hits": network_hits,
            "total": len(direct_hits) + len(network_hits),
            "decay_warnings": decay_warnings,
        }

    # ── hash 寻址 ──────────────────────────────────────

    def get_by_hash(self, content_hash: str) -> dict:
        """按 hash 寻址晶体（自传 + 文件都查）。"""
        result = {"hash": content_hash, "self_crystals": [], "file_crystals": []}

        if self.fragment_store:
            try:
                result["self_crystals"] = self.fragment_store.get_by_hash(content_hash)
            except Exception:
                pass

        if self.file_crystal_store:
            try:
                result["file_crystals"] = self.file_crystal_store.get_by_hash(content_hash)
            except Exception:
                pass

        return result

    def get_by_id(self, crystal_id: str) -> dict:
        """按 id 查晶体（自动判断自传还是文件）。"""
        # 文件晶体 id 以 fc_ 开头
        if crystal_id.startswith("fc_"):
            if self.file_crystal_store:
                return self.file_crystal_store.get_by_id(crystal_id)
            return None
        # 自传晶体 id 是整数
        try:
            fid = int(crystal_id)
            if self.fragment_store:
                conn = sqlite3.connect(self.db_path, timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=30000")
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(
                        "SELECT * FROM memory_fragments WHERE id=?", (fid,)
                    ).fetchone()
                    return dict(row) if row else None
                finally:
                    conn.close()
        except ValueError:
            pass
        return None

    # ── 网络走边 ───────────────────────────────────────

    def walk_edges(self, crystal_id: str, hops: int = NETWORK_HOP_LIMIT) -> list:
        """从某晶体出发，沿关系边走 N 跳，返回所有关联晶体。"""
        if not self.relation_store:
            return []

        visited = {crystal_id}
        frontier = [crystal_id]
        results = []

        for hop in range(hops):
            if not frontier:
                break
            next_frontier = []
            for cid in frontier:
                # 找该晶体关联的所有边
                neighbors = self._get_neighbors(cid)
                for n_id in neighbors:
                    if n_id not in visited:
                        visited.add(n_id)
                        next_frontier.append(n_id)
                        # 取晶体详情
                        crystal = self.get_by_id(str(n_id))
                        if crystal:
                            crystal["_hop"] = hop + 1
                            crystal["_via"] = cid
                            results.append(crystal)
            frontier = next_frontier
        return results

    def _get_neighbors(self, crystal_id: str) -> list:
        """找某晶体的所有邻居（自传晶体走 memory_relations，文件晶体走 crystal_parent 链）。"""
        neighbors = set()
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            # 自传晶体：走 memory_relations（source_fragment_id 或 object_id）
            try:
                fid = int(crystal_id)
                # 作为 source_fragment_id
                rows = conn.execute(
                    "SELECT object_id FROM memory_relations WHERE source_fragment_id=? AND status='active'",
                    (fid,)
                ).fetchall()
                for r in rows:
                    if r["object_id"]:
                        neighbors.add(str(r["object_id"]))
                # 作为 object_id
                rows = conn.execute(
                    "SELECT source_fragment_id FROM memory_relations WHERE object_id=? AND status='active'",
                    (fid,)
                ).fetchall()
                for r in rows:
                    if r["source_fragment_id"]:
                        neighbors.add(str(r["source_fragment_id"]))
            except ValueError:
                pass

            # 文件晶体：走 crystal_parent_id / raw_source_id 链
            if crystal_id.startswith("fc_"):
                # 下钻：找 crystal_parent_id 指向它的
                rows = conn.execute(
                    "SELECT id FROM file_crystals WHERE crystal_parent_id=? AND status='active'",
                    (crystal_id,)
                ).fetchall()
                for r in rows:
                    neighbors.add(r["id"])
                # 上钻：找它的 crystal_parent_id
                row = conn.execute(
                    "SELECT crystal_parent_id FROM file_crystals WHERE id=?",
                    (crystal_id,)
                ).fetchone()
                if row and row["crystal_parent_id"]:
                    neighbors.add(row["crystal_parent_id"])
        finally:
            conn.close()
        return list(neighbors)

    def _expand_via_edges(self, seeds: list, top_k: int) -> list:
        """对种子晶体走边扩展。"""
        if not seeds:
            return []
        expanded = []
        seen = {s.get("id") for s in seeds if s.get("id")}
        for seed in seeds[:5]:  # 只对前 5 个种子扩展
            sid = str(seed.get("id", ""))
            if not sid:
                continue
            neighbors = self.walk_edges(sid, hops=1)
            for n in neighbors:
                if n.get("id") not in seen:
                    enriched = self._enrich_with_decay(n)
                    expanded.append(enriched)
                    seen.add(n.get("id"))
        return expanded[:top_k]

    def _expand_via_ids(self, ids: list, top_k: int = 5) -> list:
        """对种子 id 列表走边扩展（crystal_recall 用）。"""
        if not ids:
            return []
        expanded = []
        seen = set(ids)
        for sid in ids[:5]:
            sid = str(sid)
            if not sid:
                continue
            try:
                neighbors = self.walk_edges(sid, hops=1)
            except Exception:
                continue
            for n in neighbors:
                if n.get("id") not in seen:
                    enriched = self._enrich_with_decay(n)
                    expanded.append(enriched)
                    seen.add(n.get("id"))
        return expanded[:top_k]

    # ── confidence_decay 信号 ─────────────────────────

    def _enrich_with_decay(self, crystal: dict) -> dict:
        """给晶体计算并附加 confidence_decay 信号。"""
        if not crystal:
            return crystal

        # 已经有 confidence_decay 字段的（自传晶体），直接用
        if "confidence_decay" in crystal and crystal["confidence_decay"] is not None:
            decay = crystal["confidence_decay"]
            # 文件晶体按 last_hash_verified_at 衰减
            if crystal.get("node_type") == "file" or crystal.get("source_type"):
                decay = self._compute_file_decay(crystal)
            crystal["confidence_decay"] = decay
            if decay < 0.5:
                crystal["_decay_reason"] = "长时间未验证 hash"
            return crystal

        # 文件晶体：按 last_hash_verified_at 计算
        if crystal.get("source_type") or crystal.get("layer") is not None:
            crystal["confidence_decay"] = self._compute_file_decay(crystal)
            if crystal["confidence_decay"] < 0.5:
                crystal["_decay_reason"] = "长时间未验证 hash"
        else:
            # 自传晶体默认 decay=1.0
            crystal["confidence_decay"] = 1.0
        return crystal

    def _compute_file_decay(self, crystal: dict) -> float:
        """文件晶体 confidence_decay：按 last_hash_verified_at 衰减。

        半衰期 30 天：30 天未验证 decay=0.5，60 天 decay=0.25。
        """
        verified_at = crystal.get("last_hash_verified_at")
        if not verified_at:
            return 0.3  # 从未验证过，给低 decay
        now = time.time()
        age_days = (now - verified_at) / 86400.0
        decay = 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
        return max(decay, 0.1)  # 最低 0.1

    # ── 批量更新 decay（定时任务用） ──────────────────

    def refresh_decay_all(self) -> dict:
        """批量重算所有晶体的 confidence_decay。

        供定时任务或反思回路调用。
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        updated = 0
        try:
            # 文件晶体
            rows = conn.execute(
                "SELECT id, last_hash_verified_at FROM file_crystals WHERE status='active'"
            ).fetchall()
            now = time.time()
            for r in rows:
                verified_at = r["last_hash_verified_at"]
                if not verified_at:
                    decay = 0.3
                else:
                    age_days = (now - verified_at) / 86400.0
                    decay = max(0.5 ** (age_days / DECAY_HALF_LIFE_DAYS), 0.1)
                conn.execute(
                    "UPDATE file_crystals SET confidence_decay=? WHERE id=?",
                    (decay, r["id"])
                )
                updated += 1
            conn.commit()
            return {"updated": updated, "type": "file_crystals"}
        finally:
            conn.close()

    # ── 按需验证：手动触发 hash 验证 ──────────────────

    def verify_hash(self, crystal_id: str) -> dict:
        """按需验证某晶体的 hash（AI 主动下钻时调用）。

        文件晶体：重新读文件切片算 hash，比对。
        自传晶体：summary_hash 不变（内容固定），直接更新时间戳。
        """
        if crystal_id.startswith("fc_"):
            return self._verify_file_crystal(crystal_id)
        else:
            return self._verify_self_crystal(crystal_id)

    def _verify_file_crystal(self, crystal_id: str) -> dict:
        """验证文件晶体：重读文件切片算 hash。"""
        if not self.file_crystal_store:
            return {"error": "file_crystal_store 未初始化"}

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT source_path, slice_index, slice_hash FROM file_crystals WHERE id=?",
                (crystal_id,)
            ).fetchone()
            if not row:
                return {"error": f"晶体不存在: {crystal_id}"}

            # 重读文件对应切片算 hash
            import hashlib
            from memory.file_slicer import slice_file
            try:
                slices = slice_file(row["source_path"])
                idx = row["slice_index"]
                if idx >= len(slices):
                    return {"crystal_id": crystal_id, "verified": False,
                            "reason": "切片序号超出范围（文件可能缩短）"}
                new_hash = hashlib.sha256(slices[idx].content.encode("utf-8")).hexdigest()[:16]
                old_hash = row["slice_hash"]
                now = time.time()

                if new_hash == old_hash:
                    # hash 一致 → 更新验证时间戳，decay 重置为 1.0
                    conn.execute(
                        "UPDATE file_crystals SET last_hash_verified_at=?, confidence_decay=1.0 WHERE id=?",
                        (now, crystal_id)
                    )
                    conn.commit()
                    return {"crystal_id": crystal_id, "verified": True,
                            "hash_match": True, "new_hash": new_hash}
                else:
                    # hash 变了 → 标 stale，decay 降为 0
                    conn.execute(
                        "UPDATE file_crystals SET status='stale', confidence_decay=0.0, updated_at=? WHERE id=?",
                        (now, crystal_id)
                    )
                    conn.commit()
                    return {"crystal_id": crystal_id, "verified": True,
                            "hash_match": False, "old_hash": old_hash, "new_hash": new_hash,
                            "action": "marked_stale"}
            except FileNotFoundError:
                return {"crystal_id": crystal_id, "verified": False,
                        "reason": f"文件不存在: {row['source_path']}"}
        finally:
            conn.close()

    def _verify_self_crystal(self, crystal_id: str) -> dict:
        """验证自传晶体：summary_hash 不变则更新时间戳。"""
        if not self.fragment_store:
            return {"error": "fragment_store 未初始化"}

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            fid = int(crystal_id)
            row = conn.execute(
                "SELECT summary_hash, text FROM memory_fragments WHERE id=?", (fid,)
            ).fetchone()
            if not row:
                return {"error": f"晶体不存在: {crystal_id}"}

            # 自传晶体内容固定，重算 text hash 验证
            import hashlib
            current_hash = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()[:16]
            stored_hash = row["summary_hash"]
            now = time.time()

            if stored_hash is None or current_hash == stored_hash:
                # 一致 → 更新时间戳
                conn.execute(
                    "UPDATE memory_fragments SET summary_hash=?, last_hash_verified_at=?, confidence_decay=1.0 WHERE id=?",
                    (current_hash, now, fid)
                )
                conn.commit()
                return {"crystal_id": crystal_id, "verified": True, "hash_match": True}
            else:
                # 内容变了（被 edit 过）→ 标记
                conn.execute(
                    "UPDATE memory_fragments SET confidence_decay=0.5, last_hash_verified_at=? WHERE id=?",
                    (now, fid)
                )
                conn.commit()
                return {"crystal_id": crystal_id, "verified": True, "hash_match": False,
                        "action": "decay_lowered"}
        finally:
            conn.close()

    # ── 统计 ───────────────────────────────────────────

    def stats(self) -> dict:
        """检索器统计。"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            # 自传晶体
            self_total = conn.execute(
                "SELECT COUNT(*) as c FROM memory_fragments WHERE node_type='self' AND dirty=1"
            ).fetchone()["c"]
            self_authority = conn.execute(
                "SELECT COUNT(*) as c FROM memory_fragments WHERE node_type='self' AND authority_level=1"
            ).fetchone()["c"]
            # 文件晶体
            file_total = conn.execute(
                "SELECT COUNT(*) as c FROM file_crystals WHERE status='active'"
            ).fetchone()["c"] if self._table_exists(conn, "file_crystals") else 0
            file_stale = conn.execute(
                "SELECT COUNT(*) as c FROM file_crystals WHERE status='stale'"
            ).fetchone()["c"] if self._table_exists(conn, "file_crystals") else 0
            # 边
            edges_by_type = {}
            if self._table_exists(conn, "memory_relations"):
                for r in conn.execute(
                    "SELECT edge_type, COUNT(*) as c FROM memory_relations WHERE status='active' GROUP BY edge_type"
                ).fetchall():
                    edges_by_type[r["edge_type"]] = r["c"]
            # decay 分布
            low_decay = conn.execute(
                "SELECT COUNT(*) as c FROM file_crystals WHERE confidence_decay < 0.5 AND status='active'"
            ).fetchone()["c"] if self._table_exists(conn, "file_crystals") else 0

            return {
                "self_crystals": self_total,
                "self_authority": self_authority,
                "file_crystals": file_total,
                "file_stale": file_stale,
                "edges_by_type": edges_by_type,
                "low_decay_count": low_decay,
            }
        finally:
            conn.close()

    def _table_exists(self, conn, table: str) -> bool:
        try:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 0")
            return True
        except sqlite3.OperationalError:
            return False


# ── Singleton ──────────────────────────────────────────

_retriever = None


def get_retriever():
    global _retriever
    if _retriever is None:
        _retriever = CMNRetriever()
    return _retriever
