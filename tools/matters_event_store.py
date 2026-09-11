# -*- coding: utf-8 -*-
"""重要事项事件流存储——append-only 事实源 + 投影重放（记忆层改造 P1）。

设计（用户拍板方案，2026-09-09）：
- 只做增量不做修改：每次变更追加一条事件，历史不可变，无覆盖接口。
- 每条事件带时间戳 + seq + parent_seq（指向被改条目的前一版本事件）→ 溯源链。
- "当前状态"是投影：从事件流重放得出，不是存储实体。
- 权重倾向新：投影天然只保留每条目最新版本；检索/展示时新旧并存处标 fork。
- 零防冲突：并发写各追加各的事件（SQLite 写锁保证原子），不阻止；
  parent_seq 不匹配只打 fork 标记留给反思/合并，不静默丢失。
- 膨胀控制：事件数超阈值落快照并截断旧事件；空闲时才跑（reflect 侧调用）。

存储形态：meta 单键 JSON（条目量小：全局≤20 + 任务段按任务清理），与现有模式一致。
事件结构：{seq, ts, op, entry_id, scope, tid, parent_seq, content, reason, fork}
op ∈ add|update|remove|clear（clear=任务收尾清空该任务段）。
"""
import json
import threading
import time

from db import get_db

_lock = threading.RLock()

EVENTS_KEY = "matters_events"        # [{...事件...}]
SNAPSHOT_KEY = "matters_snapshot"    # {"upto_seq": n, "global": [...], "tasks": {tid: [...]}}
SNAPSHOT_THRESHOLD = 200             # 事件数超过此值，下次投影时落快照并截断


def _now():
    return time.time()


def _next_seq(events, snapshot):
    base = snapshot["upto_seq"] if snapshot else 0
    return (events[-1]["seq"] + 1) if events else base + 1


def _load_snapshot():
    try:
        val = get_db().get_meta(SNAPSHOT_KEY)
        return json.loads(val) if val else None
    except Exception:
        return None


def _save_snapshot(snap):
    get_db().set_meta(SNAPSHOT_KEY, json.dumps(snap, ensure_ascii=False))


def _load_events():
    try:
        val = get_db().get_meta(EVENTS_KEY)
        events = json.loads(val) if val else []
        return events if isinstance(events, list) else []
    except Exception:
        return []


def _save_events(events):
    get_db().set_meta(EVENTS_KEY, json.dumps(events, ensure_ascii=False))


def append_event(op, entry_id, scope, tid, content, reason="", parent_seq=None):
    """追加一条事件。返回事件 dict。失败抛异常由调用方兜底。"""
    with _lock:
        events = _load_events()
        snap = _load_snapshot()
        ev = {
            "seq": _next_seq(events, snap),
            "ts": _now(),
            "op": op,
            "entry_id": str(entry_id),
            "scope": scope,
            "tid": tid,
            "parent_seq": parent_seq,
            "content": (content or "")[:200] if content is not None else None,
            "reason": (reason or "")[:300],
            "fork": False,
        }
        events.append(ev)
        _save_events(events)
        return ev


def get_parent_seq(entry_id):
    """查某条目当前最新版本事件 seq（parent_seq 溯源用）。找不到返回 None。"""
    with _lock:
        snap = _load_snapshot()
        head = {}
        if snap:
            for e in snap.get("global", []):
                if e.get("entry_id") == entry_id:
                    head[entry_id] = e["seq"]
            for tid, lst in (snap.get("tasks") or {}).items():
                for e in lst:
                    if e.get("entry_id") == entry_id:
                        head[entry_id] = e["seq"]
        for ev in _load_events():
            if ev["entry_id"] != entry_id:
                continue
            if ev["op"] in ("add", "update"):
                head[entry_id] = ev["seq"]
            elif ev["op"] == "remove":
                head.pop(entry_id, None)  # 墓碑：head 清空，复活走 add
        return head.get(entry_id)


def _replay(events, snap):
    """从快照+事件重放出投影。返回 (global_entries, task_entries, forks)。"""
    global_entries, task_entries = {}, {}
    forks = []
    if snap:
        for e in snap.get("global", []):
            global_entries[e["entry_id"]] = dict(e)
        for tid, lst in (snap.get("tasks") or {}).items():
            task_entries[tid] = {e["entry_id"]: dict(e) for e in lst}
    for ev in events:
        op, eid = ev["op"], ev["entry_id"]
        if op == "clear":
            # 任务收尾清空：只清该 tid 的任务段，全局段不动
            if ev["scope"] == "task" and ev["tid"] in task_entries:
                task_entries[ev["tid"]] = {}
            continue
        bucket = global_entries if ev["scope"] == "global" else \
            task_entries.setdefault(ev["tid"], {})
        if op == "add":
            bucket[eid] = dict(ev)
        elif op in ("update", "remove"):
            prev = bucket.get(eid)
            if op == "update":
                if prev is None:
                    # update 无前置 add：跨快照边界的数据修补，仍生效但记 fork
                    ev["fork"] = True
                    forks.append(ev)
                elif prev["seq"] != ev.get("parent_seq"):
                    # 同源分叉：两个任务基于同一旧版本各自 update，git 式分支
                    ev["fork"] = True
                    forks.append(ev)
                bucket[eid] = dict(ev)
            else:  # remove = 墓碑，条目从投影消失，事件本身保留（可回滚）
                if prev is not None and prev["seq"] != ev.get("parent_seq"):
                    ev["fork"] = True
                    forks.append(ev)
                bucket.pop(eid, None)
    return global_entries, task_entries, forks


def _migrate_legacy_if_needed():
    """懒迁移：事件流为空且存在旧覆盖式数据时，导入为初始快照（幂等）。
    旧全局段 meta=important_matters，任务段 meta=important_matters_task_{tid}。"""
    if get_db().get_meta(EVENTS_KEY) is not None:
        return
    g = []
    try:
        val = get_db().get_meta("important_matters")
        for i, e in enumerate(json.loads(val) if val else [], 1):
            eid = str(e["id"]) if isinstance(e, dict) and e.get("id") else f"g-legacy{i}"
            content = e.get("content") if isinstance(e, dict) else str(e)
            g.append({"entry_id": eid, "seq": i, "ts": e.get("created_at", 0) if isinstance(e, dict) else 0,
                      "op": "add", "scope": "global", "tid": e.get("created_by_tid") if isinstance(e, dict) else None,
                      "content": (content or "")[:200], "reason": "[迁移] 旧覆盖式数据", "parent_seq": None, "fork": False})
    except Exception:
        g = []
    tasks = {}
    try:
        for key in get_db().list_meta_keys("important_matters_task_"):
            tid = key[len("important_matters_task_"):]
            lst = []
            for j, e in enumerate(json.loads(get_db().get_meta(key) or "[]"), 1):
                eid = str(e["id"]) if isinstance(e, dict) and e.get("id") else f"t-{tid[:8]}-{j}"
                content = e.get("content") if isinstance(e, dict) else str(e)
                lst.append({"entry_id": eid, "seq": j, "ts": 0, "op": "add", "scope": "task",
                            "tid": tid, "content": (content or "")[:200], "reason": "[迁移] 旧覆盖式数据",
                            "parent_seq": None, "fork": False})
            if lst:
                tasks[tid] = lst
    except Exception:
        tasks = {}
    if g or tasks:
        _save_snapshot({"upto_seq": 10 ** 9, "taken_at": _now(), "migrated": True,
                        "global": g, "tasks": tasks})
        _save_events([])


def project():
    """重放出当前投影。返回 dict：
    {global: [...], tasks: {tid: [...]}, forks: [...], event_count: n}"""
    with _lock:
        _migrate_legacy_if_needed()
        snap = _load_snapshot()
        events = _load_events()
        g, t, forks = _replay(events, snap)
        # 快照维护：事件积压超阈值时落快照+截断（幂等，谁调都行，等效空闲清理钩子）
        if len(events) >= SNAPSHOT_THRESHOLD:
            new_snap = {
                "upto_seq": events[-1]["seq"],
                "taken_at": _now(),
                "global": [e for e in g.values()],
                "tasks": {tid: [e for e in lst.values()] for tid, lst in t.items()},
            }
            _save_snapshot(new_snap)
            _save_events([])  # 快照已含 upto_seq 前全部效果，事件流清零重启
        return {
            "global": sorted(g.values(), key=lambda e: e["seq"]),
            "tasks": {tid: sorted(lst.values(), key=lambda e: e["seq"])
                      for tid, lst in t.items()},
            "forks": forks,
            "event_count": len(events),
        }


def history_of(entry_id):
    """单条目完整版本链（溯源：为什么更新、关联了什么）。按时间正序。"""
    with _lock:
        chain = []
        snap = _load_snapshot()
        if snap:
            for lst in ([snap.get("global", [])] +
                        list((snap.get("tasks") or {}).values())):
                for e in lst:
                    if e.get("entry_id") == entry_id:
                        chain.append(e)
        for ev in _load_events():
            if ev["entry_id"] == entry_id:
                chain.append(ev)
        chain.sort(key=lambda e: e["seq"])
        return chain
