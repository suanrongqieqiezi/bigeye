"""
ReflectionLoop — CMN P4 反思回路

五项职责：
1. 建弱关联：扫描孤立晶体，用 embedding 相似度建 weak_assoc 边
2. 涌现元晶：发现晶体群共通模式，提炼元晶（crystal_parent 链）
3. 反熵修剪：删失效边、合并重复边、降权低 confidence 弱关联
4. 自检缺口：发现"AI 还没有晶体"的盲区
5. 提拔权威：满足多源印证/多次验证的晶体提为 authority=1

设计参考：[CMN实施方案.md] 第六章 P4
"""
import os
import sys
import json
import time
import hashlib

# ── DB path helper ──────────────────────────────────────

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_db_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


# ── 阈值常量 ───────────────────────────────────────────

WEAK_ASSOC_THRESHOLD = 0.65     # 相似度高于此值才建弱关联（256维embedding区分度有限，0.75太严）
WEAK_ASSOC_MAX_PER_RUN = 20     # 每次反思最多建 20 条弱关联
WEAK_ASSOC_CONFIDENCE = 0.5     # 弱关联初始 confidence

META_CRYSTAL_THRESHOLD = 0.65   # 群内平均相似度高于此值才涌现元晶（256维embedding区分度有限，0.80太严）
META_CRYSTAL_MIN_GROUP = 3      # 至少 3 个晶体成群才涌现
META_CRYSTAL_MAX_PER_RUN = 5    # 每次反思最多涌现 5 个元晶

# P6 叙事沉淀阈值
STORY_CONSOLIDATION_THRESHOLD = 0.72  # 碎片间相似度高于此值才聚成一组整理成故事
STORY_MIN_GROUP = 3                   # 至少 3 条碎片成组才整理
STORY_MAX_PER_RUN = 3                 # 每次反思最多整理 3 个故事

# P6 故事网络（跨主题弱关联）阈值
STORY_WEAK_ASSOC_SIM_THRESHOLD = 0.70  # 故事间 embedding 相似度高于此值建弱关联
STORY_WEAK_ASSOC_MAX_PER_RUN = 10      # 每次反思最多建 10 条故事弱关联
STORY_ENTITY_COOCURRENCE_MIN = 1       # 两个故事共享 ≥N 个实体才建实体共现边

PRUNE_LOW_CONFIDENCE = 0.2      # confidence 低于此值的弱关联边被删
PRUNE_STALE_DAYS = 30           # 超过 30 天未验证的边降权

AUTHORITY_MULTI_SOURCE = 3      # 多源印证：≥3 个独立来源说同样的事
AUTHORITY_MULTI_VERIFY = 3      # 多次验证：被反思回路反复确认 ≥3 次没被推翻


class ReflectionLoop:
    """CMN P4 反思回路。

    用法：
        loop = ReflectionLoop()
        report = loop.run()
        # report = {"weak_assoc": N, "meta_crystals": M, "pruned": P,
        #           "gaps": G, "promoted": K}
    """

    def __init__(self, db_path=None, llm_call_fn=None):
        self.db_path = db_path or _get_db_path()
        self.llm_call_fn = llm_call_fn  # 可选：LLM 调用函数 (prompt, user) -> str
        self._import_stores()

    def _import_stores(self):
        """懒加载 stores。"""
        try:
            from memory.fragment_store import FragmentStore
            self.fragment_store = FragmentStore()
        except Exception:
            self.fragment_store = None
        try:
            from memory.relation_store import RelationStore
            self.relation_store = RelationStore()
        except Exception:
            self.relation_store = None
        try:
            from memory.entity_store import EntityStore
            self.entity_store = EntityStore()
        except Exception:
            self.entity_store = None

    # ── 主入口 ─────────────────────────────────────────

    def run(self) -> dict:
        """执行反思回路五项职责。"""
        report = {
            "weak_assoc": 0,
            "meta_crystals": 0,
            "pruned": 0,
            "gaps": 0,
            "promoted": 0,
            "knowledge_linked": 0,
            "stories_consolidated": 0,
            "story_weak_assocs": 0,
            "observations_consolidated": {"create": 0, "update": 0, "merge": 0, "contradict": 0, "skipped": 0},
            "entities_backfilled": 0,
            "file_thoughts_extracted": 0,
            "errors": [],
        }

        if not self.fragment_store or not self.relation_store:
            report["errors"].append("stores 未初始化")
            return report

        # 前置：回填实体（detect_gaps 依赖 entities 表）
        try:
            report["entities_backfilled"] = self.backfill_entities()
        except Exception as e:
            report["errors"].append(f"backfill_entities: {e}")

        # 新增：从对话历史提取 AI 对文件的真实思考
        try:
            report["file_thoughts_extracted"] = self.extract_file_thoughts()
        except Exception as e:
            report["errors"].append(f"extract_file_thoughts: {e}")

        try:
            report["weak_assoc"] = self.build_weak_associations()
        except Exception as e:
            report["errors"].append(f"build_weak_associations: {e}")

        try:
            report["meta_crystals"] = self.emerge_meta_crystals()
        except Exception as e:
            report["errors"].append(f"emerge_meta_crystals: {e}")

        try:
            report["pruned"] = self.prune_entropy()
        except Exception as e:
            report["errors"].append(f"prune_entropy: {e}")

        try:
            report["gaps"] = len(self.detect_gaps())
        except Exception as e:
            report["errors"].append(f"detect_gaps: {e}")

        try:
            report["promoted"] = self.promote_authority()
        except Exception as e:
            report["errors"].append(f"promote_authority: {e}")

        try:
            report["knowledge_linked"] = self.link_knowledge_cores()
        except Exception as e:
            report["errors"].append(f"link_knowledge_cores: {e}")

        try:
            report["stories_consolidated"] = self.narrative_consolidation()
        except Exception as e:
            report["errors"].append(f"narrative_consolidation: {e}")

        # P6 故事网络：跨主题故事弱关联（实体共现优先，相似度兜底）
        try:
            report["story_weak_assocs"] = self.build_story_weak_assocs()
        except Exception as e:
            report["errors"].append(f"build_story_weak_assocs: {e}")

        # 职责9：观察整合（吸取 hindsight consolidation）
        try:
            report["observations_consolidated"] = self.consolidate_observations()
        except Exception as e:
            report["errors"].append(f"consolidate_observations: {e}")

        return report




    # ── 职责1：建弱关联 ────────────────────────────────

    def build_weak_associations(self) -> int:
        """扫描孤立晶体，用 embedding 相似度建 weak_assoc 边。

        策略：取最近 N 个孤立晶体，两两算相似度，高于阈值的建 weak_assoc 边。
        """
        import sqlite3
        from memory.embedder import embed

        # 找孤立晶体
        isolated = self.relation_store.find_isolated_fragments(limit=30)
        if len(isolated) < 2:
            return 0

        # 取 embedding
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            ids = [r["id"] for r in isolated]
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, embedding, text FROM memory_fragments WHERE id IN ({placeholders})",
                ids
            ).fetchall()
        finally:
            conn.close()

        embs = {}
        for r in rows:
            try:
                embs[r["id"]] = json.loads(r["embedding"])
            except Exception:
                embs[r["id"]] = []

        # 两两算相似度
        count = 0
        created_pairs = set()
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                if count >= WEAK_ASSOC_MAX_PER_RUN:
                    break
                a, b = rows[i], rows[j]
                pair_key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if pair_key in created_pairs:
                    continue

                sim = _cosine(embs.get(a["id"], []), embs.get(b["id"], []))
                if sim >= WEAK_ASSOC_THRESHOLD:
                    # 建 weak_assoc 边（subject_id 用 a，object_id 用 b）
                    reason = f"embedding 相似度 {sim:.3f}"
                    try:
                        self.relation_store.add(
                            subject_id=a["id"],
                            predicate="similar_to",
                            object_id=b["id"],
                            edge_type="weak_assoc",
                            confidence=WEAK_ASSOC_CONFIDENCE,
                            source_fragment_id=a["id"],
                            reason=reason,
                        )
                        created_pairs.add(pair_key)
                        count += 1
                    except Exception:
                        pass
            if count >= WEAK_ASSOC_MAX_PER_RUN:
                break
        return count

    # ── 职责2：涌现元晶 ────────────────────────────────

    def emerge_meta_crystals(self) -> int:
        """发现晶体群共通模式，提炼元晶。

        策略：用 weak_assoc 边找连通分量，分量内平均相似度高于阈值的群
        提炼一个元晶（crystal_parent 指向子晶体）。
        """
        import sqlite3
        from memory.embedder import embed

        # 找所有 weak_assoc 边构建图
        weak_edges = self.relation_store.get_edges_by_type("weak_assoc", limit=200)
        if len(weak_edges) < META_CRYSTAL_MIN_GROUP:
            return 0

        # 构建邻接表找连通分量
        adj = {}
        for e in weak_edges:
            s, o = e["subject_id"], e["object_id"]
            adj.setdefault(s, set()).add(o)
            adj.setdefault(o, set()).add(s)

        # BFS 找连通分量
        visited = set()
        components = []
        for node in adj:
            if node in visited:
                continue
            comp = set()
            queue = [node]
            while queue:
                n = queue.pop()
                if n in visited:
                    continue
                visited.add(n)
                comp.add(n)
                queue.extend(adj.get(n, set()) - visited)
            if len(comp) >= META_CRYSTAL_MIN_GROUP:
                components.append(list(comp))

        if not components:
            return 0

        # 取每个分量的 fragment 内容
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            count = 0
            for comp in components[:META_CRYSTAL_MAX_PER_RUN]:
                placeholders = ",".join("?" * len(comp))
                rows = conn.execute(
                    f"SELECT id, text, embedding FROM memory_fragments WHERE id IN ({placeholders})",
                    comp
                ).fetchall()
                if len(rows) < META_CRYSTAL_MIN_GROUP:
                    continue

                # 算群内平均相似度
                embs = []
                for r in rows:
                    try:
                        embs.append(json.loads(r["embedding"]))
                    except Exception:
                        embs.append([])

                avg_sim = _avg_pairwise_cosine(embs)
                if avg_sim < META_CRYSTAL_THRESHOLD:
                    continue

                # 提炼元晶（用 LLM 总结群内共通模式，或机械拼接）
                texts = [r["text"][:200] for r in rows]
                meta_text = self._generate_meta_crystal(texts, avg_sim)

                # 存为元晶：crystal_parent_id=NULL（它是顶层），raw_source 指向第一个子晶体
                # 元晶体降权：importance=5（跟普通记忆一样），不靠加成霸榜
                # 元晶应该靠真 relevance 上榜，不靠 importance/weight 顶上来
                meta_id = self.fragment_store.add(
                    meta_text,
                    source="reflection_meta",
                    tags="meta_crystal,emerged",
                    importance=5.0,  # 从 7.0 降到 5.0：防霸榜
                    epistemic="opinion",  # 元晶是 AI 的判断
                    node_type="self",
                    crystal_parent_id=None,
                    raw_source_id=str(rows[0]["id"]),
                )

                if meta_id:
                    # 更新子晶体的 crystal_parent_id 指向元晶
                    for r in rows:
                        self.fragment_store.update_cmn_fields(
                            r["id"], crystal_parent_id=str(meta_id)
                        )
                    count += 1
            return count
        finally:
            conn.close()

    def _generate_meta_crystal(self, texts: list, avg_sim: float) -> str:
        """生成元晶文本。LLM 可用时走三问压缩，否则机械拼接。"""
        combined = "\n".join(f"- {t}" for t in texts[:8])
        if self.llm_call_fn:
            try:
                prompt = (
                    f"以下是 {len(texts)} 个相似记忆（平均相似度 {avg_sim:.2f}）：\n{combined}\n\n"
                    "提炼它们的共通模式，按格式输出：\n"
                    "<<<结论>>>\n共同结论（不超过50字）\n\n"
                    "<<<为什么>>>\n共通因果（不超过150字）\n\n"
                    "<<<下一步>>>\n基于这个模式，下次该怎么做（不超过80字）"
                )
                result = self.llm_call_fn(prompt, "")
                if result and "<<<" in result:
                    return result.strip()
            except Exception:
                pass
        # 机械降级
        return (
            f"<<<结论>>>\n{len(texts)} 个记忆的共通模式（相似度 {avg_sim:.2f}）\n\n"
            f"<<<为什么>>>\n{combined[:400]}…\n\n"
            f"<<<下一步>>>\n无"
        )

    # ── 职责3：反熵修剪 ────────────────────────────────

    def prune_entropy(self) -> int:
        """反熵修剪：删低 confidence 边 + 合并重复边。"""
        import sqlite3
        count = 0

        # 3.1 删低 confidence 弱关联
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            cur = conn.execute(
                "DELETE FROM memory_relations WHERE edge_type='weak_assoc' AND confidence < ?",
                (PRUNE_LOW_CONFIDENCE,)
            )
            count += cur.rowcount
            conn.commit()
        finally:
            conn.close()

        # 3.2 合并重复边（同 subject+object+edge_type 只保留最新一条）
        duplicates = self.relation_store.find_duplicate_edges()
        for dup in duplicates:
            ids = [int(x) for x in dup["ids"].split(",") if x]
            if len(ids) <= 1:
                continue
            # 保留最大 id（最新），删其他
            keep = max(ids)
            for rid in ids:
                if rid != keep:
                    if self.relation_store.delete_edge(rid):
                        count += 1
        return count

    # ── 职责4：自检缺口 ────────────────────────────────

    def detect_gaps(self) -> list:
        """发现"AI 还没有晶体"的盲区。

        策略：找高频被关联的实体，但该实体没有对应的深度认知型记忆
       （source='deep' 或 importance>=7 的自传晶体）。
        """
        import sqlite3
        gaps = []
        if not self.entity_store:
            return gaps

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            # mention_count >= 3：被多条记忆关联的实体
            # NOT EXISTS：没有深度认知记忆（deep/importance>=7）专门反思过它
            rows = conn.execute(
                """SELECT e.id, e.name, e.type, e.mention_count
                   FROM memory_entities e
                   WHERE e.mention_count >= 3
                   AND NOT EXISTS (
                       SELECT 1 FROM memory_fragments f
                       WHERE f.node_type='self' AND f.dirty=1
                       AND (f.source='deep' OR f.importance >= 7)
                       AND f.text LIKE '%' || e.name || '%'
                   )
                   ORDER BY e.mention_count DESC LIMIT 20"""
            ).fetchall()
            for r in rows:
                gaps.append({
                    "entity_id": r["id"],
                    "entity_name": r["name"],
                    "entity_type": r["type"],
                    "mention_count": r["mention_count"],
                    "suggestion": f"实体「{r['name']}」被频繁提及但没有专门的自传晶体",
                })
            return gaps
        finally:
            conn.close()

    # ── 职责5：提拔权威 ────────────────────────────────

    def promote_authority(self) -> int:
        """满足条件的晶体提拔为权威。

        三条信号：
        1. 多源印证：≥3 个独立 source 的晶体说同样的事
        2. 多次验证：被反思回路反复确认 ≥3 次没被推翻（用 raw_count 近似）
        3. 文件晶体 authority=1 已在建库时设，这里只处理自传晶体
        """
        import sqlite3
        count = 0
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            now = time.time()

            # 信号2：多次验证（raw_count >= 3 且 authority=0）
            rows = conn.execute(
                """SELECT id, text, raw_count FROM memory_fragments
                   WHERE node_type='self' AND authority_level=0 AND dirty=1
                   AND raw_count >= ?""",
                (AUTHORITY_MULTI_VERIFY,)
            ).fetchall()
            for r in rows:
                if self.fragment_store.promote_to_authority(r["id"]):
                    count += 1

            # 信号1：多源印证（同 text 在 ≥3 个不同 source 出现）
            # 注：dedup 机制下相同 text 会合并 weight，但不同 source 的近似 text 不会
            # 这里用 entity_ids 共享 + text 相似来近似"多源"
            multi_source_rows = conn.execute(
                """SELECT f1.id FROM memory_fragments f1
                   WHERE f1.node_type='self' AND f1.authority_level=0 AND f1.dirty=1
                   AND (
                       SELECT COUNT(DISTINCT f2.source) FROM memory_fragments f2
                       WHERE f2.dirty=1 AND f2.text LIKE '%' || SUBSTR(f1.text, 1, 20) || '%'
                   ) >= ?""",
                (AUTHORITY_MULTI_SOURCE,)
            ).fetchall()
            for r in multi_source_rows:
                if self.fragment_store.promote_to_authority(r["id"]):
                    count += 1

            conn.commit()
            return count
        finally:
            conn.close()

    # ── 职责6：知识晶体关联同主题碎片 ────────────────────

    def link_knowledge_cores(self) -> int:
        """扫描所有 knowledge 层晶体，把同主题的 core 碎片关联过去。

        补漏：remember_knowledge 存时会即时关联一次，但之后新存的 core 碎片
        不会自动关联。反思回路定期跑这个补上。
        """
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            rows = conn.execute(
                """SELECT id, tags FROM memory_fragments
                   WHERE layer='knowledge' AND dirty=1"""
            ).fetchall()
        finally:
            conn.close()

        total = 0
        for r in rows:
            try:
                linked = self.fragment_store.link_cores_to_knowledge(
                    r["id"], topic_tag=r["tags"]
                )
                total += linked
            except Exception:
                pass
        return total

    # ── 职责8：叙事沉淀（P6） ──────────────────────────

    def narrative_consolidation(self) -> int:
        """把相关碎片整理成连贯故事（P6 叙事记忆层）。

        流程：
        1. 取未被 story 整理过的 core 碎片
        2. 按 embedding 相似度聚类（≥0.72，≥3 条成组）
        3. 组内按 created_at ASC 排序
        4. 同主题唯一化：查同主题是否已有 story
           - 已有 → 用 MERGE_STORY_PROMPT 合并旧故事+新碎片，update_story 更新
           - 没有 → 用 NARRATIVE_CONSOLIDATION_PROMPT 新建 story
        5. 存为 layer="story"
        6. link_materials 把素材碎片归档（dirty=0）
        7. 同 topic_id 的多条 story 之间建 next_in_topic 链
        """
        from memory.embedder import cosine_sim
        from prompts.narrative_prompts import (
            format_narrative_prompt, format_merge_story_prompt
        )

        # 1. 取未整理的 core 碎片（limit 开大：窗口太小会把新碎片挡在外面饿死，
        #    8-02 首轮整理后前 200 条全是低相似残留，8-03 后新碎片永远轮不到 → story 断更）
        cores = self.fragment_store.get_unconsolidated_cores(limit=5000)
        if len(cores) < STORY_MIN_GROUP:
            return 0

        # 2. 贪心聚类：取第一条，找所有相似的成组，标记已分组，重复
        grouped_ids = set()
        clusters = []
        for i, seed in enumerate(cores):
            if seed["id"] in grouped_ids:
                continue
            try:
                seed_emb = json.loads(seed["embedding"])
            except Exception:
                continue
            if not seed_emb:
                continue

            group = [seed]
            for other in cores[i+1:]:
                if other["id"] in grouped_ids:
                    continue
                try:
                    other_emb = json.loads(other["embedding"])
                except Exception:
                    continue
                if not other_emb:
                    continue
                sim = cosine_sim(seed_emb, other_emb)
                if sim >= STORY_CONSOLIDATION_THRESHOLD:
                    group.append(other)

            if len(group) >= STORY_MIN_GROUP:
                clusters.append(group)
                for g in group:
                    grouped_ids.add(g["id"])
                if len(clusters) >= STORY_MAX_PER_RUN:
                    break

        if not clusters:
            return 0

        # 3-7. 每组排序 → 唯一化判断 → LLM 叙事 → 存/更新 story → 归档 → 建链
        count = 0
        import time as _time
        for group in clusters:
            # 组内按 created_at ASC
            group.sort(key=lambda x: x.get("created_at", 0))

            # 提取主题（用第一条碎片的 tags 或文本前 20 字）
            topic = group[0].get("tags") or group[0]["text"][:20]
            # 组内多数 topic_id（如果有）
            topic_ids = [g.get("topic_id") for g in group if g.get("topic_id")]
            group_topic_id = topic_ids[0] if topic_ids else None

            # 事件时间：取组内最早和最晚的 created_at
            times = [g.get("created_at", 0) for g in group]
            try:
                t_start = _time.strftime("%Y-%m-%d", _time.localtime(min(times)))
                t_end = _time.strftime("%Y-%m-%d", _time.localtime(max(times)))
                event_time = f"{t_start} ~ {t_end}" if t_start != t_end else t_start
                span_days = (max(times) - min(times)) / 86400
                if span_days < 1:
                    event_span = "hours"
                elif span_days < 7:
                    event_span = "days"
                else:
                    event_span = "weeks"
            except Exception:
                event_time = None
                event_span = "single"

            source_ids = [g["id"] for g in group]

            # ── 同主题唯一化：查同主题是否已有 story ──
            # 用碎片组 embedding 均值去匹配 story，比用 topic 文本 embed 更准
            # （topic 通常是碎片前20字，短文本 vs story 长叙事文本相似度偏低）
            group_embs = []
            for g in group:
                try:
                    emb = json.loads(g["embedding"]) if g.get("embedding") else None
                    if emb:
                        group_embs.append(emb)
                except Exception:
                    pass
            group_emb_mean = None
            if group_embs:
                dim = len(group_embs[0])
                group_emb_mean = [
                    sum(e[i] for e in group_embs) / len(group_embs)
                    for i in range(dim)
                ]

            existing_story = self.fragment_store.find_story_by_topic(
                topic=topic, topic_id=group_topic_id, topic_emb=group_emb_mean
            )

            if existing_story:
                # 合并：旧故事 + 新碎片 → 重新叙事
                old_story_text = existing_story.get("text", "")
                prompt = format_merge_story_prompt(topic, old_story_text, group)
                llm_user_msg = "你在补充整理关于这件事的记忆，把它和之前的故事合在一起。"
                story_text = ""
                if self.llm_call_fn:
                    try:
                        story_text = self.llm_call_fn(prompt, llm_user_msg)
                    except Exception as e:
                        print(f"[reflection] merge narrative LLM failed: {e}")

                # 机械降级：LLM 失败时旧故事+新碎片拼接
                if not story_text or len(story_text) < 50:
                    parts = [f"[{g['text'][:80]}]" for g in group[:5]]
                    story_text = f"{old_story_text}\n\n后来又发生了：{' '.join(parts)}"

                # 更新已有 story
                ok = self.fragment_store.update_story(
                    story_id=existing_story["id"],
                    text=story_text,
                    source_ids=source_ids,
                    event_time=event_time,
                    event_span=event_span,
                    tags=topic,
                )
                if ok:
                    # 归档新素材碎片
                    self.fragment_store.link_materials(existing_story["id"], source_ids)
                    count += 1
                    print(f"[reflection] story #{existing_story['id']} merged: "
                          f"topic='{topic}', +{len(group)} fragments, "
                          f"event_time={event_time}")
            else:
                # 新建 story
                prompt = format_narrative_prompt(topic, group)
                story_text = ""
                if self.llm_call_fn:
                    try:
                        story_text = self.llm_call_fn(prompt, "你正在整理自己的记忆，把它串成故事。")
                    except Exception as e:
                        print(f"[reflection] narrative LLM failed: {e}")

                # 机械降级：LLM 失败时拼接碎片
                if not story_text or len(story_text) < 50:
                    parts = [f"[{g['text'][:80]}]" for g in group[:5]]
                    story_text = f"那天我经历了这些：{' '.join(parts)}"

                # 新建 story
                story_id = self.fragment_store.add(
                    story_text,
                    source="reflection_story",
                    tags=topic,
                    topic_id=group_topic_id,
                    importance=6.5,
                    epistemic="experience",
                    node_type="self",
                    layer="story",
                    event_time=event_time,
                    event_span=event_span,
                    source_ids=source_ids,
                )

                if story_id:
                    # 归档素材碎片
                    self.fragment_store.link_materials(story_id, source_ids)

                    # 同 topic_id 建链：找这个 topic 下的其他 story，建 next_in_topic
                    if group_topic_id:
                        self._link_story_to_topic_chain(story_id, group_topic_id)

                    count += 1
                    print(f"[reflection] story #{story_id} created: "
                          f"topic='{topic}', {len(group)} fragments, "
                          f"event_time={event_time}")

        return count

    # ── 职责9：观察整合（吸取 hindsight consolidation） ────────

    def consolidate_observations(self, limit: int = 30) -> dict:
        """把 dirty=1 的 core 碎片整合成 observation（knowledge 层）。

        吸取 hindsight 的四动作：create / update / merge / contradict。
        流程：
        1. 取 dirty=1 的 core 碎片（按 created_at 最旧的优先，增量消化）
        2. 每条碎片向量检索已有 observation（knowledge 层，相似度 ≥0.72）
        3. LLM 裁决四动作（create/update/merge/contradict）
        4. 执行：create→add knowledge；update→更新观察+证据数；
           merge→观察 raw_count+1；contradict→建矛盾边（memory_links）
        5. 碎片处理完标 dirty=0

        全可逆：合并/更新前源碎片原文进 memory_archive。
        """
        from prompts.consolidation_prompts import (
            format_observation_decision_prompt,
            format_merge_observations_prompt,
        )
        import sqlite3

        report = {"create": 0, "update": 0, "merge": 0, "contradict": 0, "skipped": 0}
        if not self.fragment_store:
            return report

        # 知识页层（懒加载，失败不阻塞 consolidate）
        page_store = None
        try:
            from memory.knowledge_pages import KnowledgePageStore
            page_store = KnowledgePageStore(db_path=self.db_path, llm_call_fn=self.llm_call_fn)
        except Exception as e:
            print(f"[reflection] knowledge_pages store init failed: {e}")
        page_report = {"created": 0, "versioned": 0, "drafted": 0}

        # 1. 取待整合的 core 碎片（dirty=1 且未被 story/observation 消化过）
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            rows = conn.execute(
                """SELECT id, text, created_at, topic_id, tags, source
                    FROM memory_fragments
                    WHERE layer='core' AND dirty=1
                    ORDER BY created_at ASC LIMIT ?""",
                (limit,)
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return report

        # 2-4. 逐条处理
        for row in rows:
            fid = row["id"]
            text = row["text"]
            try:
                # 2. 找相似观察（knowledge 层，≥0.72）
                obs = self.fragment_store.find_similar_knowledge(text, threshold=0.72, limit=3)

                # 3. LLM 裁决
                action = "create"
                obs_text = text
                target_id = None
                if self.llm_call_fn:
                    prompt = format_observation_decision_prompt(
                        {"id": fid, "text": text, "created_at": row["created_at"]},
                        obs or [],
                    )
                    try:
                        resp = self.llm_call_fn(prompt, "你正在整理自己的长期记忆。")
                        parsed = self._parse_obs_decision(resp)
                        action = parsed.get("action", "create")
                        target_id = parsed.get("target_id")
                        if parsed.get("observation_text"):
                            obs_text = parsed["observation_text"]
                    except Exception as e:
                        print(f"[reflection] obs decision LLM failed: {e}")
                        # 机械降级：有相似观察→merge，无→create
                        action = "merge" if obs else "create"

                # 4. 执行四动作
                if action == "create" or (action not in ("update", "merge", "contradict")):
                    oid = self.fragment_store.add(
                        obs_text,
                        source="reflection_observation",
                        tags=row["tags"] or "",
                        topic_id=row["topic_id"],
                        importance=7.0,
                        epistemic="experience",
                        node_type="self",
                        layer="knowledge",
                        source_ids=[fid],
                    )
                    if oid:
                        # 源碎片归档为素材
                        self.fragment_store.link_materials(oid, [fid])
                        report["create"] += 1
                        print(f"[reflection] observation #{oid} created from #{fid}")
                        # 知识页归组
                        if page_store:
                            try:
                                res = page_store.absorb(oid, obs_text)
                                self._count_page_action(page_report, res)
                            except Exception as e:
                                print(f"[reflection] page absorb(create) #{oid} failed: {e}")

                elif action == "update" and target_id:
                    ok = self.fragment_store.update_observation(
                        target_id, obs_text, new_source_id=fid
                    )
                    if ok:
                        report["update"] += 1
                        print(f"[reflection] observation #{target_id} updated by #{fid}")
                        # 观察内容变了 → 知识页升版
                        if page_store:
                            try:
                                res = page_store.absorb(target_id, obs_text)
                                self._count_page_action(page_report, res)
                            except Exception as e:
                                print(f"[reflection] page absorb(update) #{target_id} failed: {e}")

                elif action == "merge" and target_id:
                    ok = self.fragment_store.bump_observation(target_id, new_source_id=fid)
                    if ok:
                        report["merge"] += 1
                        print(f"[reflection] observation #{target_id} evidence++ by #{fid}")

                elif action == "contradict" and target_id:
                    ok = self.fragment_store.mark_contradiction(target_id, fid)
                    if ok:
                        report["contradict"] += 1
                        print(f"[reflection] contradiction #{target_id} <-> #{fid}")
                else:
                    report["skipped"] += 1

                # 5. 碎片标记已处理
                try:
                    conn = sqlite3.connect(self.db_path, timeout=30)
                    conn.execute("PRAGMA busy_timeout=30000")
                    conn.execute("UPDATE memory_fragments SET dirty=0 WHERE id=?", (fid,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            except Exception as e:
                print(f"[reflection] consolidate #{fid} failed: {e}")
                report["skipped"] += 1

        report["pages"] = page_report
        return report

    def _count_page_action(self, page_report: dict, res: dict) -> None:
        """统计知识页归组结果。"""
        if not res:
            return
        action = res.get("action")
        if action in page_report:
            page_report[action] += 1

    def _parse_obs_decision(self, resp: str) -> dict:
        """解析 LLM 四动作裁决输出（容错：去 markdown 代码块、找 JSON）。"""
        import re
        if not resp:
            return {}
        # 去 ```json ... ``` 包裹
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", resp, re.S)
        if m:
            resp = m.group(1)
        else:
            # 直接找第一个 { 到最后一个 }
            s, e = resp.find("{"), resp.rfind("}")
            if s >= 0 and e > s:
                resp = resp[s:e + 1]
        try:
            return json.loads(resp)
        except Exception:
            return {}

    def _link_story_to_topic_chain(self, new_story_id: int, topic_id: str) -> int:
        """把新建 story 接到同 topic_id 的故事链尾部。

        找同 topic_id 下最新的那条 story（非自己），建 next_in_topic 边。
        这样同主题故事按创建顺序自然形成链：
            story#1 →(next_in_topic)→ story#2 →(next_in_topic)→ story#3
        """
        try:
            chain = self.fragment_store.get_story_chain(topic_id=topic_id, limit=50)
            # 排除自己，取最后一条（最新）
            others = [s for s in chain if s["id"] != new_story_id]
            if not others:
                return 0
            # 按 created_at 排序，找最后一条
            others.sort(key=lambda x: x.get("created_at", 0))
            prev_story = others[-1]
            self.fragment_store.link_stories(
                from_story_id=prev_story["id"],
                to_story_id=new_story_id,
                link_type="next_in_topic",
                weight=0.9
            )
            return 1
        except Exception as e:
            print(f"[reflection] link_story_to_topic_chain failed: {e}")
            return 0

    def build_story_weak_assocs(self) -> int:
        """跨主题故事弱关联（P6 故事网络）。

        两条策略，先实体后相似度：
        1. 实体共现：两个故事提到 ≥1 个相同实体 → 建 similar_to 边（confidence=0.8）
        2. 相似度兜底：embedding 相似度 ≥0.70 → 建 similar_to 边（confidence=0.5）

        已有边不重复建（memory_links PRIMARY KEY (from_id, to_id)）。
        跨主题的故事由此连成网，AI 召回一个故事时能自然延伸到相关故事。
        """
        import sqlite3
        from memory.embedder import cosine_sim

        # 取所有 story
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            rows = conn.execute(
                "SELECT id, text, embedding, tags, topic_id FROM memory_fragments "
                "WHERE layer='story' AND dirty=1 ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 2:
            return 0

        # 解析 embedding
        stories = []
        for r in rows:
            try:
                emb = json.loads(r["embedding"])
            except Exception:
                emb = []
            stories.append({
                "id": r["id"], "text": r["text"] or "", "emb": emb,
                "tags": r["tags"] or "", "topic_id": r["topic_id"]
            })

        # ── 策略1：实体共现 ──
        # 取所有实体名，在 story 文本里匹配
        entity_names = []
        if self.entity_store:
            try:
                ents = self.entity_store.list_all(limit=200)
                entity_names = [e.get("name", "").strip() for e in ents if e.get("name")]
            except Exception:
                pass

        count = 0
        entity_pairs = set()
        if entity_names:
            # 给每个 story 标记它提到的实体
            story_entities = {}
            for s in stories:
                text_lower = s["text"].lower()
                hits = set()
                for name in entity_names:
                    if name and name.lower() in text_lower:
                        hits.add(name)
                story_entities[s["id"]] = hits

            # 两两查共现实体
            for i in range(len(stories)):
                for j in range(i + 1, len(stories)):
                    if count >= STORY_WEAK_ASSOC_MAX_PER_RUN:
                        break
                    a, b = stories[i], stories[j]
                    # 同 topic_id 的已经用 next_in_topic 连过了，跳过
                    if a["topic_id"] and b["topic_id"] and a["topic_id"] == b["topic_id"]:
                        continue
                    shared = story_entities[a["id"]] & story_entities[b["id"]]
                    if len(shared) >= STORY_ENTITY_COOCURRENCE_MIN:
                        pair_key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                        if pair_key in entity_pairs:
                            continue
                        reason = f"实体共现: {','.join(list(shared)[:3])}"
                        try:
                            self.fragment_store.link_stories(
                                from_story_id=a["id"], to_story_id=b["id"],
                                link_type="similar_to", weight=0.8
                            )
                            # 也存到 relation_store，带 reason 便于审计
                            self.relation_store.add(
                                subject_id=a["id"], predicate="similar_to",
                                object_id=b["id"], edge_type="story_weak_assoc",
                                confidence=0.8, source_fragment_id=a["id"],
                                reason=reason
                            )
                            entity_pairs.add(pair_key)
                            count += 1
                        except Exception:
                            pass
                if count >= STORY_WEAK_ASSOC_MAX_PER_RUN:
                    break

        if count >= STORY_WEAK_ASSOC_MAX_PER_RUN:
            return count

        # ── 策略2：相似度兜底 ──
        for i in range(len(stories)):
            for j in range(i + 1, len(stories)):
                if count >= STORY_WEAK_ASSOC_MAX_PER_RUN:
                    break
                a, b = stories[i], stories[j]
                if not a["emb"] or not b["emb"]:
                    continue
                # 同 topic_id 跳过
                if a["topic_id"] and b["topic_id"] and a["topic_id"] == b["topic_id"]:
                    continue
                # 已经建过实体共现边的跳过
                pair_key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if pair_key in entity_pairs:
                    continue

                sim = cosine_sim(a["emb"], b["emb"])
                if sim >= STORY_WEAK_ASSOC_SIM_THRESHOLD:
                    try:
                        self.fragment_store.link_stories(
                            from_story_id=a["id"], to_story_id=b["id"],
                            link_type="similar_to", weight=0.5
                        )
                        self.relation_store.add(
                            subject_id=a["id"], predicate="similar_to",
                            object_id=b["id"], edge_type="story_weak_assoc",
                            confidence=0.5, source_fragment_id=a["id"],
                            reason=f"embedding 相似度 {sim:.3f}"
                        )
                        count += 1
                    except Exception:
                        pass
            if count >= STORY_WEAK_ASSOC_MAX_PER_RUN:
                break

        return count

    # ── 职责7：从对话提取 AI 对文件的真实思考 ────────────

    FILE_THOUGHT_LOOKBACK_HOURS = 24  # 只看最近 24 小时的对话
    FILE_THOUGHT_MAX_PER_RUN = 10     # 每次反思最多提取 10 条文件思考

    def extract_file_thoughts(self) -> int:
        """从对话历史提取 AI 对文件的真实思考。

        哲学：晶体必须经过 AI 脑子。
        - 不机械压缩文件内容
        - 扫描 AI 调 read_file 后的对话，提取 AI 真实讨论/结论
        - 存为自传晶体，raw_source_id 指向文件切片

        策略：
        1. 扫 messages 表找最近 read_file 工具调用（短事务，查完就关 conn）
        2. 取该工具调用后的 AI 文本回复
        3. 用 LLM 提取"AI 对这个文件得出了什么结论"（LLM 调用期间不持 conn）
        4. 存为自传晶体，raw_source 指向文件第一个切片
        """
        import sqlite3
        import json as _json

        if not self.llm_call_fn:
            return 0  # 没 LLM 就跳过（机械提取质量太差）

        # ── 阶段 1：查数据（短事务，查完关 conn）──
        file_discussions = []
        existing_sources = set()

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            cutoff = time.time() - self.FILE_THOUGHT_LOOKBACK_HOURS * 3600
            rows = conn.execute(
                """SELECT id, topic_id, text, args, ts
                   FROM messages
                   WHERE role='ai' AND args IS NOT NULL
                   AND args LIKE '%read_file%'
                   AND ts >= ?
                   ORDER BY ts DESC LIMIT 50""",
                (cutoff,)
            ).fetchall()

            for r in rows:
                try:
                    args_data = _json.loads(r["args"]) if r["args"] else {}
                except Exception:
                    continue
                tool_calls = args_data.get("tool_calls", []) if isinstance(args_data, dict) else []

                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    if fn.get("name") != "read_file":
                        continue
                    try:
                        tc_args = _json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        continue
                    file_path = tc_args.get("path")
                    if not file_path:
                        continue

                    next_ai = conn.execute(
                        """SELECT text, ts FROM messages
                           WHERE topic_id=? AND role='ai' AND id > ?
                           AND text IS NOT NULL AND text != ''
                           ORDER BY id ASC LIMIT 1""",
                        (r["topic_id"], r["id"])
                    ).fetchone()

                    if next_ai and next_ai["text"]:
                        ai_text = next_ai["text"][:2000]
                        if len(ai_text) > 50:
                            file_discussions.append({
                                "path": file_path,
                                "ai_text": ai_text,
                                "topic_id": r["topic_id"],
                                "ts": next_ai["ts"],
                            })

            # 预查已有 raw_source_id（避免后续逐条查）
            existing_rows = conn.execute(
                "SELECT DISTINCT raw_source_id FROM memory_fragments "
                "WHERE raw_source_id IS NOT NULL AND node_type='self'"
            ).fetchall()
            existing_sources = {r["raw_source_id"] for r in existing_rows}
        finally:
            conn.close()  # ← 关键：查完就关，不持 conn 调 LLM

        if not file_discussions:
            return 0

        # 去重：同一路径只保留最近一次讨论
        seen_paths = {}
        for d in file_discussions:
            seen_paths[d["path"]] = d
        file_discussions = list(seen_paths.values())[:self.FILE_THOUGHT_MAX_PER_RUN]

        # ── 阶段 2：查文件切片 id（独立短事务）──
        tasks = []  # [{slice_id, path, ai_text}]
        for d in file_discussions:
            try:
                from memory.file_crystal_store import get_store as get_fc_store
                fc_store = get_fc_store()
                slices = fc_store.get_by_path(d["path"], layer=0)
                if not slices:
                    continue
                slice_id = slices[0]["id"]
                if slice_id in existing_sources:
                    continue  # 已有同 raw_source 的晶体，跳过
                tasks.append({"slice_id": slice_id, "path": d["path"], "ai_text": d["ai_text"]})
            except Exception:
                continue

        if not tasks:
            return 0

        # ── 阶段 3：调 LLM + 写库（不持长连接）──
        count = 0
        for t in tasks:
            prompt = (
                f"以下是 AI 阅读文件 {t['path']} 后的回复：\n\n{t['ai_text']}\n\n"
                "提取 AI 对这个文件得出的核心结论（不超过100字，用第一人称）。"
                "只提取真实结论，如果只是泛泛而谈没有具体结论就回复'无有效结论'。"
            )
            try:
                thought = self.llm_call_fn(prompt, "")
            except Exception:
                thought = ""

            if not thought or "无有效结论" in thought or len(thought) < 10:
                continue

            try:
                meta_id = self.fragment_store.add(
                    thought.strip(),
                    source="reflection_file_thought",
                    tags="file_thought,emerged",
                    importance=6.0,
                    epistemic="experience",
                    node_type="self",
                    crystal_parent_id=None,
                    raw_source_id=t["slice_id"],
                )
                if meta_id:
                    count += 1
            except Exception as e:
                print(f"[reflection_loop] file_thought 写入失败: {e}")
                continue

        return count

    # ── 前置：回填实体 ────────────────────────────────

    # 英文停用词（不作为实体）
    _STOPWORDS = frozenset({
        "the", "and", "for", "not", "but", "you", "all", "any", "can", "had",
        "her", "was", "one", "our", "out", "are", "has", "have", "this", "that",
        "with", "from", "they", "will", "would", "there", "their", "what",
        "about", "which", "when", "make", "could", "than", "them", "been",
        "want", "very", "just", "like", "need", "know", "think", "sure",
        "true", "false", "none", "null", "true", "self", "this", "true",
    })

    def backfill_entities(self, batch_size: int = 50) -> int:
        """扫描没有 entity_ids 的记忆碎片，用正则提取实体写入 entities 表。

        策略：提取英文技术标识符（含下划线或>=4字符全小写）和中文技术名词。
        每条记忆最多提取 5 个实体，避免噪声。
        已存在的实体只更新 mention_count，不重新 embed（避免 ONNX 推理开销）。
        """
        import re
        import sqlite3
        if not self.entity_store:
            return 0

        # 英文标识符正则：含下划线 或 长度>=4 或 含大写字母（技术词特征）
        en_pat = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]{2,}\b')

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            # 找 entity_ids 为 NULL 或空的 fragments
            rows = conn.execute(
                """SELECT id, text FROM memory_fragments
                   WHERE dirty=1 AND (entity_ids IS NULL OR entity_ids = '' OR entity_ids = '[]')
                   ORDER BY id DESC LIMIT ?""",
                (batch_size,)
            ).fetchall()
            if not rows:
                return 0

            now = time.time()
            count = 0
            new_entity_count = 0

            for r in rows:
                text = r["text"] or ""
                entities_found = []

                # 提取英文技术词
                for m in en_pat.findall(text):
                    lower = m.lower()
                    if lower in self._STOPWORDS:
                        continue
                    # 至少满足一个技术词特征：含下划线、全大写、或长度>=5
                    if "_" in m or m.isupper() or len(m) >= 5:
                        if m not in entities_found:
                            entities_found.append(m)
                    if len(entities_found) >= 5:
                        break

                if not entities_found:
                    continue

                # 批量查已存在的实体（避免逐个 upsert 的 ONNX 开销）
                entity_ids = []
                for name in entities_found:
                    norm = name.strip().lower()
                    existing = conn.execute(
                        "SELECT id FROM memory_entities WHERE name=? AND type='tech'",
                        (norm,)
                    ).fetchone()
                    if existing:
                        # 已存在：只更新 mention_count 和 last_seen，不 re-embed
                        conn.execute(
                            "UPDATE memory_entities SET mention_count=mention_count+1, last_seen=? WHERE id=?",
                            (now, existing["id"])
                        )
                        entity_ids.append(existing["id"])
                    else:
                        # 新实体：直接 SQL 插入（不 embed，避免 ONNX 推理开销）
                        # embedding 留空，后续需要实体搜索时再补
                        cur = conn.execute(
                            """INSERT INTO memory_entities
                               (name, type, aliases, first_seen, last_seen, mention_count, embedding, status)
                               VALUES (?, 'tech', '[]', ?, ?, 1, '[]', 'active')""",
                            (norm, now, now)
                        )
                        entity_ids.append(cur.lastrowid)

                if entity_ids:
                    conn.execute(
                        "UPDATE memory_fragments SET entity_ids=? WHERE id=?",
                        (json.dumps(entity_ids), r["id"])
                    )
                    count += 1

            conn.commit()
            return count
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


def _avg_pairwise_cosine(embs: list) -> float:
    """群内平均两两余弦相似度。"""
    if len(embs) < 2:
        return 0.0
    total = 0.0
    count = 0
    for i in range(len(embs)):
        for j in range(i + 1, len(embs)):
            total += _cosine(embs[i], embs[j])
            count += 1
    return total / count if count > 0 else 0.0


# ── Singleton ──────────────────────────────────────────

_loop = None


def _default_llm_call(prompt: str, user: str = "") -> str:
    """默认 LLM 调用：复用 memory/reflection 的 _llm_prompt。"""
    try:
        from memory.reflection import _llm_prompt
        return _llm_prompt("你是一个自我反思的AI助手", prompt)
    except Exception as e:
        print(f"[reflection_loop] LLM 调用失败: {e}")
        return ""


def get_loop():
    global _loop
    if _loop is None:
        _loop = ReflectionLoop(llm_call_fn=_default_llm_call)
    return _loop
