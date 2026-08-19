#!/usr/bin/env python3
"""Mindmap tools — AI 读写思维导图（与 DAG 工具对齐的写通道）。

思维导图数据存于 data/missions/{topic_id}/mindmap.json，单文件 JSON。
大眼通过这些工具能在对话中直接操作思维导图，与用户协同思考。

工具清单：
    get_mindmap        — 查看全图（节点/边/统计）
    add_mindmap_node   — 添加节点（可选连线到现有节点）
    update_mindmap_node— 改节点文本/类型/备注
    add_mindmap_edge   — 连线
    remove_mindmap_node— 删节点（连带删边）
    remove_mindmap_edge— 删连线
    update_mindmap_edge— 改连线标签/颜色/线型
    link_mindmap_to_dag— 把节点关联到 DAG 任务节点（task_ref）
    mindmap_history    — 列出最近改动历史（git log 风格）
    mindmap_undo       — 撤回 N 步，返回新状态 + diff（让 AI 预览变化）
    mindmap_redo       — 重做 N 步，返回新状态 + diff
"""
import json
import os
import sys
import time
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from .registry import register_tool


# ── 思维导图历史记录（git 风格版本控制）──
# 每次保存自动记一条快照，支持 undo/redo 和跳转，AI 可调用查看 diff。
_MM_HISTORY_LIMIT = 1000  # 每话题最多保留 1000 条历史

def _mm_history_db():
    """获取数据库连接（每线程独立）"""
    from db import get_db
    return get_db().conn

def _mm_history_ensure_schema():
    """建表（幂等）"""
    try:
        conn = _mm_history_db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS mindmap_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id    TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            snapshot    TEXT NOT NULL,
            action      TEXT,
            source      TEXT,
            created_at  REAL NOT NULL,
            UNIQUE(topic_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_mmh_topic ON mindmap_history(topic_id, seq);
        """)
        conn.commit()
    except Exception as e:
        print(f"[mindmap_history] ensure schema failed: {e}", file=sys.stderr)

# 模块加载时建表
_mm_history_ensure_schema()


def _mm_compute_diff(old_mm, new_mm):
    """对比两个快照，返回 diff 摘要（让 AI/用户一眼看出变化）。"""
    old_nodes = {n["id"]: n for n in (old_mm.get("nodes") or [])}
    new_nodes = {n["id"]: n for n in (new_mm.get("nodes") or [])}
    old_edges = {(e["source"], e["target"]): e for e in (old_mm.get("edges") or [])}
    new_edges = {(e["source"], e["target"]): e for e in (new_mm.get("edges") or [])}

    added_nodes = sorted(set(new_nodes) - set(old_nodes))
    removed_nodes = sorted(set(old_nodes) - set(new_nodes))
    modified_nodes = []
    for nid in set(old_nodes) & set(new_nodes):
        o, n = old_nodes[nid], new_nodes[nid]
        fields = []
        for f in ("text", "type", "note"):
            if (o.get(f) or "") != (n.get(f) or ""):
                fields.append({"field": f, "from": (o.get(f) or "")[:40], "to": (n.get(f) or "")[:40]})
        if fields:
            modified_nodes.append({"id": nid, "text": n.get("text", "")[:30], "fields": fields})

    added_edges = sorted([f"{s}->{t}" for (s, t) in set(new_edges) - set(old_edges)])
    removed_edges = sorted([f"{s}->{t}" for (s, t) in set(old_edges) - set(new_edges)])

    return {
        "added_nodes": added_nodes[:20],
        "removed_nodes": removed_nodes[:20],
        "modified_nodes": modified_nodes[:20],
        "added_edges": added_edges[:20],
        "removed_edges": removed_edges[:20],
        "summary": f"+{len(added_nodes)}节点 -{len(removed_nodes)}节点 ~{len(modified_nodes)}节点改 +{len(added_edges)}边 -{len(removed_edges)}边"
    }


def _mm_history_get_current_seq(topic_id):
    """获取当前指针 seq。存在 meta 表，键 mm_current_seq_{topic_id}。"""
    try:
        from db import get_db
        db = get_db()
        v = db.get_topic_meta(topic_id, "mm_current_seq")
        return int(v) if v else 0
    except Exception:
        return 0


def _mm_history_set_current_seq(topic_id, seq):
    """更新当前指针"""
    try:
        from db import get_db
        db = get_db()
        db.set_topic_meta(topic_id, "mm_current_seq", str(seq))
    except Exception as e:
        print(f"[mindmap_history] set current seq failed: {e}", file=sys.stderr)


def _mm_history_record(topic_id, mm_data, action="edit", source="user"):
    """记一条历史。在 _save_mindmap 内部调用。
    - 新改动会丢弃 current_seq 之后的所有记录（git 风格：分叉时丢弃未来）
    - 超出上限删最旧的
    """
    try:
        conn = _mm_history_db()
        cur_seq = _mm_history_get_current_seq(topic_id)
        # 丢弃 current_seq 之后的记录（如果 undo 过，再改动会覆盖 redo 栈）
        conn.execute(
            "DELETE FROM mindmap_history WHERE topic_id=? AND seq > ?",
            (topic_id, cur_seq)
        )
        new_seq = cur_seq + 1
        snapshot = json.dumps(mm_data, ensure_ascii=False)
        conn.execute("""
            INSERT INTO mindmap_history(topic_id, seq, snapshot, action, source, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
        """, (topic_id, new_seq, snapshot, action, source, time.time()))
        _mm_history_set_current_seq(topic_id, new_seq)

        # 清理超出上限的旧记录
        conn.execute("""
            DELETE FROM mindmap_history
            WHERE topic_id=? AND seq <= (
                SELECT COALESCE(MAX(seq), 0) - ? FROM mindmap_history WHERE topic_id=?
            )
        """, (topic_id, _MM_HISTORY_LIMIT, topic_id))
        conn.commit()
        return new_seq
    except Exception as e:
        print(f"[mindmap_history] record failed: {e}", file=sys.stderr)
        return 0


def _mm_history_get_snapshot(topic_id, seq):
    """获取指定 seq 的快照"""
    try:
        conn = _mm_history_db()
        row = conn.execute(
            "SELECT snapshot FROM mindmap_history WHERE topic_id=? AND seq=?",
            (topic_id, seq)
        ).fetchone()
        if row:
            return json.loads(row[0])
        return None
    except Exception:
        return None


def _mm_history_list(topic_id, limit=20):
    """列出最近 N 条历史（含与上一版的 diff 摘要）"""
    try:
        conn = _mm_history_db()
        cur_seq = _mm_history_get_current_seq(topic_id)
        rows = conn.execute("""
            SELECT seq, action, source, created_at FROM mindmap_history
            WHERE topic_id=? ORDER BY seq DESC LIMIT ?
        """, (topic_id, limit)).fetchall()
        result = []
        for r in rows:
            seq, action, source, ts = r
            result.append({
                "seq": seq,
                "action": action,
                "source": source,
                "time": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
                "is_current": seq == cur_seq,
            })
        return {
            "current_seq": cur_seq,
            "history": result,
            "can_undo": cur_seq > 1,
            "can_redo": len(rows) > 0 and rows[0][0] > cur_seq,
        }
    except Exception as e:
        return {"error": str(e)}


def _mm_history_goto(topic_id, target_seq, source="user"):
    """跳到指定 seq。返回新状态 + diff + 快照数据 + 按钮可用性（一次返回所有前端需要的数据）"""
    try:
        conn = _mm_history_db()
        # 检查 target_seq 是否存在
        row = conn.execute(
            "SELECT seq FROM mindmap_history WHERE topic_id=? AND seq=?",
            (topic_id, target_seq)
        ).fetchone()
        if not row:
            return {"error": f"seq {target_seq} 不存在"}

        cur_seq = _mm_history_get_current_seq(topic_id)
        old_mm = _mm_history_get_snapshot(topic_id, cur_seq) or _load_mindmap(topic_id)
        new_mm = _mm_history_get_snapshot(topic_id, target_seq)
        if not new_mm:
            return {"error": "快照读取失败"}

        diff = _mm_compute_diff(old_mm, new_mm)
        _mm_history_set_current_seq(topic_id, target_seq)

        # 同步写回 mindmap.json（让前端能拉到）
        _save_mindmap_raw(topic_id, new_mm)

        # 查询按钮可用性（避免前端再发一次请求）
        max_row = conn.execute(
            "SELECT MAX(seq) FROM mindmap_history WHERE topic_id=?", (topic_id,)
        ).fetchone()
        max_seq = max_row[0] if max_row and max_row[0] else 0

        return {
            "ok": True,
            "from_seq": cur_seq,
            "to_seq": target_seq,
            "diff": diff,
            "mindmap": new_mm,                    # 直接返回快照，前端不用再 GET
            "node_count": len(new_mm.get("nodes") or []),
            "edge_count": len(new_mm.get("edges") or []),
            "can_undo": target_seq > 1,            # 按钮状态，前端不用再查 history
            "can_redo": target_seq < max_seq,
            "current_seq": target_seq,
        }
    except Exception as e:
        return {"error": str(e)}


def _save_mindmap_raw(topic_id, mm_data):
    """只写文件，不记历史（undo/redo 跳转时用，避免循环记历史）"""
    p = _mm_path(topic_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mm_data["updated_at"] = int(time.time())
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mm_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return mm_data


def _get_active_topic_id():
    """获取当前活跃话题 ID（与 task_v4_tools 一致）"""
    try:
        from db import get_db
        db = get_db()
        return db.get_active_topic_id()
    except Exception:
        return None


def _mm_path(topic_id):
    """思维导图文件路径。用完整 topic_id（与前端 API / DAG 存储一致）"""
    return os.path.join(BASE_DIR, "data", "missions", topic_id, "mindmap.json")


def _load_mindmap(topic_id):
    """读思维导图。不存在返回空结构。"""
    p = _mm_path(topic_id)
    if not os.path.isfile(p):
        return {
            "schema": 1,
            "topic_id": topic_id,
            "title": "思维导图",
            "updated_at": 0,
            "nodes": [],
            "edges": [],
        }
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return {
            "schema": 1,
            "topic_id": topic_id,
            "title": "思维导图",
            "updated_at": 0,
            "nodes": [],
            "edges": [],
        }


def _save_mindmap(topic_id, mm_data, action="edit", source="user"):
    """原子写 + 记历史。所有写操作的统一入口。
    action/source 用于历史记录，标识这次改动的来源。
    """
    # 写文件
    _save_mindmap_raw(topic_id, mm_data)
    # 记历史
    _mm_history_record(topic_id, mm_data, action=action, source=source)
    return mm_data


def _new_id(prefix="m"):
    return prefix + "_" + uuid.uuid4().hex[:10]


def _node_summary(n):
    """节点摘要（用于返回）"""
    return {
        "id": n.get("id"),
        "text": n.get("text", "")[:40],
        "type": n.get("type", "idea"),
        "note": (n.get("note") or "")[:60],
    }


# 节点宽高（与前端 MM_NODE_W 对齐，用于碰撞检测）
_MM_NODE_W = 200
_MM_NODE_H = 50


def _find_free_slot(base_x, base_y, existing_nodes, step=60):
    """从 (base_x, base_y) 开始螺旋搜索空位，避免与现有节点重叠。
    简化策略：先尝试同一行右侧，再换行下移。前端 dagre 会统一重排，这里只需不重叠即可。
    """
    if not existing_nodes:
        return base_x, base_y
    # 收集已占用 bbox
    occupied = []
    for n in existing_nodes:
        nx = n.get("x", 0)
        ny = n.get("y", 0)
        occupied.append((nx, ny, nx + _MM_NODE_W, ny + _MM_NODE_H))
    # 螺旋搜索：行优先，每行试 5 个位置，最多 20 行
    for row in range(20):
        for col in range(5):
            x = base_x + col * step
            y = base_y + row * step
            bbox = (x, y, x + _MM_NODE_W, y + _MM_NODE_H)
            # 碰撞检测：任意角点在已占用框内即冲突
            clash = any(
                bbox[0] < ox2 and bbox[2] > ox1 and bbox[1] < oy2 and bbox[3] > oy1
                for ox1, oy1, ox2, oy2 in occupied
            )
            if not clash:
                return x, y
    # 兜底：直接返回 base（让前端 dagre 处理）
    return base_x, base_y


# ══════════════════════════════════════════════════════
# 工具实现
# ══════════════════════════════════════════════════════

@register_tool(
    name="get_mindmap",
    description="查看思维导图。无参自动找当前话题，返回节点列表、边列表、统计。",
    parameters={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选，不传则用当前活跃话题）",
            },
        },
        "required": [],
    },
)
def get_mindmap(topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。请先在主聊天框选定一个话题。"}
        mm = _load_mindmap(topic_id)
        nodes = mm.get("nodes") or []
        edges = mm.get("edges") or []
        type_counts = {}
        for n in nodes:
            nt = n.get("type", "idea")
            type_counts[nt] = type_counts.get(nt, 0) + 1
        # 边摘要：source_text -> target_text (label)
        id2text = {n.get("id"): n.get("text", "")[:30] for n in nodes}
        edge_brief = []
        for e in edges:
            s = id2text.get(e.get("source"), e.get("source", "")[:8])
            t = id2text.get(e.get("target"), e.get("target", "")[:8])
            lbl = e.get("label", "")
            edge_brief.append(f"{s} -> {t}" + (f" [{lbl}]" if lbl else ""))
        # 更新时间转可读格式（避免 AI 为了转时间戳又去跑 bash/python）
        updated_at = mm.get("updated_at", 0)
        updated_at_human = ""
        if updated_at:
            try:
                from datetime import datetime
                updated_at_human = datetime.fromtimestamp(int(updated_at)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                updated_at_human = ""
        return {
            "title": mm.get("title", "思维导图"),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "type_counts": type_counts,
            "nodes": [_node_summary(n) for n in nodes],
            "edges_brief": edge_brief[:40],  # 控制返回体积
            "router": mm.get("router", "cubic"),       # 全局连线样式
            "layout": mm.get("layout", "tree-h"),      # 当前布局
            "updated_at": updated_at,
            "updated_at_human": updated_at_human,        # 可读时间，省去 AI 转换
            "hint": "用 add_mindmap_node / add_mindmap_edge / update_mindmap_node / remove_mindmap_node 修改图",
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="add_mindmap_node",
    description="添加思维导图节点，可同时连线到父节点。类型：idea/decision/question。",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "节点文本（简短，≤16字最佳）",
            },
            "type": {
                "type": "string",
                "description": "节点类型",
                "enum": ["idea", "decision", "question"],
                "default": "idea",
            },
            "note": {
                "type": "string",
                "description": "备注/详情（可选）",
                "default": "",
            },
            "parent_id": {
                "type": "string",
                "description": "父节点ID（可选）。传了会自动从父节点连一条边到新节点。",
                "default": "",
            },
            "edge_label": {
                "type": "string",
                "description": "连线的标签（可选，配合 parent_id）",
                "default": "",
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选，不传则用当前活跃话题）",
            },
        },
        "required": ["text"],
    },
)
def add_mindmap_node(text: str, type: str = "idea", note: str = "",
                     parent_id: str = "", edge_label: str = "", topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。请先在主聊天框选定一个话题。"}
        if type not in ("idea", "decision", "question"):
            type = "idea"
        mm = _load_mindmap(topic_id)
        new_id = _new_id("m")
        # 坐标：在画布右下角找空位（防重叠，前端会再做 dagre 重排）
        existing = mm["nodes"]
        if parent_id:
            parent = next((n for n in existing if n.get("id") == parent_id), None)
            if not parent:
                return {"error": f"父节点 {parent_id} 不存在"}
            # 父节点右侧 260px，y 在父节点附近螺旋找空位
            base_x = parent.get("x", 100) + 260
            base_y = parent.get("y", 100)
            x, y = _find_free_slot(base_x, base_y, existing)
        else:
            # 无父节点：画布最右下角扩展
            x, y = _find_free_slot(
                (max((n.get("x", 0) for n in existing), default=100) + 260) if existing else 100,
                (max((n.get("y", 0) for n in existing), default=100)) if existing else 100,
                existing
            )
        node = {
            "id": new_id,
            "text": text.strip()[:40],
            "note": note.strip()[:500],
            "type": type,
            "x": x, "y": y,
            "color": None,
            "collapsed": False,
            "links": [],
        }
        mm["nodes"].append(node)
        result = {"node": _node_summary(node)}
        # 自动连线
        if parent_id:
            edge = {
                "id": _new_id("e"),
                "source": parent_id,
                "target": new_id,
                "label": edge_label.strip()[:20],
                "style": "solid",
                "router": None,
                "color": None,
                "dash": "solid",
            }
            mm["edges"].append(edge)
            result["edge"] = {"id": edge["id"], "source": parent_id, "target": new_id, "label": edge["label"]}
        _save_mindmap(topic_id, mm, action="add_node", source="ai")
        result["ok"] = True
        result["node_count"] = len(mm["nodes"])
        return result
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="update_mindmap_node",
    description="修改思维导图节点的文本/类型/备注。不能改 id。"
                "用于修正想法表述、升级 idea 为 decision、补充详情。",
    parameters={
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "节点ID",
            },
            "text": {
                "type": "string",
                "description": "新文本（可选，不传则不改）",
            },
            "type": {
                "type": "string",
                "description": "新类型（可选）",
                "enum": ["idea", "decision", "question"],
            },
            "note": {
                "type": "string",
                "description": "新备注（可选）",
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": ["node_id"],
    },
)
def update_mindmap_node(node_id: str, text: str = "", type: str = "",
                        note: str = "", topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        mm = _load_mindmap(topic_id)
        node = next((n for n in mm["nodes"] if n.get("id") == node_id), None)
        if not node:
            return {"error": f"节点 {node_id} 不存在"}
        if text:
            node["text"] = text.strip()[:40]
        if type and type in ("idea", "decision", "question"):
            node["type"] = type
        if note:
            node["note"] = note.strip()[:500]
        _save_mindmap(topic_id, mm, action="update_node", source="ai")
        return {"ok": True, "node": _node_summary(node)}
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="add_mindmap_edge",
    description="在思维导图两个节点之间连线。用于表达关联（无方向限制，自由联想）。"
                "source/target 都是节点 id。",
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "源节点ID",
            },
            "target": {
                "type": "string",
                "description": "目标节点ID",
            },
            "label": {
                "type": "string",
                "description": "连线标签（可选，简短说明关系）",
                "default": "",
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": ["source", "target"],
    },
)
def add_mindmap_edge(source: str, target: str, label: str = "", topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        if source == target:
            return {"error": "不能自连"}
        mm = _load_mindmap(topic_id)
        ids = {n.get("id") for n in mm["nodes"]}
        if source not in ids:
            return {"error": f"源节点 {source} 不存在"}
        if target not in ids:
            return {"error": f"目标节点 {target} 不存在"}
        # 去重（同 source-target 不重复加）
        for e in mm["edges"]:
            if e.get("source") == source and e.get("target") == target:
                return {"error": "连线已存在", "edge_id": e.get("id")}
        edge = {
            "id": _new_id("e"),
            "source": source,
            "target": target,
            "label": label.strip()[:20],
            "style": "solid",
            "router": None,
            "color": None,
            "dash": "solid",
        }
        mm["edges"].append(edge)
        _save_mindmap(topic_id, mm, action="add_edge", source="ai")
        return {"ok": True, "edge": {"id": edge["id"], "source": source, "target": target, "label": edge["label"]}}
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="remove_mindmap_node",
    description="删除思维导图节点。会连带删除与该节点相连的所有边。"
                "用于清理错误想法、合并重复节点。",
    parameters={
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "节点ID",
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": ["node_id"],
    },
)
def remove_mindmap_node(node_id: str, topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        mm = _load_mindmap(topic_id)
        before = len(mm["nodes"])
        mm["nodes"] = [n for n in mm["nodes"] if n.get("id") != node_id]
        if len(mm["nodes"]) == before:
            return {"error": f"节点 {node_id} 不存在"}
        # 连带删边
        edges_before = len(mm["edges"])
        mm["edges"] = [e for e in mm["edges"]
                       if e.get("source") != node_id and e.get("target") != node_id]
        removed_edges = edges_before - len(mm["edges"])
        _save_mindmap(topic_id, mm, action="remove_node", source="ai")
        return {"ok": True, "removed_node": node_id, "removed_edges": removed_edges}
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="remove_mindmap_edge",
    description="删除思维导图的一条连线。节点保留。",
    parameters={
        "type": "object",
        "properties": {
            "edge_id": {
                "type": "string",
                "description": "连线ID（可选，不传则用 source+target 找）",
            },
            "source": {
                "type": "string",
                "description": "源节点ID（配合 target 定位边）",
                "default": "",
            },
            "target": {
                "type": "string",
                "description": "目标节点ID（配合 source 定位边）",
                "default": "",
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": [],
    },
)
def remove_mindmap_edge(edge_id: str = "", source: str = "", target: str = "", topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        mm = _load_mindmap(topic_id)
        before = len(mm["edges"])
        if edge_id:
            mm["edges"] = [e for e in mm["edges"] if e.get("id") != edge_id]
        elif source and target:
            mm["edges"] = [e for e in mm["edges"]
                           if not (e.get("source") == source and e.get("target") == target)]
        else:
            return {"error": "需要传 edge_id 或 source+target"}
        if len(mm["edges"]) == before:
            return {"error": "连线不存在"}
        _save_mindmap(topic_id, mm, action="remove_edge", source="ai")
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="update_mindmap_edge",
    description="修改思维导图连线的标签或样式。用于调整关系表述、改连线颜色/虚线。"
                "定位方式：优先 edge_id，否则用 source+target。"
                "可改字段：label(标签)、color(颜色 #hex 或空)、dash(实线 solid/虚线 dashed)。",
    parameters={
        "type": "object",
        "properties": {
            "edge_id": {
                "type": "string",
                "description": "连线ID（可选，不传则用 source+target 定位）",
            },
            "source": {
                "type": "string",
                "description": "源节点ID（配合 target 定位边）",
                "default": "",
            },
            "target": {
                "type": "string",
                "description": "目标节点ID（配合 source 定位边）",
                "default": "",
            },
            "label": {
                "type": "string",
                "description": "新标签（可选，不传不改）",
            },
            "color": {
                "type": "string",
                "description": "连线颜色 #hex 格式（可选，传空串清除）",
            },
            "dash": {
                "type": "string",
                "description": "线型：solid(实线,默认) 或 dashed(虚线)",
                "enum": ["solid", "dashed"],
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": [],
    },
)
def update_mindmap_edge(edge_id: str = "", source: str = "", target: str = "",
                        label: str = None, color: str = None, dash: str = None,
                        topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        mm = _load_mindmap(topic_id)
        # 定位边
        edge = None
        if edge_id:
            edge = next((e for e in mm["edges"] if e.get("id") == edge_id), None)
        elif source and target:
            edge = next((e for e in mm["edges"]
                         if e.get("source") == source and e.get("target") == target), None)
        else:
            return {"error": "需要传 edge_id 或 source+target"}
        if not edge:
            return {"error": "连线不存在"}
        # 更新字段（只改传了的）
        changed = []
        if label is not None:
            edge["label"] = label.strip()[:20]
            changed.append("label")
        if color is not None:
            edge["color"] = color.strip() or None
            changed.append("color")
        if dash is not None:
            edge["dash"] = dash if dash in ("solid", "dashed") else "solid"
            changed.append("dash")
        if not changed:
            return {"ok": True, "hint": "没有要修改的字段"}
        _save_mindmap(topic_id, mm, action="update_edge", source="ai")
        return {"ok": True, "edge_id": edge.get("id"), "changed": changed}
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="link_mindmap_to_dag",
    description="把思维导图节点关联到 DAG 任务节点（task_ref 类型）。"
                "用于建立'思考空间 → 执行计划'的桥。关联后思维导图节点会显示 DAG 节点状态色条，点击可跳转流程图。",
    parameters={
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "思维导图节点ID",
            },
            "dag_task_id": {
                "type": "string",
                "description": "DAG 任务ID（可选，不传则用当前话题的任务）",
            },
            "dag_node_id": {
                "type": "string",
                "description": "DAG 节点ID",
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": ["node_id", "dag_node_id"],
    },
)
def link_mindmap_to_dag(node_id: str, dag_node_id: str, dag_task_id: str = "", topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        # 若没传 dag_task_id，按当前话题找
        if not dag_task_id:
            try:
                from task.dag import DAG
                tid, _ = DAG.get_task_by_topic(topic_id)
                if not tid:
                    return {"error": "当前话题没有 DAG 任务"}
                dag_task_id = tid
            except Exception as e:
                return {"error": f"查找 DAG 任务失败: {e}"}
        mm = _load_mindmap(topic_id)
        node = next((n for n in mm["nodes"] if n.get("id") == node_id), None)
        if not node:
            return {"error": f"节点 {node_id} 不存在"}
        # 升级为 task_ref 类型
        node["type"] = "task_ref"
        links = node.get("links") or []
        # 去重
        exists = any(l.get("kind") == "dag" and l.get("node_id") == dag_node_id for l in links)
        if exists:
            return {"ok": True, "note": "已关联过", "node": _node_summary(node)}
        links.append({"kind": "dag", "task_id": dag_task_id, "node_id": dag_node_id})
        node["links"] = links
        _save_mindmap(topic_id, mm, action="link_to_dag", source="ai")
        return {"ok": True, "node": _node_summary(node), "link": {"kind": "dag", "task_id": dag_task_id, "node_id": dag_node_id}}
    except Exception as e:
        return {"error": str(e)}


# ── 思维导图历史工具（git 风格版本控制，AI 可撤回/重做/查看 diff）──

@register_tool(
    name="mindmap_history",
    description="查看思维导图的改动历史（git log 风格）。返回最近 N 条记录，标注当前所在版本。"
                "用于了解之前做过哪些改动、能否撤回/重做。",
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返回最近多少条历史（默认20）",
                "default": 20,
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": [],
    },
)
def mindmap_history(limit: int = 20, topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        return _mm_history_list(topic_id, limit)
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="mindmap_undo",
    description="撤回思维导图最近 N 步改动（默认1步）。返回新状态 + diff，让 AI 预览这次撤回改了什么。"
                "撤回会同步到前端可视化。注意：撤回后再改动会丢弃 redo 栈（git 风格）。",
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "integer",
                "description": "撤回多少步（默认1）",
                "default": 1,
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": [],
    },
)
def mindmap_undo(steps: int = 1, topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        steps = max(1, min(steps, 100))  # 限制单次最多100步
        cur_seq = _mm_history_get_current_seq(topic_id)
        target_seq = max(1, cur_seq - steps)
        if target_seq == cur_seq:
            return {"ok": True, "hint": "已在最早版本，无法继续撤回", "current_seq": cur_seq}
        return _mm_history_goto(topic_id, target_seq, source="ai_undo")
    except Exception as e:
        return {"error": str(e)}


@register_tool(
    name="mindmap_redo",
    description="重做思维导图最近 N 步改动（默认1步）。返回新状态 + diff。"
                "仅当之前撤回过且未做新改动时有效。",
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "integer",
                "description": "重做多少步（默认1）",
                "default": 1,
            },
            "topic_id": {
                "type": "string",
                "description": "话题ID（可选）",
            },
        },
        "required": [],
    },
)
def mindmap_redo(steps: int = 1, topic_id: str = ""):
    try:
        if not topic_id:
            topic_id = _get_active_topic_id()
        if not topic_id:
            return {"error": "没有当前话题。"}
        steps = max(1, min(steps, 100))
        cur_seq = _mm_history_get_current_seq(topic_id)
        # 找最大 seq
        conn = _mm_history_db()
        row = conn.execute(
            "SELECT MAX(seq) FROM mindmap_history WHERE topic_id=?", (topic_id,)
        ).fetchone()
        max_seq = row[0] if row and row[0] else 0
        target_seq = min(max_seq, cur_seq + steps)
        if target_seq == cur_seq:
            return {"ok": True, "hint": "已是最新版本，无法继续重做", "current_seq": cur_seq}
        return _mm_history_goto(topic_id, target_seq, source="ai_redo")
    except Exception as e:
        return {"error": str(e)}
