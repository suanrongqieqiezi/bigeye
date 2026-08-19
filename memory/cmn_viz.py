"""
CMN 可视化数据构建 — 为 /api/cmn/viz/* 路由提供 X6 格式的图数据。

三个视图：
  1. build_network_graph  — 晶体网络（self + file 节点 + 派生/金字塔/链接边）
  2. build_relations_graph — 实体关系图（entities + memory_relations）
  3. get_pyramid_tree      — 金字塔树形（委托给 FileCrystalStore.get_pyramid）
  4. list_indexed_files    — 列出所有已建晶体的文件路径

返回格式统一为 X6 友好：{nodes:[{id,label,...}], edges:[{source,target,...}]}
"""
import os
import sys
import sqlite3


def _get_db_path():
    MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(MEMORY_DIR)
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "data", "chat.db")
    return os.path.join(ROOT_DIR, "data", "chat.db")


# ── 视图 1：晶体网络图 ────────────────────────────────────

def build_network_graph(limit: int = 100, include_files: bool = True,
                        include_self: bool = True) -> dict:
    """构建晶体网络图（X6 格式）。

    节点类型：
      - file: 文件晶体（file_crystals）
      - self: 自传晶体（memory_fragments WHERE node_type='self'）

    边类型：
      - pyramid: 文件金字塔父子（crystal_parent_id）
      - derive:  自传晶体派生自文件晶体（crystal_parent_id）
      - link:    碎片间链接（memory_links）
    """
    conn = sqlite3.connect(_get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        nodes = []
        edges = []
        node_ids = set()

        # 1. 文件晶体节点
        if include_files:
            rows = conn.execute(
                "SELECT id, source_path, slice_index, layer, summary, status, "
                "authority_level, source_type "
                "FROM file_crystals WHERE status='active' "
                "ORDER BY layer DESC, slice_index LIMIT ?",
                (limit,)
            ).fetchall()
            for r in rows:
                nid = r["id"]
                label = f"{nid[:14]}\nL{r['layer']}#{r['slice_index']}"
                if r["source_type"] == "knowledge_base":
                    label += " ★"
                nodes.append({
                    "id": nid,
                    "type": "file",
                    "label": label,
                    "layer": r["layer"],
                    "status": r["status"],
                    "authority": r["authority_level"],
                    "source_type": r["source_type"],
                    "source_path": r["source_path"],
                    "summary": (r["summary"] or "")[:120],
                })
                node_ids.add(nid)

        # 2. 自传晶体节点
        if include_self:
            rows = conn.execute(
                "SELECT id, text, ts, epistemic, authority_level, "
                "crystal_parent_id, node_type, importance "
                "FROM memory_fragments WHERE dirty=1 AND node_type='self' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            for r in rows:
                nid = f"frag_{r['id']}"
                text = (r["text"] or "").replace("\n", " ")
                label = text[:32] + ("…" if len(text) > 32 else "")
                if r["authority_level"]:
                    label += " ★"
                nodes.append({
                    "id": nid,
                    "type": "self",
                    "label": label,
                    "ts": r["ts"],
                    "epistemic": r["epistemic"],
                    "authority": r["authority_level"],
                    "importance": r["importance"],
                    "text": text[:200],
                })
                node_ids.add(nid)

        # 3. 边：文件金字塔父子
        if include_files:
            rows = conn.execute(
                "SELECT id, crystal_parent_id FROM file_crystals "
                "WHERE status='active' AND crystal_parent_id IS NOT NULL"
            ).fetchall()
            for r in rows:
                if r["id"] in node_ids and r["crystal_parent_id"] in node_ids:
                    edges.append({
                        "source": r["crystal_parent_id"],
                        "target": r["id"],
                        "type": "pyramid",
                    })

        # 4. 边：自传→文件派生
        if include_self and include_files:
            rows = conn.execute(
                "SELECT id, crystal_parent_id FROM memory_fragments "
                "WHERE dirty=1 AND node_type='self' AND crystal_parent_id IS NOT NULL"
            ).fetchall()
            for r in rows:
                fid = f"frag_{r['id']}"
                if fid in node_ids and r["crystal_parent_id"] in node_ids:
                    edges.append({
                        "source": r["crystal_parent_id"],
                        "target": fid,
                        "type": "derive",
                    })

        # 5. 边：碎片间链接 memory_links
        if include_self:
            rows = conn.execute(
                """SELECT from_id, to_id, link_type, weight FROM memory_links
                   WHERE from_id IN (
                       SELECT id FROM memory_fragments
                       WHERE dirty=1 AND node_type='self'
                   ) AND to_id IN (
                       SELECT id FROM memory_fragments
                       WHERE dirty=1 AND node_type='self'
                   )"""
            ).fetchall()
            for r in rows:
                src = f"frag_{r['from_id']}"
                tgt = f"frag_{r['to_id']}"
                if src in node_ids and tgt in node_ids:
                    edges.append({
                        "source": src,
                        "target": tgt,
                        "type": r["link_type"] or "link",
                        "weight": r["weight"],
                    })

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "file_nodes": sum(1 for n in nodes if n["type"] == "file"),
                "self_nodes": sum(1 for n in nodes if n["type"] == "self"),
                "edges": len(edges),
            },
        }
    finally:
        conn.close()


# ── 视图 2：实体关系图 ────────────────────────────────────

EDGE_TYPE_COLORS = {
    "fact": "#6b7280",
    "causal": "#ef4444",
    "temporal": "#3b82f6",
    "derive": "#10b981",
    "support": "#22c55e",
    "negate": "#dc2626",
    "weak_assoc": "#a855f7",
}


def build_relations_graph(limit: int = 200, edge_type: str = None) -> dict:
    """构建实体关系图（X6 格式）。

    节点：memory_entities（person/concept/etc.）
    边：  memory_relations（subject → object，带 predicate/edge_type）
    """
    conn = sqlite3.connect(_get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        where = "r.status='active'"
        params = []
        if edge_type:
            where += " AND r.edge_type=?"
            params.append(edge_type)

        rows = conn.execute(
            f"""SELECT r.id, r.subject_id, r.predicate, r.object_id, r.object_value,
                       r.edge_type, r.confidence, r.reason,
                       s.name as subject_name, s.type as subject_type,
                       s.mention_count as subject_mentions,
                       o.name as object_name, o.type as object_type,
                       o.mention_count as object_mentions
                FROM memory_relations r
                LEFT JOIN memory_entities s ON r.subject_id = s.id
                LEFT JOIN memory_entities o ON r.object_id = o.id
                WHERE {where}
                ORDER BY r.valid_from DESC LIMIT ?""",
            params + [limit]
        ).fetchall()

        nodes = []
        edges = []
        node_map = {}  # id → node dict

        def _ensure_node(node_id, name, etype, mentions, status):
            if node_id in node_map:
                return
            label = name or node_id
            n = {
                "id": node_id,
                "label": label[:20] + ("…" if len(label) > 20 else ""),
                "type": etype or "entity",
                "mention_count": mentions or 0,
                "status": status,
            }
            nodes.append(n)
            node_map[node_id] = n

        for r in rows:
            src_id = f"ent_{r['subject_id']}"
            _ensure_node(src_id, r["subject_name"], r["subject_type"],
                         r["subject_mentions"], "active")

            if r["object_id"]:
                tgt_id = f"ent_{r['object_id']}"
                _ensure_node(tgt_id, r["object_name"], r["object_type"],
                             r["object_mentions"], "active")
            elif r["object_value"]:
                # 字面量值 → 虚拟节点
                tgt_id = f"val_{r['id']}"
                val = r["object_value"]
                if val not in node_map:
                    n = {
                        "id": tgt_id,
                        "label": val[:24] + ("…" if len(val) > 24 else ""),
                        "type": "value",
                        "mention_count": 0,
                        "status": "literal",
                    }
                    nodes.append(n)
                    node_map[val] = n
                    node_map[tgt_id] = n
            else:
                continue

            edges.append({
                "source": src_id,
                "target": tgt_id,
                "label": r["predicate"],
                "type": r["edge_type"] or "fact",
                "confidence": r["confidence"],
                "reason": r["reason"],
                "color": EDGE_TYPE_COLORS.get(r["edge_type"], "#6b7280"),
            })

        return {
            "nodes": nodes,
            "edges": edges,
            "edge_type_colors": EDGE_TYPE_COLORS,
            "stats": {
                "entities": sum(1 for n in nodes if n["type"] != "value"),
                "literals": sum(1 for n in nodes if n["type"] == "value"),
                "edges": len(edges),
            },
        }
    finally:
        conn.close()


# ── 视图 3：金字塔树形 ────────────────────────────────────

def get_pyramid_tree(path: str) -> dict:
    """委托给 FileCrystalStore.get_pyramid。"""
    from memory.file_crystal_store import get_store
    return get_store().get_pyramid(path)


def pyramid_to_x6(pyramid: dict) -> dict:
    """把 get_pyramid 的树形结构转成 X6 格式。"""
    nodes = []
    edges = []

    def _walk(node, parent_id=None):
        c = node.get("crystal", node)  # 兼容两种结构
        nid = c["id"]
        label = f"{nid[:14]}\nL{c.get('layer', 0)}#{c.get('slice_index', 0)}"
        if c.get("authority_level"):
            label += " ★"
        nodes.append({
            "id": nid,
            "label": label,
            "type": "file",
            "layer": c.get("layer", 0),
            "slice_index": c.get("slice_index", 0),
            "authority": c.get("authority_level", 0),
            "status": c.get("status", "active"),
            "summary": (c.get("summary") or "")[:120],
            "content_preview": (c.get("content") or "")[:80],
        })
        if parent_id:
            edges.append({
                "source": parent_id,
                "target": nid,
                "type": "pyramid",
            })
        for child in node.get("children", []):
            _walk(child, nid)

    for top in pyramid.get("tree", []):
        _walk(top)

    return {
        "nodes": nodes,
        "edges": edges,
        "path": pyramid.get("path"),
        "top_layer": pyramid.get("top_layer", 0),
        "total_crystals": pyramid.get("total_crystals", len(nodes)),
    }


# ── 辅助：列出已建晶体的文件 ──────────────────────────────

def list_indexed_files() -> list[dict]:
    """列出所有已建晶体的文件路径及统计。"""
    conn = sqlite3.connect(_get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        rows = conn.execute(
            """SELECT source_path, source_type,
                      COUNT(*) as crystals_count,
                      MAX(layer) as top_layer,
                      SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) as active_count,
                      SUM(CASE WHEN status='stale' THEN 1 ELSE 0 END) as stale_count,
                      MAX(updated_at) as last_updated
               FROM file_crystals
               GROUP BY source_path
               ORDER BY MAX(updated_at) DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
