# -*- coding: utf-8 -*-
"""任务重要信息数据层（原「重要事项」，2026-10-26 正式降级）。

降级原因（实测证据）：全局段 15 条里 13 条是某一次具体任务的技术细节，却作为
Block 1「对我很重要的事」无条件注入每一个任务 → 每次工作都先读一遍无关的历史
任务结论，持续误导。现在：
- 只有「任务段」是活的：combined_entries(tid) 只拼当前任务段；
- 全局段读写成废：read_matters_raw 保留（审计），set_matters/write_matters_raw
  返回失败而不是静默写；add_matter 收到 scope=global 会重定向到任务段并说明；
- 每条信息镜像进记忆网络（tools/matters_memory.py），语义相关时才被召回。

数据模型：
- 任务段：事件流 scope="task", tid=<tid>（matters_event_store 投影），
  entry {id, content, scope:"task", created_by_tid, created_at}
- 全局段：历史遗留（scope="global"），默认空，仅 history_of / 审计可读。
- 写缓冲（挂起任务暂存全局段改动）：随全局段一起失去意义，仅保留清空路径
  以免历史残留反过来把全局段写活。

旧格式兼容：全局段旧条目为纯字符串；读取时转新结构（id=位置序号字符串），不回写。
新条目 id 用 "t-<uuid8>"。
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
from tools import matters_event_store as evstore
from tools import matters_memory
from .registry import stable_state


# ⚠️ stable_state：hot_reload 会 reload 本模块，模块级 RLock 被重建 → 出现两把锁，
# 两个线程可同时读-改-写同一份事项分段/写缓冲（meta JSON 覆写），后写的覆盖先写的。
_matters_lock = stable_state("mission_overlay_matters_lock", threading.RLock)

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
    """全局段读取（**只读审计**：注入与工具层都不再使用）。

    2026-10-26 起全局段冻结为历史数据 —— 保留读取是为了 history_of / 迁移核对
    还能看到当年的条目，不是为了再把它注入出去。
    """
    try:
        proj = evstore.project()
        out = []
        for e in proj["global"]:
            out.append({
                "id": e["entry_id"], "content": _norm_content(e.get("content")),
                "scope": "global", "created_by_tid": e.get("tid"),
                "created_at": e.get("ts") or 0, "_seq": e["seq"],
            })
        return out
    except Exception:
        import traceback
        traceback.print_exc()
        return []


def _reconcile(scope, tid, entries):
    """对账式写入：对比当前投影，把差异追加为事件（add/update/remove），
    不覆盖历史。同集合不同顺序 = 零事件。返回 True/False。

    只服务任务段（scope="task"）：全局段已废止，任何 global 写入一律拒绝。
    """
    if scope != "task":
        print(f"[matters] ⛔ _reconcile 拒绝非任务段写入 scope={scope}")
        return False
    if not tid:
        print("[matters] ⛔ _reconcile 缺少 tid，无法写任务段")
        return False
    try:
        proj = evstore.project()
        current = {e["entry_id"]: e for e in proj["tasks"].get(tid, [])}
        want = {}
        for e in entries:
            want[str(e["id"])] = e
        for eid, e in want.items():
            prev = current.get(eid)
            content = _norm_content(e.get("content"))
            if prev is None:
                evstore.append_event("add", eid, scope, tid, content,
                                     reason="[对账] 写入含新增", parent_seq=None)
            elif _norm_content(prev.get("content")) != content:
                evstore.append_event("update", eid, scope, tid, content,
                                     reason="[对账] 内容变更", parent_seq=prev["seq"])
        for eid in current:
            if eid not in want:
                prev = current[eid]
                evstore.append_event("remove", eid, scope, tid,
                                     _norm_content(prev.get("content")),
                                     reason="[对账] 写入不含此条", parent_seq=prev["seq"])
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def write_matters_raw(entries):
    """全局段写入已废止（2026-10-26 降级）。恒返回 False 并打日志。

    为什么不静默成功：调用方拿到 False 才会走"没保存"的分支 —— 静默丢掉用户/
    AI 记下的东西是最坏的失败方式。要跨任务留信息请走记忆网络
    （remember_knowledge / memory 层），那里才是语义检索的地盘。
    """
    print(f"[matters] ⛔ 全局段写入已废止，拒绝写入 {len(entries or [])} 条"
          f"（改用任务段或 remember_knowledge）")
    return False


def read_task_raw(tid):
    if not tid:
        return []
    try:
        proj = evstore.project()
        out = []
        for e in proj["tasks"].get(tid, []):
            out.append({
                "id": e["entry_id"], "content": _norm_content(e.get("content")),
                "scope": "task", "created_by_tid": e.get("tid") or tid,
                "created_at": e.get("ts") or 0, "_seq": e["seq"],
            })
        return out
    except Exception:
        import traceback
        traceback.print_exc()
        return []


def write_task_raw(tid, entries):
    """兼容签名：原为整表覆盖，现实现为对账式事件追加。"""
    return _reconcile("task", tid, entries)


def get_active_tid():
    try:
        from tools.task_context import get_current_topic
        return get_current_topic()
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
    """当前任务的完整视图：**只有任务段**（+历史写缓冲预览）。

    2026-10-26 起不再拼全局段 —— 这是「任务隔离」的唯一开关点。注入、工具、
    前端序号操作全都走这里，所以只改这一处就能保证没有第二条路把全局段带回来。
    """
    with _matters_lock:
        entries = read_task_raw(tid)
        for p in get_pending(tid):
            pid = str(p.get("id") or "")
            if p.get("op") == "add":
                entries.append({
                    "id": pid or _new_id("task"), "content": _norm_content(p.get("content")),
                    "scope": "task", "created_by_tid": tid,
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
    """兼容旧接口：返回字符串列表（注入与前端显示用）。只含任务段。"""
    return [e["content"] for e in combined_entries(tid)]


def get_global_contents():
    """仅全局段内容（历史遗留，审计/迁移核对用；不参与注入）。"""
    return [e["content"] for e in read_matters_raw()]


def set_matters(entries):
    """兼容旧接口 —— 已废止：原为整表写全局段，现在恒返回 False。

    历史调用方（reflection 沉淀、老脚本）必须改走任务段或记忆网络，
    否则会以为存上了而实际什么都没存。"""
    print(f"[matters] ⛔ set_matters 已废止（原写全局段），收到 {len(entries or [])} 条"
          f" → 未保存。请改用 add_matter(scope='task') 或 remember_knowledge")
    return False



# ── 血统（保持旧签名，reflection 直接复用）─────────────

def _record_lineage(op, index, old_content=None, new_content=None):
    """任务重要信息的变更留痕。index 可为序号或标签字符串（如 "任务段"）。失败不影响主操作。

    落 layer="archival"（2026-10-26 改）：原先写 layer="story"，而 story 层会被
    Block「我的叙事记忆」**常驻注入**（LIMIT 8）—— 于是"新增重要事项#3：…"这种
    变更日志也在每个任务里常驻，和它要记录的清单一起占据注意力。变更日志是审计
    材料不是工作记忆，只留档不参与注入；要读版本链走 matters_event_store.history_of。
    """
    try:
        ts = time.strftime("%Y%m%d %H:%M")
        if op == "add":
            change_desc = f'新增任务重要信息#{index}："{new_content}"'
        elif op == "update":
            change_desc = f'任务重要信息#{index}由"{old_content}"改为"{new_content}"'
        elif op == "remove":
            change_desc = f'删除任务重要信息#{index}："{old_content}"'
        else:
            change_desc = f'变更#{index}："{new_content}"'
        text = f"[血统] {ts} {change_desc}（操作：{op}）"
        from memory.fragment_store import get_store
        from memory.relation_store import RelationStore
        fid = get_store().add(
            text, source="lineage", tags="任务重要信息,血统", layer="archival",
            importance=5.0, epistemic="experience",
        )
        RelationStore().add(
            subject_id=fid, predicate=f"任务重要信息#{index}被{op}",
            object_value=change_desc, edge_type="fact",
            reason=f"血统记录：{op} 任务重要信息#{index}",
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
    """任务收尾：清掉该任务的挂起缓冲（全局段已废止，缓冲无处可合并）。

    2026-10-26：写缓冲原本是"挂起任务改全局段先暂存、收尾再合并"。全局段取消后
    缓冲失去目标。这里**不再把内容搬进任何地方**（尤其是不能落回全局段，否则等于
    给已废止的跨任务清单开后门），只回收缓冲并留一条可审的记录。

    返回 {"merged":0, "orphaned":[...], "detail":[...], "discarded":n}。
    """
    report = {"merged": 0, "orphaned": [], "detail": [], "discarded": 0}
    with _matters_lock:
        pend = get_pending(tid)
        if not pend:
            clear_pending(tid)
            return report
        report["discarded"] = len(pend)
        report["orphaned"] = list(pend)
        report["detail"].append(
            f"[{tid[:8]}] 挂起缓冲 {len(pend)} 项因「重要信息降级为跟随任务」不再合并，已丢弃"
            f"（内容仍在 matters_events 事件流里可追溯）")
        print(f"[matters] ♻️ 丢弃历史挂起缓冲 tid={tid[:8]} n={len(pend)}")
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
    """新增一条「该任务重要的信息」。只写当前任务段。

    2026-10-26 降级：全局段废止。传 scope=global 不再跨任务生效，会被**重定向**
    到当前任务段并在 note 里说清楚 —— 不静默照做（AI 以为存了全局却没有），
    也不直接报错（信息本身还是该留下）。真要跨任务沉淀，走 remember_knowledge。

    返回 {success, id, scope:"task", ...} 或 {error}。"""
    content = _norm_content(content)
    if not content:
        return {"error": "内容不能为空"}
    tid = tid or get_active_tid()
    note = ""
    if scope and scope != "task":
        note = ("注意：重要信息已降级为「跟随任务」，不存在跨任务的全局清单。"
                "本条已写入当前任务段；要跨任务长期留存请用 remember_knowledge。")
        print(f"[matters] ⚠️ 收到 scope={scope}，已重定向到任务段 tid={str(tid)[:8]}")
    if not tid:
        return {"error": "当前没有活跃任务，无法写入任务段（重要信息跟随任务）"}
    with _matters_lock:
        traw = read_task_raw(tid)
        entry = {"id": _new_id("task"), "content": content, "scope": "task",
                 "created_by_tid": tid, "created_at": _now()}
        traw.append(entry)
        if not write_task_raw(tid, traw):
            return {"error": "写入 DB 失败，重要信息未保存。请重试。"}
        _record_lineage("add", f"任务段{tid[:8]}", None, content)
        # 连接记忆网络：让它在语义相关时能被召回，而不是靠注入常驻
        matters_memory.mirror_add(tid, entry["id"], content)
        out = {"success": True, "id": entry["id"], "scope": "task", "content": content}
        if note:
            out["note"] = note
        return out


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
    """改写一条「该任务重要的信息」。只能改当前任务段里的条目。"""
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
        owner = entry.get("created_by_tid") or tid
        traw = read_task_raw(owner)
        hit = False
        for e in traw:
            if e["id"] == eid:
                e["content"] = content
                hit = True
                break
        if not hit:
            return {"error": "任务段条目未找到，可能已被清理"}
        if not write_task_raw(owner, traw):
            return {"error": "写入 DB 失败，重要信息未保存。请重试。"}
        _record_lineage("update", f"任务段{tid[:8]}", old, content)
        matters_memory.mirror_update(eid, content, tid=owner)
        return {"success": True, "id": eid, "scope": "task", "content": content}


def remove_matter(index=None, mid=None, reason=None, tid=None, by="tool"):
    """删除一条「该任务重要的信息」，记忆网络里的镜像一并下掉。"""
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
        owner = entry.get("created_by_tid") or tid
        traw = [e for e in read_task_raw(owner) if e["id"] != eid]
        if not write_task_raw(owner, traw):
            return {"error": "写入 DB 失败，重要信息未保存。请重试。"}
        _record_lineage("remove", f"任务段{tid[:8]}", old, None)
        matters_memory.mirror_remove(eid)
        return {"success": True, "id": eid, "scope": "task", "removed": old}

