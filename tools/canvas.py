# -*- coding: utf-8 -*-
"""
canvas.py — 工作区画布布局存取（画布+窗口模型）

设计共识（2026-09-19 UI新格局定稿）：
1. 工作区 = 画布 + 窗口：窗口是"真实对象的取景框"（导图/文件/草稿纸/临时件）
2. 窗口可任意摆位、关闭不丢内容、下次打开恢复原位 → 布局持久化到本文件
3. 路径系统分配：画布数据由引擎管理，用户不接触路径概念
4. 存储位置：data/missions/{tid}/canvas.json（与 dag.json/mindmap.json 同目录）
5. 窗口内容指针 ref 不复制内容，只存"指向哪里"：
   - mindmap → 导图对象 id（多对象导图，节点②落地）
   - file    → 工作区内相对路径
   - draft   → 任务草稿纸（挂任务，用户否决搬家，保持不变）
   - temp    → 临时件独立存储文件（工作区/temps/）
"""
import json
import os
import threading
import time
import uuid

from .registry import stable_state

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # bigeyeZero 根

# 画布档并发保护：同话题多次回存（多标签拖动/多端同时改）会同时写同一个
# canvas.json，Windows 下 os.replace 会因 tmp 文件被占用报 WinError 32。
# 每个话题一把可重入锁（同线程内嵌套 save 不死锁），再配唯一 tmp 名双保险。
# ⚠️ stable_state：hot_reload 会 reload 本模块，模块级 RLock 被重建 → 出现两把
# 锁，两个线程可同时跑 save_canvas 写同一个 canvas.json（丢窗口/写坏档）。
_SAVE_LOCK = stable_state("canvas_save_lock", threading.RLock)

WINDOW_TYPES = ("mindmap", "file", "draft", "temp")
# 顶层标签类型（2026-09-19 标签化）：画布本身也是一个标签
# outdir（2026-10-22）：任务的「产出文件夹」标签——用户在输入框里选的外部目录。
# 🔴 这是白名单，漏登记 = 标签被 _norm_tab 静默丢弃（save 回包一覆盖，active 直接
#    回落成 t_files，表现成"点一下就跳回工作区"）。加类型必须同时加在这里。
TAB_TYPES = ("files", "explorer", "canvas", "mindmap", "file", "draft", "temp", "outdir")
FILE_NAME = "canvas.json"

# 窗口几何限制
MIN_W, MIN_H = 160, 120
MAX_COORD = 20000
MAX_TITLE = 120
# 布局限制
PANE_MAX_TABS = 40
ASSET_W_MIN, ASSET_W_MAX = 200, 720
# 分屏：最多两栏；左栏权重夹紧到 0.15~0.85（防止拖成零宽）
SPLIT_MAX_PANES = 2
RATIO_MIN, RATIO_MAX = 0.15, 0.85


def _missions_dir():
    return os.path.join(BASE_DIR, "data", "missions")


def canvas_path(tid):
    """某话题的画布文件绝对路径。"""
    tid = str(tid or "").strip()
    if not tid or "/" in tid or "\\" in tid or ".." in tid:
        raise ValueError("非法话题ID")
    return os.path.join(_missions_dir(), tid, FILE_NAME)


def default_tabs():
    """默认标签集：一个文件标签。"""
    return [{"id": "t_files", "type": "files", "title": "文件", "ref": None}]


def default_pane():
    """默认单栏：带一个文件标签。"""
    return {"type": "pane", "id": "p0", "tabs": default_tabs(), "active": "t_files"}


def default_canvas():
    return {"version": 3, "windows": [], "layout": default_pane(),
            "ratio": 1.0, "assetWidth": 320, "updated_at": 0}


def _norm_tab(t):
    """单个标签规范化；类型非法返回 None（丢弃）。"""
    if not isinstance(t, dict) or t.get("type") not in TAB_TYPES:
        return None
    out = {
        "id": str(t.get("id") or ("t_" + uuid.uuid4().hex[:8])),
        "type": t["type"],
        "title": str(t.get("title") or "")[:MAX_TITLE],
        "ref": t.get("ref") if isinstance(t.get("ref"), (dict, str, type(None))) else None,
    }
    # outdir 标签记住"上次浏览到哪一层"（前端 P7 写、wsTabBody 读）。不带上就等于
    # 每次切回标签都跳回根目录 —— 这里静默丢字段，前端一点异常都看不到。
    if t.get("path"):
        out["path"] = str(t.get("path"))[:512]
    return out


def _norm_pane(p):
    """单栏规范化：标签去重、保底一个、active 必须命中。"""
    if not isinstance(p, dict):
        return default_pane()
    tabs, seen = [], set()
    for t in (p.get("tabs") or []):
        t = _norm_tab(t)
        if not t or t["id"] in seen:
            continue
        seen.add(t["id"])
        tabs.append(t)
        if len(tabs) >= PANE_MAX_TABS:
            break
    if not tabs:
        tabs = default_tabs()
    active = p.get("active")
    if active not in {t["id"] for t in tabs}:
        active = tabs[0]["id"]
    return {"type": "pane", "id": str(p.get("id") or "p0"), "tabs": tabs, "active": active}


def _norm_layout(layout, legacy_tabs=None, legacy_active=None):
    """规范化 layout。支持 pane 单栏 / split 左右两栏；旧扁平 tabs 自动包成 pane。

    节点⑫：split 只允许 dir=row 且恰好两个子栏；子栏多于两个取前两个，
    少于两个降级为单栏（不报错、不丢标签）。ratio 为左栏 flex 权重。
    """
    if isinstance(layout, dict):
        if layout.get("type") == "pane":
            return _norm_pane(layout)
        if layout.get("type") == "split":
            kids = []
            for c in (layout.get("children") or []):
                if isinstance(c, dict) and c.get("type") == "pane":
                    kids.append(_norm_pane(c))
                if len(kids) >= 2:
                    break
            if len(kids) == 2:
                return {"type": "split", "dir": "row", "ratio": _norm_ratio(layout.get("ratio")),
                        "children": kids}
            if len(kids) == 1:
                return kids[0]
    if isinstance(legacy_tabs, list):
        return _norm_pane({"tabs": legacy_tabs, "active": legacy_active})
    return default_pane()


def _norm_ratio(v, default=0.5):
    """分屏比例夹紧到 [RATIO_MIN, RATIO_MAX]，非法值回落默认。"""
    try:
        r = float(v)
    except (TypeError, ValueError):
        r = float(default)
    if r != r:  # NaN
        r = float(default)
    return max(RATIO_MIN, min(RATIO_MAX, r))


# 旧画布窗口类型 → 顶层标签类型（窗口升格为标签）
_WIN2TAB = {"file": "file", "mindmap": "mindmap", "draft": "draft", "temp": "temp"}


def _migrate_windows_to_tabs(canvas):
    """旧画布 windows → 顶层标签（幂等）。返回 True 表示发生了迁移。

    2026-09-19 标签化：画布这一层取消，原窗口类型升格为顶层标签。
    迁移后清空 windows，避免每次 load 重复生成标签。
    """
    wins = canvas.get("windows") or []
    if not wins:
        return False
    pane = canvas.get("layout") if isinstance(canvas.get("layout"), dict) else {}
    tabs = list(pane.get("tabs") or [])
    ids = {t.get("id") for t in tabs if isinstance(t, dict)}
    changed = False
    for w in wins:
        if not isinstance(w, dict):
            continue
        ttype = _WIN2TAB.get(str(w.get("type") or ""))
        if not ttype:
            continue
        tid = "t_" + uuid.uuid4().hex[:8]
        tabs.append({"id": tid, "type": ttype,
                     "title": str(w.get("title") or "")[:MAX_TITLE],
                     "ref": w.get("ref") if isinstance(w.get("ref"), (dict, str, type(None))) else None})
        ids.add(tid)
        changed = True
    if changed:
        canvas["layout"] = _norm_pane({"tabs": tabs, "active": pane.get("active")})
        canvas["windows"] = []
    return changed


def _normalize_canvas(canvas):
    """补齐/校验 layout、ratio、assetWidth。旧档不丢 windows。"""
    canvas["layout"] = _norm_layout(canvas.get("layout"), canvas.get("tabs"), canvas.get("active"))
    canvas.pop("tabs", None)    # 扁平字段只作输入兼容，不落盘
    canvas.pop("active", None)
    try:
        r = float(canvas.get("ratio", 1.0))
    except (TypeError, ValueError):
        r = 1.0
    canvas["ratio"] = min(0.9, max(0.1, r))
    try:
        aw = int(canvas.get("assetWidth", 320))
    except (TypeError, ValueError):
        aw = 320
    canvas["assetWidth"] = min(ASSET_W_MAX, max(ASSET_W_MIN, aw))
    return canvas


def load_canvas(tid):
    """读画布布局。文件缺失→默认空画布；损坏→备份后重建（坏档自愈）。"""
    p = canvas_path(tid)
    if not os.path.exists(p):
        return default_canvas()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("结构不对")
        if not isinstance(data.get("windows"), list):
            data["windows"] = []
        data = _normalize_canvas(data)
        if _migrate_windows_to_tabs(data):
            try:
                save_canvas(tid, data)   # 迁移即落盘，下次 load 不再重复
            except Exception:
                pass
        return data
    except Exception:
        try:
            os.replace(p, p + ".corrupt")  # 坏档留证，不覆盖用户现场
        except Exception:
            pass
        return default_canvas()


def save_canvas(tid, canvas):
    """整包保存（校验+原子写）。前端全量回存、服务端函数式修改都走这里。

    兼容：调用方只传 windows（如画布窗口拖动回存）时，布局字段从磁盘已有档
    合并，绝不用默认值把用户标签重置掉。
    """
    if not isinstance(canvas, dict):
        raise ValueError("canvas 必须是对象")
    if not isinstance(canvas.get("windows"), list):
        # 前端标签化后只回存 {layout, ratio, assetWidth}；windows 从磁盘旧档补齐
        try:
            canvas["windows"] = list(load_canvas(tid).get("windows") or [])
        except Exception:
            canvas["windows"] = []
    clean = []
    for w in canvas["windows"]:
        clean.append(_validate_window(w))
    canvas["windows"] = clean
    if not isinstance(canvas.get("layout"), dict) and not isinstance(canvas.get("tabs"), list):
        try:
            old = load_canvas(tid)
            canvas["layout"] = old.get("layout")
            if canvas.get("ratio") is None:
                canvas["ratio"] = old.get("ratio")
            if canvas.get("assetWidth") is None:
                canvas["assetWidth"] = old.get("assetWidth")
        except Exception:
            pass
    canvas = _normalize_canvas(canvas)
    canvas["version"] = 3
    canvas["updated_at"] = time.time()
    p = canvas_path(tid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".%s.tmp" % uuid.uuid4().hex[:8]   # 唯一 tmp：并发写互不占用
    with _SAVE_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(canvas, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return canvas


def _validate_window(w):
    """单窗口校验+规范化。非法字段抛 ValueError，几何越界收紧到限制内。"""
    if not isinstance(w, dict):
        raise ValueError("窗口必须是对象")
    if w.get("type") not in WINDOW_TYPES:
        raise ValueError("窗口类型必须是 " + "/".join(WINDOW_TYPES))
    w["id"] = str(w.get("id") or ("w_" + uuid.uuid4().hex[:8]))
    title = str(w.get("title") or "").strip()
    w["title"] = title[:MAX_TITLE]

    def _num(key, default, minimum):
        try:
            v = float(w.get(key, default))
        except (TypeError, ValueError):
            v = float(default)
        v = max(minimum, min(MAX_COORD, v))
        w[key] = v
        return v

    _num("x", 40, -MAX_COORD)
    _num("y", 40, -MAX_COORD)
    _num("w", 420, MIN_W)
    _num("h", 320, MIN_H)

    ref = w.get("ref")
    if ref is not None and not isinstance(ref, dict):
        raise ValueError("ref 必须是对象或 null")
    w["ref"] = ref
    now = time.time()
    w.setdefault("created_at", now)
    w["updated_at"] = now
    return w


# ── 函数式修改（服务端 AI/工具用，避免整包回传） ──────────

def add_window(tid, win):
    """加一个窗口，自动补 id/时间戳，返回规范化后的窗口。"""
    canvas = load_canvas(tid)
    win = _validate_window(win)
    win["id"] = "w_" + uuid.uuid4().hex[:8]
    canvas["windows"].append(win)
    save_canvas(tid, canvas)
    return win


def update_window(tid, win_id, patch):
    """部分更新：patch 里给什么改什么（x/y/w/h/title/ref），其余不动。"""
    canvas = load_canvas(tid)
    for w in canvas["windows"]:
        if w.get("id") == win_id:
            merged = dict(w)
            merged.update({k: v for k, v in (patch or {}).items()
                           if k in ("x", "y", "w", "h", "title", "ref")})
            merged["id"] = win_id
            new_w = _validate_window(merged)
            canvas["windows"][canvas["windows"].index(w)] = new_w
            save_canvas(tid, canvas)
            return new_w
    raise KeyError("窗口不存在: " + str(win_id))


def remove_window(tid, win_id):
    """删窗口（只删布局框，不删窗口背后的对象本身）。"""
    canvas = load_canvas(tid)
    before = len(canvas["windows"])
    canvas["windows"] = [w for w in canvas["windows"] if w.get("id") != win_id]
    if len(canvas["windows"]) == before:
        raise KeyError("窗口不存在: " + str(win_id))
    save_canvas(tid, canvas)
    return True


def get_window(tid, win_id):
    for w in load_canvas(tid)["windows"]:
        if w.get("id") == win_id:
            return w
    return None


# ── 标签操作（服务端 AI/工具用）──────────────────────
# 节点⑫：支持单栏 / 左右双栏；栏操作统一走 _pane(canvas, index)。

def _pane(canvas, index=0):
    """取第 index 栏对象（内部可变引用）。单栏时 index=0 即那唯一一栏。"""
    lay = canvas["layout"]
    if isinstance(lay, dict) and lay.get("type") == "split":
        kids = lay.get("children") or []
        if 0 <= index < len(kids):
            return kids[index]
        return kids[0] if kids else default_pane()
    return lay


def pane_count(canvas):
    """当前栏数（1 或 2）。"""
    lay = canvas.get("layout")
    if isinstance(lay, dict) and lay.get("type") == "split":
        return len([c for c in (lay.get("children") or []) if isinstance(c, dict)])
    return 1


def split_pane(tid, tab_id, side="right", ratio=0.5):
    """把 tab_id 从原栏移到新栏（side=left/right），原栏保留其余标签。

    - 已是两栏 → ValueError（不出现第三栏）
    - 栏内只剩这一个标签 → 拒绝拆分（拆了原栏就空了）
    - 原栏被拆空后若无标签，回落到默认 files 标签（不销毁栏）
    """
    canvas = load_canvas(tid)
    if pane_count(canvas) >= SPLIT_MAX_PANES:
        raise ValueError("最多 %d 栏" % SPLIT_MAX_PANES)
    src = _pane(canvas, 0)
    hit = None
    for t in src["tabs"]:
        if t["id"] == tab_id:
            hit = t
    if not hit:
        raise KeyError("标签不存在: " + str(tab_id))
    if len(src["tabs"]) <= 1:
        raise ValueError("栏内只剩一个标签，无法分屏")
    src["tabs"] = [t for t in src["tabs"] if t["id"] != tab_id]
    if src["active"] == tab_id:
        src["active"] = src["tabs"][0]["id"]
    new_pane = {"type": "pane", "id": "p" + uuid.uuid4().hex[:6],
                "tabs": [hit], "active": hit["id"]}
    r = _norm_ratio(ratio)
    if side == "left":
        canvas["layout"] = {"type": "split", "dir": "row", "ratio": 1 - r,
                            "children": [new_pane, src]}
    else:
        canvas["layout"] = {"type": "split", "dir": "row", "ratio": r,
                            "children": [src, new_pane]}
    save_canvas(tid, canvas)
    return canvas["layout"]


def merge_panes(tid, from_index=1):
    """把 from_index 栏的标签全部并入另一栏（合并栏），恢复单栏。

    单栏时无操作直接返回。标签按原顺序追加，去重（同 id 只留一份）。
    """
    canvas = load_canvas(tid)
    if pane_count(canvas) < 2:
        return canvas["layout"]
    lay = canvas["layout"]
    kids = lay["children"]
    src = kids[from_index]
    dst = kids[1 - from_index]
    ids = {t["id"] for t in dst["tabs"]}
    for t in src["tabs"]:
        if t["id"] not in ids:
            dst["tabs"].append(t)
            ids.add(t["id"])
    if len(dst["tabs"]) > PANE_MAX_TABS:
        dst["tabs"] = dst["tabs"][:PANE_MAX_TABS]
    if dst["active"] not in ids:
        dst["active"] = dst["tabs"][0]["id"]
    canvas["layout"] = dst
    canvas["ratio"] = 1.0
    save_canvas(tid, canvas)
    return canvas["layout"]


def add_tab(tid, tab, index=None):
    """新建标签并激活。类型非法→ValueError；满上限→ValueError。"""
    tab = _norm_tab(tab)
    if not tab:
        raise ValueError("标签类型必须是 " + "/".join(TAB_TYPES))
    canvas = load_canvas(tid)
    pane = _pane(canvas)
    if len(pane["tabs"]) >= PANE_MAX_TABS:
        raise ValueError("标签数已达上限 %d" % PANE_MAX_TABS)
    tab["id"] = "t_" + uuid.uuid4().hex[:8]
    if isinstance(index, int) and 0 <= index <= len(pane["tabs"]):
        pane["tabs"].insert(index, tab)
    else:
        pane["tabs"].append(tab)
    pane["active"] = tab["id"]
    save_canvas(tid, canvas)
    return tab


def set_active_tab(tid, tab_id):
    """切换激活标签。id 不在当前栏→KeyError。"""
    canvas = load_canvas(tid)
    pane = _pane(canvas)
    if tab_id not in {t["id"] for t in pane["tabs"]}:
        raise KeyError("标签不存在: " + str(tab_id))
    pane["active"] = tab_id
    save_canvas(tid, canvas)
    return pane


def rename_tab(tid, tab_id, title):
    """改标签标题（不激活）。"""
    canvas = load_canvas(tid)
    pane = _pane(canvas)
    for t in pane["tabs"]:
        if t["id"] == tab_id:
            t["title"] = str(title or "")[:MAX_TITLE]
            save_canvas(tid, canvas)
            return t
    raise KeyError("标签不存在: " + str(tab_id))


def remove_tab(tid, tab_id):
    """关标签。激活权交给右侧邻居，无右侧→左侧；栏内保底留一个默认标签。

    返回 {pane, closed}：closed=True 表示关的是当前激活标签。
    """
    canvas = load_canvas(tid)
    pane = _pane(canvas)
    ids = [t["id"] for t in pane["tabs"]]
    if tab_id not in ids:
        raise KeyError("标签不存在: " + str(tab_id))
    idx = ids.index(tab_id)
    was_active = pane["active"] == tab_id
    pane["tabs"] = [t for t in pane["tabs"] if t["id"] != tab_id]
    if not pane["tabs"]:
        pane["tabs"] = default_tabs()   # 不销毁栏，回落默认标签
        pane["active"] = pane["tabs"][0]["id"]
    elif was_active:
        pane["active"] = pane["tabs"][min(idx, len(pane["tabs"]) - 1)]["id"]
    save_canvas(tid, canvas)
    return {"pane": pane, "closed": was_active}


def set_layout_meta(tid, ratio=None, asset_width=None):
    """改分屏比例 / 资产面板宽度（只传要改的那个）。

    ratio 同步写进 split 节点自身（渲染层从 layout.ratio 读，避免双源）；
    单栏时 ratio 仅作为下次分屏的默认权重保留。
    """
    canvas = load_canvas(tid)
    if ratio is not None:
        r = _norm_ratio(ratio)
        canvas["ratio"] = r
        lay = canvas.get("layout")
        if isinstance(lay, dict) and lay.get("type") == "split":
            lay["ratio"] = r
    if asset_width is not None:
        try:
            aw = int(asset_width)
        except (TypeError, ValueError):
            aw = 320
        canvas["assetWidth"] = min(ASSET_W_MAX, max(ASSET_W_MIN, aw))
    save_canvas(tid, canvas)
    return {"ratio": canvas["ratio"], "assetWidth": canvas["assetWidth"],
            "panes": pane_count(canvas)}
