# -*- coding: utf-8 -*-
"""多任务数据分段与写缓冲——重要事项全局/任务分段（节点2）+ 写缓冲暂存区（节点4）+ 收尾合并（节点5数据层）。

数据模型：
- 全局段：meta "important_matters"，条目 {id, content, scope:"global", created_by_tid, created_at}
- 任务段：meta f"important_matters_task_{tid}"，同结构，scope:"task"
- 叠加视图：combined_entries(tid) = 全局段 + 任务段（+挂起缓冲的叠加预览）
- 写缓冲：挂起中的任务改全局段时暂存到 meta f"mission_overlay_pending_{tid}"，
  任务结束 merge_pending_on_finish 合并；挂起期间 AI 通过叠加视图实时可见。

旧格式兼容：全局段旧条目为纯字符串；读取时转新结构（id=位置序号字符串），不回写。
新条目 id 用 "g-<uuid8>" / "t-<uuid8>"。
"""
import json
import os
import sys
import threading
import time
import uuid

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from db import get_db

_matters_lock = threading.RLock()

TASK_KEY = "important_matters_task_"
PENDING_KEY = "mission_overlay_pending_"
SUSPEND_KEY = "task_suspended_"


def _now():
    return time.time()


def _norm_content(c, cap=200):
    return str(c or "").strip()[:cap]


def _new_id(scope):
    return ("g-" if scope == "global" else "t-") + uuid.uuid4().hex[:8]


# ── 原始存取（读写都返回/接收 dict 列表）───────────────

def read_matters_raw():
    """全局段原始读取。旧字符串条目转新结构（id=序号），仅内存转换不回写。"""
    try:
        val = get_db().get_meta("important_matters")
        if not val:
            return []
        entries = json.loads(val)
        if not isinstance(entries, list):
            return []
        out = []
        for i, e in enumerate(entries, 1):
            if isinstance(e, dict):
                d = {
                    "id": str(e.get("id") or i),
                    "content": _norm_content(e.get("content")),
                    "scope": "global",
                    "created_by_tid": e.get("created_by_tid"),
                    "created_at": e.get("created_at") or 0,
                }
            else:
                d = {"id": str(i), "content": str(e), "scope": "global",
                     "created_by_tid": None, "created_at": 0}
            out.append(d)
        return out
    except Exception:
        import traceback
        traceback.print_exc()
        return []


def write_matters_raw(entries):
    try:
        get_db().set_meta("important_matters", json.dumps(entries, ensure_ascii=False))
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def read_task_raw(tid):
    if not tid:
        return []
    try:
        val = get_db().get_meta(TASK_KEY + tid)
        if not val:
            return []
        entries = json.loads(val)
        if not isinstance(entries, list):
            return []
        out = []
        for e in entries:
            if isinstance(e, dict):
                d = {
                    "id": str(e.get("id") or _new_id("t")),
                    "content": _norm_content(e.get("content")),
                    "scope": "task",
                    "created_by_tid": e.get("created_by_tid") or tid,
                    "created_at": e.get("created_at") or 0,
                }
            else:
                d = {"id": _new_id("t"), "content": str(e), "scope": "task",
                     "created_by_tid": tid, "created_at": 0}
            out.append(d)
        return out
    except Exception:
        import traceback
        traceback.print_exc()
        return []


def write_task_raw(tid, entries):
    try:
        get_db().set_meta(TASK_KEY + tid, json.dumps(entries, ensure_ascii=False))
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def get_active_tid():
    try:
        return get_db().get_active_topic_id()
    except Exception:
        return None


# ── 挂起标记（由服务端在任务挂起/恢复时设置）──────────

def set_suspended(tid, on=True):
    try:
        get_db().set_meta(SUSPEND_KEY + tid, "1" if on else "0")
        return True
    except Exception:
        return False


def is_suspended(tid):
    if not tid:
        return False
    try:
        return get_db().get_meta(SUSPEND_KEY + tid) == "1"
    except Exception:
        return False


# ── 叠加视图 ─────────────────────────────────────────────

def combined_entries(tid=None):
    """叠加视图：全局段 + 任务段，再应用挂起缓冲预览。视图与序号操作统一走这里。"""
    with _matters_lock:
        entries = read_matters_raw() + read_task_raw(tid)
        for p in get_pending(tid):
            pid = str(p.get("id") or "")
            if p.get("op") == "add":
                entries.append({
                    "id": pid or _new_id("g"), "content": _norm_content(p.get("content")),
                    "scope": "global", "created_by_tid": tid,
                    "created_at": p.get("ts") or 0, "_pending": True,
                })
            elif p.get("op") == "update":
                for e in entries:
                    if e["id"] == pid:
                        e["content"] = _norm_content(p.get("content"))
                        e["_pending"] = True
                        break
            elif p.get("op") == "remove":
                entries = [e for e in entries if e["id"] != pid]
        return entries


def get_matters(tid=None):
    """兼容旧接口：返回字符串列表（注入与前端显示用）。"""
    return [e["content"] for e in combined_entries(tid)]


def get_global_contents():
    """仅全局段内容（去重判断用，不含任务段）。"""
    return [e["content"] for e in read_matters_raw()]


def set_matters(entries):
    """兼容旧接口：写入全局段。接受 str 列表或 dict 列表，尽量保留原 id。"""
    with _matters_lock:
        old = read_matters_raw()
        out = []
        for i, e in enumerate(entries):
            if isinstance(e, dict):
                d = dict(e)
                d["content"] = _norm_content(d.get("content"))
                d.setdefault("id", old[i]["id"] if i < len(old) else _new_id("g"))
                d.setdefault("scope", "global")
                d.setdefault("created_by_tid", None)
                d.setdefault("created_at", 0)
            else:
                d = {
                    "id": old[i]["id"] if i < len(old) else _new_id("g"),
                    "content": _norm_content(e), "scope": "global",
                    "created_by_tid": old[i].get("created_by_tid") if i < len(old) else None,
                    "created_at": old[i].get("created_at", 0) if i < len(old) else 0,
                }
            out.append(d)
        return write_matters_raw(out)


# ── 血统（保持旧签名，reflection 直接复用）─────────────

def _record_lineage(op, index, old_content=None, new_content=None):
    """重要事项变更留痕。index 可为序号或标签字符串（如 "任务段"）。失败不影响主操作。"""
    try:
        ts = time.strftime("%Y%m%d %H:%M")
        if op == "add":
            change_desc = f'新增重要事项#{index}："{new_content}"'
        elif op == "update":
            change_desc = f'重要事项#{index}由"{old_content}"改为"{new_content}"'
        elif op == "remove":
            change_desc = f'删除重要事项#{index}："{old_content}"'
        else:
            change_desc = f'变更#{index}："{new_content}"'
        text = f"[血统] {ts} {change_desc}（操作：{op}）"
        from memory.fragment_store import get_store
        from memory.relation_store import RelationStore
        fid = get_store().add(
            text, source="lineage", tags="重要事项,血统", layer="story",
            importance=7.0, epistemic="experience",
        )
        RelationStore().add(
            subject_id=fid, predicate=f"重要事项#{index}被{op}",
            object_value=change_desc, edge_type="fact",
            reason=f"血统记录：{op} 重要事项#{index}",
        )
    except Exception:
        pass


# ── 写缓冲（挂起任务的全局段改动暂存）─────────────────

def add_pending(op, entry_id, content, old_content, reason, tid=None):
    tid = tid or get_active_tid()
    if not tid:
        return False
    p = {
        "op": op, "id": str(entry_id or ""), "content": _norm_content(content),
        "old_content": old_content, "reason": _norm_content(reason, 300),
        "ts": _now(), "tid": tid, "scope": "global",
    }
    with _matters_lock:
        pend = get_pending(tid)
        # 同一条目的连续 update 合并（保留最初 old_content）
        if op == "update":
            for q in pend:
                if q["op"] == "update" and q["id"] == p["id"]:
                    q["content"] = p["content"]
                    q["reason"] = p["reason"]
                    q["ts"] = p["ts"]
                    return _save_pending(tid, pend)
        if op == "remove":

            # remove 抵消未合并的 add（含其后续 update）：纯抵消时 remove 自身也不留

            before = len(pend)

            pend = [q for q in pend if not (q["op"] == "add" and q["id"] == p["id"])]

            removed_add = len(pend) < before

            pend = [q for q in pend if not (q["op"] == "update" and q["id"] == p["id"])]

            if removed_add:

                return _save_pending(tid, pend)

        pend.append(p)

        return _save_pending(tid, pend)


def get_pending(tid):
    if not tid:
        return []
    try:
        val = get_db().get_meta(PENDING_KEY + tid)
        if not val:
            return []
        data = json.loads(val)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_pending(tid, pend):
    try:
        get_db().set_meta(PENDING_KEY + tid, json.dumps(pend, ensure_ascii=False))
        return True
    except Exception:
        return False


def clear_pending(tid):
    try:
        get_db().set_meta(PENDING_KEY + tid, "[]")
        return True
    except Exception:
        return False


def merge_pending_on_finish(tid):
    """任务收尾：把挂起缓冲合并进全局段。新增/改/删直通并留痕，目标丢失的记为孤儿。
    返回 {"merged": n, "orphaned": [...], "detail": [...]}。"""
    report = {"merged": 0, "orphaned": [], "detail": []}
    with _matters_lock:
        pend = get_pending(tid)
        if not pend:
            clear_pending(tid)
            return report
        raw = read_matters_raw()
        def _fmt_reason(p):
            r = (p.get("reason") or "").strip()
            return f"（理由：{r}）" if r else ""
        for p in pend:
            op, pid = p.get("op"), str(p.get("id") or "")
            content = _norm_content(p.get("content"))
            if op == "add":
                raw.append({"id": pid or _new_id("g"), "content": content,
                            "scope": "global", "created_by_tid": tid,
                            "created_at": p.get("ts") or _now()})
                _record_lineage("add", f"收尾合并({tid[:8]})", None, content)
                report["detail"].append(f"新增「{content[:40]}」{_fmt_reason(p)}")
                report["merged"] += 1
            elif op == "update":
                hit = False
                for e in raw:
                    if e["id"] == pid:
                        _record_lineage("update", f"收尾合并({tid[:8]})", e["content"], content)
                        report["detail"].append(
                            f"【核心规则】更新「{(e['content'] or '')[:30]}」→「{content[:40]}」{_fmt_reason(p)}")
                        e["content"] = content
                        hit = True
                        break
                if hit:
                    report["merged"] += 1
                else:
                    report["orphaned"].append(p)
            elif op == "remove":
                before = len(raw)
                raw = [e for e in raw if e["id"] != pid]
                if len(raw) < before:
                    _record_lineage("remove", f"收尾合并({tid[:8]})", p.get("old_content"), None)
                    report["detail"].append(
                        f"【核心规则】删除「{(p.get('old_content') or '')[:40]}」{_fmt_reason(p)}")
                    report["merged"] += 1
                else:
                    report["orphaned"].append(p)
        write_matters_raw(raw)
        clear_pending(tid)
        try:
            get_db().set_meta(SUSPEND_KEY + tid, "0")
        except Exception:
            pass
    return report


def merge_orphans_on_startup():
    """启动时扫所有残留挂起缓冲（任务崩溃/被掐断导致未合并），逐一按同规则合并。
    每条操作都带 reason，脱离原上下文也能审。返回 {scanned, merged, orphaned, details}。"""
    report = {"scanned": 0, "merged": 0, "orphaned": [], "details": []}
    try:
        keys = get_db().list_meta_keys(PENDING_KEY)
    except Exception:
        import traceback
        traceback.print_exc()
        return report
    for k in keys:
        tid = k[len(PENDING_KEY):]
        if not tid:
            continue
        if not get_pending(tid):
            continue
        report["scanned"] += 1
        r = merge_pending_on_finish(tid)
        report["merged"] += r.get("merged", 0)
        report["orphaned"].extend(r.get("orphaned", []))
        for d in r.get("detail", []):
            report["details"].append(f"[{tid[:8]}] {d}")
        # 顺手清掉过期的挂起标记
        try:
            get_db().set_meta(SUSPEND_KEY + tid, "0")
        except Exception:
            pass
    return report


# ── 三个核心变更操作（工具与 reflection 共用）─────────

def add_matter(content, scope=None, reason=None, tid=None, by="tool"):
    """新增重要事项。scope=global 写全局段（挂起任务进缓冲），scope=task 写当前任务段。
    返回 {success, id, scope, ...} 或 {error}。"""
    content = _norm_content(content)
    if not content:
        return {"error": "内容不能为空"}
    tid = tid or get_active_tid()
    scope = scope or ("task" if tid else "global")
    if scope not in ("global", "task"):
        return {"error": "scope 只能是 global 或 task"}
    if scope == "task" and not tid:
        return {"error": "当前没有活跃任务，无法写入任务段"}
    with _matters_lock:
        if scope == "global":
            if is_suspended(tid):
                pid = _new_id("g")
                add_pending("add", pid, content, None, reason, tid)
                return {"success": True, "id": pid, "scope": "global", "buffered": True,
                        "note": "当前任务挂起中，已进入写缓冲，任务结束合并生效"}
            raw = read_matters_raw()
            if len(raw) >= 20:
                return {"error": "全局重要事项已达 20 条上限，请先清理"}
            entry = {"id": _new_id("g"), "content": content, "scope": "global",
                     "created_by_tid": tid, "created_at": _now()}
            raw.append(entry)
            if not write_matters_raw(raw):
                return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
            _record_lineage("add", f"全局({by})", None, content)
            return {"success": True, "id": entry["id"], "scope": "global", "content": content}
        # 任务段
        traw = read_task_raw(tid)
        entry = {"id": _new_id("t"), "content": content, "scope": "task",
                 "created_by_tid": tid, "created_at": _now()}
        traw.append(entry)
        if not write_task_raw(tid, traw):
            return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
        _record_lineage("add", f"任务段{tid[:8]}", None, content)
        return {"success": True, "id": entry["id"], "scope": "task", "content": content}


def _resolve_target(index, mid, tid):
    """按 id 或注入序号（1开始，叠加视图）定位条目。返回 (entry, position_label) 或 (None, err)。"""
    entries = combined_entries(tid)
    if mid:
        for e in entries:
            if e["id"] == str(mid):
                return e, e["id"]
        return None, f"找不到 id 为 {mid} 的条目"
    if index is None:
        return None, "需要 index 或 id 其一"
    idx = int(index) - 1
    if idx < 0 or idx >= len(entries):
        return None, f"序号 {index} 超出范围，当前共 {len(entries)} 条"
    return entries[idx], entries[idx]["id"]


def update_matter(index=None, mid=None, content=None, reason=None, tid=None, by="tool"):
    content = _norm_content(content)
    if not content:
        return {"error": "内容不能为空"}
    tid = tid or get_active_tid()
    with _matters_lock:
        entry, label = None, None
        try:
            entry, label = _resolve_target(index, mid, tid)
        except Exception:
            pass
        if not entry:
            return {"error": label or "找不到目标条目"}
        eid, old = entry["id"], entry["content"]
        scope = entry.get("scope", "global")
        if scope == "global":
            if is_suspended(tid):
                add_pending("update", eid, content, old, reason, tid)
                return {"success": True, "id": eid, "scope": "global", "buffered": True,
                        "note": "当前任务挂起中，已进入写缓冲，任务结束合并生效"}
            raw = read_matters_raw()
            for e in raw:
                if e["id"] == eid:
                    e["content"] = content
                    break
            if not write_matters_raw(raw):
                return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
            _record_lineage("update", f"全局({by})", old, content)
            return {"success": True, "id": eid, "scope": "global", "content": content}
        traw = read_task_raw(entry.get("created_by_tid") or tid)
        hit = False
        for e in traw:
            if e["id"] == eid:
                e["content"] = content
                hit = True
                break
        if not hit:
            return {"error": "任务段条目未找到，可能已被清理"}
        if not write_task_raw(entry.get("created_by_tid") or tid, traw):
            return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
        _record_lineage("update", f"任务段{tid[:8]}", old, content)
        return {"success": True, "id": eid, "scope": "task", "content": content}


def remove_matter(index=None, mid=None, reason=None, tid=None, by="tool"):
    tid = tid or get_active_tid()
    with _matters_lock:
        entry, label = None, None
        try:
            entry, label = _resolve_target(index, mid, tid)
        except Exception:
            pass
        if not entry:
            return {"error": label or "找不到目标条目"}
        eid, old = entry["id"], entry["content"]
        scope = entry.get("scope", "global")
        if scope == "global":
            if is_suspended(tid):
                add_pending("remove", eid, None, old, reason, tid)
                return {"success": True, "id": eid, "scope": "global", "buffered": True,
                        "removed": old,
                        "note": "当前任务挂起中，已进入写缓冲，任务结束合并生效"}
            raw = [e for e in read_matters_raw() if e["id"] != eid]
            if not write_matters_raw(raw):
                return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
            _record_lineage("remove", f"全局({by})", old, None)
            return {"success": True, "id": eid, "scope": "global", "removed": old}
        owner = entry.get("created_by_tid") or tid
        traw = [e for e in read_task_raw(owner) if e["id"] != eid]
        if not write_task_raw(owner, traw):
            return {"error": "写入 DB 失败，重要事项未保存。请重试。"}
        _record_lineage("remove", f"任务段{tid[:8]}", old, None)
        return {"success": True, "id": eid, "scope": "task", "removed": old}
