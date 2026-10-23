#!/usr/bin/env python3
"""File operations for AI — read, write, search files."""
import os
import re
import sys
import threading
from .registry import register_tool, stable_local

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ stable_local：hot_reload 会 reload 本模块，模块级 threading.local() 会被重建，
# 正在跑的循环会丢掉工作区 → 相对路径退回进程 CWD（2026-09-24 实证）。
_ws = stable_local("file_tools_ws")


def set_workspace(path):
    """Set workspace for current thread. Relative paths resolve against this."""
    _ws.path = path


def set_allow_outside(allow):
    """Set whether AI may access paths outside the workspace/project root.
    Controlled by user via frontend toggle. Default False (locked to workspace)."""
    _ws.allow_outside = bool(allow)


def _get_allow_outside():
    return getattr(_ws, 'allow_outside', False)


def set_extra_roots(paths):
    """额外放开访问的目录（本任务的「产出文件夹」）。

    与 allow_outside 的区别很关键：
      allow_outside=True → 拆掉围墙（整盘可读写）；
      extra_roots=[p]    → 围墙上多开一扇门（只放开这几棵子树）。
    用户只想给任务"多一个产出目录"时不该被迫开越权，所以单列一路。
    传空列表即收回（切任务/清除时由 server 每轮重设）。
    """
    _ws.extra_roots = [p for p in (paths or []) if p]


def _get_extra_roots():
    return list(getattr(_ws, 'extra_roots', None) or [])


def _is_within_dir(full_path, dir_path):
    """Check if full_path is inside dir_path (case-insensitive on Windows).
    用 commonpath 判定父子关系，避免裸 startswith 的前缀误判
    （如 workspace 与 workspace-foo 互为前缀时误放行）。
    """
    a = os.path.normcase(os.path.abspath(full_path))
    b = os.path.normcase(os.path.abspath(dir_path))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        # 不同盘符等无法构成公共前缀 → 必然不在界内
        return False


# ── 路径句柄层（2026-10-22）──────────────────────────────────────────
# 问题：让模型手拼长绝对路径必然出错 —— 多一个反斜杠、少一层目录、中文名记错一个字、
# 结尾多一个点（Windows 静默吞掉），而报错只回"文件不存在"，模型只能瞎猜重试。
# 对策（对标 DeepSeek Harness 的 workspace+ 标签映射 / @file 相对引用）：
#   ① 给每个可写根一个短句柄：`工作区`、`产出`；
#   ② 入口侧：`产出/xxx`、`@工作区/src/a.py` 直接解析，模型不用记长路径；
#   ③ 出口侧：工具返回的路径一律换算成句柄形式（ref），模型复制回去必然解析得通；
#   ④ 写错时先归一化（引号/重复斜杠/尾点），再给"同名文件在哪"的建议。
_ALIAS_MAIN = ("工作区", "产出")


def _alias_map():
    """句柄 → 绝对目录。每次现算（工作区/产出目录都随任务变，绝不缓存）。"""
    m = {}
    ws = getattr(_ws, 'path', None)
    if ws:
        m['工作区'] = ws
        m['ws'] = ws
    for i, p in enumerate(_get_extra_roots()):
        if i == 0:
            m['产出'] = p
            m['out'] = p
            m['output'] = p
        else:
            m['产出%d' % (i + 1)] = p
    return m


def _ref_path(full):
    """绝对路径 → 模型可复制的短引用（命中工作区/产出时用句柄）。

    出口侧的唯一换算点：模型看到什么就照着写什么，不需要理解目录结构。
    """
    try:
        full = os.path.abspath(full)
        _m = _alias_map()
        for name in _ALIAS_MAIN:
            root = _m.get(name)
            if root and _is_within_dir(full, root):
                rel = os.path.relpath(full, root).replace('\\', '/')
                return name if rel == '.' else name + '/' + rel
    except Exception:
        pass
    return full


def _normalize_input(path):
    """把模型给的路径字符串洗干净。

    每一条都对应一次真实事故：
      · 引号 / 首尾空格：从资源管理器"复制路径"或自然语言里带出来的；
      · 结尾的点与空格：Windows 会静默丢弃 ⇒ 写成 "a.mp4." 实际落到 "a.mp4"，
        模型拿自己那个字符串回读却"文件不存在"；
      · 重复分隔符 "D:\\\\a\\\\b"：手拼绝对路径最常见的形态。
    不做 realpath（不解析软链），'..' 交给 abspath 折叠。
    """
    p = "" if path is None else str(path)
    p = p.strip().strip('"').strip("'").strip()
    if not p:
        return ""
    if os.name == 'nt':
        p = p.rstrip(' .')
    if p.startswith('\\\\'):
        p = '\\\\' + re.sub(r'[\\/]{2,}', '\\\\', p[2:])
    else:
        p = re.sub(r'[\\/]{2,}', lambda m: m.group(0)[0], p)
    return p


def _split_alias(p):
    """识别 '工作区/xxx'、'产出/xxx'、'@产出/xxx'。返回 (句柄, 余下部分)。

    长句柄优先匹配，避免短别名把长的切断。
    """
    s = p[1:] if p.startswith('@') else p
    amap = _alias_map()
    for alias in sorted(amap.keys(), key=len, reverse=True):
        low = s.lower()
        if low == alias.lower():
            return alias, ""
        for sep in ('/', '\\'):
            pre = alias + sep
            if low.startswith(pre.lower()):
                return alias, s[len(pre):].lstrip('/\\')
    return None, p


class AliasNotSet(ValueError):
    """用了未注册的路径句柄（如本任务还没设产出文件夹却写了 '产出/xxx'）。"""


# 保留词 → 人类可读的名字。这些词出现在路径开头时，必须当成句柄来解释，
# 绝不能悄悄退化成"工作区下的同名子目录"。
_RESERVED_HEADS = {'产出': '产出文件夹', 'out': '产出文件夹', 'output': '产出文件夹',
                   '工作区': '工作区', 'ws': '工作区'}


def _check_unset_alias(p):
    """保留词开头但该句柄当前没注册 → 直接抛错。

    不这么做的后果：'产出/a.txt' 会被当成普通相对路径，落到 <工作区>/产出/a.txt，
    文件"写成功"了但位置完全错 —— 用户和模型都以为写进了产出文件夹（静默错位）。
    """
    if not p:
        return
    head = re.split(r'[\\/]', p, 1)[0].strip().lower()
    if head not in _RESERVED_HEADS:
        return
    if head in {k.lower() for k in _alias_map()}:
        return
    if head in ('工作区', 'ws'):
        raise AliasNotSet('当前没有可用工作区，不能用 "%s/..."；请改用相对路径。' % head)
    raise AliasNotSet(
        '本任务还没有设置"产出文件夹"，所以 "%s/..." 无效。'
        '请改用 "工作区/..." 或工作区内的相对路径；'
        '如果用户确实要写到独立目录，先让用户在输入框「仅工作区」菜单里选择产出文件夹。'
        % head
    )


_SUGGEST_SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build',
                 '.idea', '.mypy_cache', '.pytest_cache', '.tox'}
_SUGGEST_MAX_DIRS = 400
_SUGGEST_MAX_FILES = 8000
_SUGGEST_MAX_NAMES = 4000
_SUGGEST_MAX_HITS = 3


def _suggest_path(missing):
    """目标不存在时，在已知根里找同名/近似项，返回可直接使用的句柄路径。

    先按**文件名完全相同**找（层级写错是最常见的手拼事故）；
    没有再退到 difflib 近似（少个字母/打错一个字），阈值 0.86、最多 1 条 ——
    宁可只给一条高把握的，也不要列一串"可能"把模型带去更远的错文件。
    """
    base = os.path.basename(missing.rstrip('\\/'))
    if not base:
        return []
    roots = []
    ws = getattr(_ws, 'path', None)
    if ws and os.path.isdir(ws):
        roots.append(ws)
    roots.extend([p for p in _get_extra_roots() if os.path.isdir(p)])
    hits, name2path, ndirs, nfiles = [], {}, 0, 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            ndirs += 1
            if ndirs > _SUGGEST_MAX_DIRS:
                break
            dirnames[:] = [d for d in dirnames if d not in _SUGGEST_SKIP]
            nfiles += len(filenames)
            if base in filenames or base in dirnames:
                ref = _ref_path(os.path.join(dirpath, base))
                if ref not in hits:
                    hits.append(ref)
                    if len(hits) >= _SUGGEST_MAX_HITS:
                        return hits
            if len(name2path) < _SUGGEST_MAX_NAMES:
                for _n in filenames:
                    name2path.setdefault(_n, os.path.join(dirpath, _n))
                for _n in dirnames:
                    name2path.setdefault(_n, os.path.join(dirpath, _n))
            if nfiles > _SUGGEST_MAX_FILES:
                break
    if not hits and name2path:
        import difflib
        cand = difflib.get_close_matches(base, list(name2path.keys()), n=1, cutoff=0.86)
        if cand:
            hits.append(_ref_path(name2path[cand[0]]))
    return hits


# ── 工作区嵌套防护（2026-10-13）──────────────────────────
# 背景：workspace 本身是 data/missions/<id>/workspace，AI 手拼该前缀当相对路径时
# 会被 join 进当前 workspace，形成 workspace/data/missions/<id>/workspace/... 嵌套。
# 对策（对标 deepseek-harness 执行端换算）：识别"missions 内部前缀"，按项目根重解析，
# 得到的就是 AI 本意指向的真实路径。其余相对路径行为不变（'data/...' 仍指工作区内）。
_MISSION_PREFIX_RE = re.compile(
    r'^\.?[/\\]?[dD]ata[/\\][mM]issions[/\\][0-9a-fA-F]{6,}'
    r'(?:[/\\][wW]orkspace(?=[/\\]|$))?[/\\]?'
)


def _safe_path(path):
    """Resolve path relative to workspace (if set), else project root.
    Always normalizes via abspath to close path traversal.

    支持路径句柄：'工作区/xxx'、'产出/xxx'、'@产出/xxx'（见 _alias_map）。
    data/missions/<id>[/workspace] 开头的相对路径按项目根解析（真实任务路径），
    不再叠加进当前 workspace —— 工作区嵌套 bug 的执行端防线。
    """
    p = _normalize_input(path)
    if not p:
        return os.path.abspath(getattr(_ws, 'path', None) or BASE_DIR)
    _alias, _rest = _split_alias(p)
    if _alias:
        _root = _alias_map()[_alias]
        return os.path.abspath(os.path.join(_root, _rest)) if _rest else os.path.abspath(_root)
    _check_unset_alias(p)
    # Windows: 'C:'（无反斜杠）是当前目录相对路径，补成 'C:\\' 才是盘符根
    if os.name == 'nt' and len(p) == 2 and p[1] == ':':
        p = p + '\\'
    if os.path.isabs(p):
        return os.path.abspath(p)
    if _MISSION_PREFIX_RE.match(p):
        # AI 给的是"项目根相对"语义的任务内部路径 → 直接按项目根解析
        return os.path.abspath(os.path.join(BASE_DIR, p))
    base = getattr(_ws, 'path', None) or BASE_DIR
    return os.path.abspath(os.path.join(base, p))


# ── 文件修改历史记录钩子 ──────────────────────────────
# write_file/edit_file 写入成功后自动调用，记录改前快照到 file_edit_history。
# 失败静默——历史记录不能阻塞主业务。

def _record_file_edit(full_path, before_content, tool, operation=None):
    """记录一次文件修改。before_content=None 表示新建文件。"""
    try:
        import hashlib, json, time as _time
        from db import get_db
        from .task_context import get_current_topic
        db = get_db()
        topic_id = get_current_topic() or ""
        if not topic_id:
            return
        after_content = None
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                after_content = f.read()
        except Exception:
            return
        if after_content is None:
            return
        after_hash = hashlib.sha256(after_content.encode("utf-8")).hexdigest()[:16]
        before_hash = None
        if before_content is not None:
            before_hash = hashlib.sha256(before_content.encode("utf-8")).hexdigest()[:16]
            if before_hash == after_hash:
                return  # 内容没变，不记录
        # 找同一文件上一条记录作为 parent（串成链）
        row = db._fetchone(
            "SELECT id FROM file_edit_history WHERE file_path = ? ORDER BY id DESC LIMIT 1",
            (full_path,)
        )
        parent_id = row["id"] if row else None
        db._execute(
            "INSERT INTO file_edit_history "
            "(topic_id, file_path, tool, turn, parent_id, before_hash, after_hash, "
            " before_content, operation, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (topic_id, full_path, tool, 0, parent_id, before_hash, after_hash,
             before_content, json.dumps(operation, ensure_ascii=False) if operation else None,
             _time.time())
        )
        db._commit()
    except Exception:
        pass


def _boundary_error(full_path, path, for_write=False):
    """Build an AI-actionable error when a path is out of bounds.

    报错里直接给出**能用的写法**（句柄 + 绝对路径），模型照抄即可 ——
    只说"超出范围"等于让它继续猜。
    """
    ws = getattr(_ws, 'path', None)
    boundary = ws or BASE_DIR
    _extras = _get_extra_roots()
    _usable = [f"{k}/ → {v}" for k, v in _alias_map().items() if k in _ALIAS_MAIN]
    parts = [
        f'路径超出允许范围: "{path}"',
        f'当前工作区: {boundary}',
    ]
    if _usable:
        parts.append("可以这样写: " + " | ".join(_usable))
    if for_write:
        parts.append('如需写入项目目录外的文件，请设置 confirm=true 重新调用')
        parts.append('或用相对路径/句柄写到允许的目录内')
    else:
        parts.append('请用 "工作区/..." 或 "产出/..." 开头的写法，或工作区内的相对路径')
    return {
        "error": "路径超出允许范围",
        "path": _ref_path(full_path) if full_path else path,
        "resolved": full_path,
        "workspace": boundary,
        "extra_roots": _extras,
        "aliases": {k: v for k, v in _alias_map().items() if k in _ALIAS_MAIN},
        "hint": " | ".join(parts[1:]),
    }


def _check_boundary(full_path, for_write=False):
    """Check path is within workspace / project root / extra roots.
    Returns (is_allowed, error_dict_or_None).

    用户可通过前端开关控制是否允许 AI 跨出工作区：
    - allow_outside=True：读写均放行（用户授权）
    - allow_outside=False（默认）：读写均限制在工作区/项目根目录内
    - 两种情况都额外放行 extra_roots（任务的产出文件夹）
    """
    if _get_allow_outside():
        return True, None
    ws = getattr(_ws, 'path', None)
    in_workspace = ws and _is_within_dir(full_path, ws)
    in_project = _is_within_dir(full_path, BASE_DIR)
    if in_workspace or in_project:
        return True, None
    for _root in _get_extra_roots():
        try:
            if _is_within_dir(full_path, _root):
                return True, None
        except (OSError, ValueError):
            continue
    return False, _boundary_error(full_path, full_path, for_write=for_write)


@register_tool(
    name="read_file",
    description="读文件或列目录。已阅文件默认返回思考+hash（不返回原文），看原文传 read_full=true。相对路径基于工作区。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件或目录路径。优先用句柄：'产出/xxx'=产出文件夹、'工作区/xxx'=工作区；也可写相对路径或绝对路径"
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（1开始），默认1",
                "default": 1
            },
            "limit": {
                "type": "integer",
                "description": "读取行数，默认100",
                "default": 100
            },
            "read_full": {
                "type": "boolean",
                "description": "已阅文件是否强制返回原文（默认false：已阅返回思考+状态）",
                "default": False
            },
            "slice": {
                "type": "integer",
                "description": "读取特定切片原文（按切片序号）。与 offset/limit 互斥。",
                "default": None
            }
        },
        "required": ["path"]
    }
)
def read_file(path: str, offset: int = 1, limit: int = 100,
              read_full: bool = False, slice: int = None):
    try:
        full = _safe_path(path)
        allowed, err = _check_boundary(full)
        if not allowed:
            return err
        if not os.path.exists(full):
            # 不存在时别只说"文件不存在"：给同名项的句柄路径，模型照抄即可，
            # 不用再靠猜层级试第二次（手拼路径最常见的失败就是少一层/多一层）。
            res = {"error": f"文件不存在: {_ref_path(full)}"}
            _sug = _suggest_path(full)
            if _sug:
                res["did_you_mean"] = _sug
                res["hint"] = "上面是同样文件名的真实位置，用它重新调用，不要自己拼路径"
            return res
        if os.path.isdir(full):
            items = os.listdir(full)
            _ref = _ref_path(full)
            return {
                "path": path,
                "ref": _ref,
                # 目录里只回文件名，再给一个可拼接的前缀：模型拼 "path_prefix + name"
                # 就是合法路径，不需要自己数层级。
                "path_prefix": (_ref if _ref.endswith('/') else _ref + '/'),
                "type": "directory",
                "items": sorted(items),
            }

        # ── CMN 透明接入：已阅文件返回思考+状态 ──
        if not read_full and slice is None:
            memory_view = _try_memory_view(full, path)
            if memory_view is not None:
                return memory_view

        # ── 切片模式：读特定切片原文 ──
        if slice is not None:
            return _read_slice(full, path, slice)

        # ── 正常分页读原文 ──
        # 即使读原文，也要设置 crystal_session（让 remember 能建桥）
        _try_set_session_for_path(full, path)

        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        start = max(0, offset - 1)
        end = min(total, start + limit)
        result = []
        for i in range(start, end):
            result.append(f"{i+1}:{lines[i].rstrip()}")
        _ref = _ref_path(full)
        header = f"[{_ref} L{start+1}-{end}/{total}]"
        return {
            "path": _ref,
            "type": "file",
            "total_lines": total,
            "content": header + "\n" + "\n".join(result),
        }
    except Exception as e:
        return {"error": f"读文件失败: {e}"}


def _try_memory_view(full_path: str, display_path: str):
    """尝试返回已阅文件的思考视图。未阅返回 None。"""
    try:
        from memory.file_crystal_store import get_store
        store = get_store()
        status = store.get_file_status(full_path)
        if not status.get("seen_before"):
            # 未阅：异步建切片（透明，不阻塞）
            _async_build_slices(full_path)
            return None

        # 已阅：返回思考+状态
        from tools.crystal_session import set_session

        # 收集所有切片 id（供 remember 建桥）
        slice_ids = [t["slice_id"] for t in status.get("thoughts", []) if t.get("slice_id")]
        # 即使没思考，也要拿切片 id 列表
        if not slice_ids:
            from memory.file_crystal_store import _row_to_dict
            import sqlite3
            conn = sqlite3.connect(store.db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id FROM file_crystals WHERE source_path=? AND layer=0 AND status='active'",
                    (full_path,)
                ).fetchall()
                slice_ids = [r["id"] for r in rows]
            finally:
                conn.close()

        set_session(path=display_path, slice_ids=slice_ids,
                    source_type=status.get("source_type", ""))

        # 格式化返回
        return _format_memory_view(display_path, status)
    except Exception as e:
        # 任何异常降级为普通读文件（不阻塞 AI）
        print(f"[file_tools] memory view 降级: {e}")
        return None


def _format_memory_view(path: str, status: dict) -> dict:
    """格式化已阅文件的思考视图返回。"""
    import time as _time
    thoughts = status.get("thoughts", [])
    changed = status.get("changed_slices", [])
    last_verified = status.get("last_verified")
    if last_verified:
        last_verified_str = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(last_verified))
    else:
        last_verified_str = "未知"

    # 构建思考摘要
    thought_lines = []
    for t in thoughts:
        stale_mark = "（已 stale）" if t.get("stale") else ""
        imp_mark = f" ⭐{t['importance']:.1f}" if t.get("importance", 0) >= 7 else ""
        thought_lines.append(
            f"  • [切片{t['slice_index']} {t.get('line_range', '')}] "
            f"{t['thought_text']}{stale_mark}{imp_mark}"
        )
    thoughts_text = "\n".join(thought_lines) if thought_lines else "  （之前没留下思考）"

    # 构建变化摘要
    if changed:
        change_lines = []
        for c in changed:
            change_lines.append(
                f"  • 切片{c['slice_index']} ({c.get('line_range', '')}) hash 变了"
            )
        changes_text = "\n".join(change_lines)
        hint = "文件部分变化。要看变化切片原文用 read_file(path, slice=N)。其他切片思考仍有效。"
    else:
        changes_text = "  （无变化）"
        hint = "文件未变。要看原文用 read_file(path, read_full=true)，看某段用 read_file(path, slice=N)。"

    return {
        "path": path,
        "type": "file_memory",
        "seen_before": True,
        "last_verified": last_verified_str,
        "hash_changed": status.get("hash_changed", False),
        "total_slices": status.get("total_slices", 0),
        "thoughts": thoughts_text,
        "changes": changes_text,
        "hint": hint,
    }


def _read_slice(full_path: str, display_path: str, slice_index: int) -> dict:
    """读取特定切片原文。"""
    try:
        from memory.file_crystal_store import get_store
        store = get_store()
        sl = store.read_file_slice(full_path, slice_index)
        if sl:
            # 设置 session（让 remember 能建桥到这个切片）
            from tools.crystal_session import set_session
            set_session(path=display_path, slice_ids=[sl["id"]],
                        source_type=sl.get("source_type", ""))
            return {
                "path": display_path,
                "type": "file_slice",
                "slice_index": slice_index,
                "slice_id": sl["id"],
                "line_range": sl.get("slice_range", ""),
                "content": sl.get("content", ""),
            }
        return {"error": f"切片 {slice_index} 不存在。用 read_file(path) 查看文件状态。"}
    except Exception as e:
        return {"error": f"读切片失败: {e}"}


def _async_build_slices(full_path: str):
    """异步建切片（不阻塞 read_file 返回）。"""
    import threading

    def _build():
        try:
            from memory.file_crystal_store import get_store
            store = get_store()
            store.build_slices_only(full_path)
        except Exception as e:
            print(f"[file_tools] async build_slices 失败: {e}")

    threading.Thread(target=_build, daemon=True).start()


def _try_set_session_for_path(full_path: str, display_path: str):
    """读原文时也尝试设置 crystal_session（让 remember 能建桥）。

    轻量查询：只拿切片 id 列表，不算 hash 变化。
    """
    try:
        from memory.file_crystal_store import get_store
        from tools.crystal_session import set_session
        import sqlite3
        store = get_store()
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id FROM file_crystals WHERE source_path=? AND layer=0 AND status='active'",
                (full_path,)
            ).fetchall()
            if rows:
                slice_ids = [r["id"] for r in rows]
                set_session(path=display_path, slice_ids=slice_ids)
        finally:
            conn.close()
    except Exception:
        pass  # 静默失败，不影响读文件


@register_tool(
    name="write_file",
    description="创建或覆盖文件。相对路径基于工作区，写入后用 read_file 验证。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径。优先用句柄：'产出/xxx'=产出文件夹、'工作区/xxx'=工作区；也可写相对路径或绝对路径"
            },
            "content": {
                "type": "string",
                "description": "完整文件内容"
            },
            "confirm": {
                "type": "boolean",
                "description": "写入工作区外的文件需设为 true",
                "default": False
            }
        },
        "required": ["path", "content"]
    }
)
def write_file(path: str, content: str, confirm: bool = False):
    try:
        full = _safe_path(path)
        allowed, err = _check_boundary(full, for_write=True)
        if not allowed and not confirm:
            return err
        # 记录改前内容（新建文件为 None）
        _before = None
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    _before = f.read()
            except Exception:
                pass
        _dir = os.path.dirname(full) or '.'
        _dir_created = not os.path.isdir(_dir)      # 父目录是新盖的：手拼路径最典型的错法
        os.makedirs(_dir, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # 写入成功后记录修改历史（失败静默，不阻塞主业务）
        _record_file_edit(full, _before, "write_file")
        # 读回验证 —— 确保落盘内容和预期一致
        verify_ok = False
        verify_msg = ""
        try:
            with open(full, "r", encoding="utf-8") as f:
                written = f.read()
            if written == content:
                verify_ok = True
            else:
                # 可能行长差异（如 \n vs \r\n），做标准化比较
                if written.replace('\r\n', '\n') == content.replace('\r\n', '\n'):
                    verify_ok = True
                    verify_msg = "行尾格式已标准化(\r\n)"
                elif written.replace('\r\n', '\n').replace('\r', '\n') == content.replace('\r\n', '\n').replace('\r', '\n'):
                    verify_ok = True
                    verify_msg = "行尾格式已标准化"
                else:
                    verify_msg = f"内容不匹配：期望{len(content)}字符，写入{len(written)}字符"
        except Exception as ve:
            verify_msg = f"读回验证失败: {ve}"

        result = {
            "success": True,
            # path 回的是句柄写法（不是模型输入的原样字符串）：下次要引用这个文件，
            # 直接照抄 path 就能解析，不用自己再拼一次前缀。
            "path": _ref_path(full),
            "resolved": full,
            "chars": len(content),
        }
        if _dir_created:
            result["note_new_dir"] = ("父目录原本不存在，已新建：" + _ref_path(_dir)
                                      + "（若这不是你的本意，检查路径是否多写了一层）")
        if verify_ok:
            result["verified"] = True
            if verify_msg:
                result["note"] = verify_msg
        else:
            result["verified"] = False
            result["note"] = verify_msg or "写入后验证未通过"
        return result
    except Exception as e:
        return {"error": f"写文件失败: {e}"}


# ── grep 扫描策略（修复：不再按文件数 30 截断，改为全量收集候选 + 只截断命中结果）──
_GREP_SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist',
                   'build', '.idea', '.mypy_cache', '.pytest_cache', '.tox', '.hg', '.svn'}
_GREP_BIN_EXTS = {'.pyc', '.pyo', '.so', '.dll', '.dylib', '.exe', '.bin', '.o', '.a', '.lib',
                  '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.svgz',
                  '.zip', '.gz', '.tar', '.7z', '.rar', '.whl', '.pyz',
                  '.db', '.sqlite', '.sqlite3', '.pdf', '.docx', '.xlsx',
                  '.mp3', '.mp4', '.wav', '.woff', '.woff2', '.ttf', '.eot', '.class'}
_GREP_MAX_FILE_BYTES = 2 * 1024 * 1024   # 单文件上限 2MB，跳过超大文件
_GREP_MAX_SCAN_FILES = 20000             # 候选文件上限（极端防护，正常目录远达不到）
GREP_MAX_RESULTS = 30                    # 命中条数上限


def _collect_scan_files(root):
    """递归收集可搜索文件（全量，不按文件数截断）。
    返回 (files, truncated_scan)：truncated_scan=True 表示撞到 _GREP_MAX_SCAN_FILES 上限。"""
    files = []
    for dirpath, dirs, fnames in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _GREP_SKIP_DIRS]
        for fn in fnames:
            if os.path.splitext(fn)[1].lower() in _GREP_BIN_EXTS:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > _GREP_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(fp)
            if len(files) >= _GREP_MAX_SCAN_FILES:
                return files, True
    return files, False


@register_tool(
    name="grep",
    description="在文件中搜索文本。支持正则表达式，用于找代码、找配置、找任何内容。",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "搜索的文本或正则表达式"
            },
            "path": {
                "type": "string",
                "description": "搜索路径：文件、目录、或 glob 模式如 'tools/*.py'"
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "是否区分大小写，默认否",
                "default": False
            },
            "max_results": {
                "type": "integer",
                "description": "命中条数上限，默认 30（只截断结果，不再按文件数截断导致漏搜）",
                "default": 30
            }
        },
        "required": ["pattern", "path"]
    }
)
def grep(pattern: str, path: str, case_sensitive: bool = False, max_results: int = GREP_MAX_RESULTS):
    try:
        import glob as glob_mod
        full = _safe_path(path)
        # Boundary check: for globs, check the directory part
        check_path = full
        if not os.path.exists(full) and ('*' in full or '?' in full or '[' in full):
            check_path = os.path.dirname(full) or '.'
        allowed, err = _check_boundary(os.path.abspath(check_path))
        if not allowed:
            return err

        search_path = full
        files = []
        truncated_scan = False
        if os.path.isfile(search_path):
            files = [search_path]
        elif os.path.isdir(search_path):
            # 全量收集候选文件（修复：原实现只走到第 30 个文件就 break，
            # 内容排在后面的文件被漏搜 → 假阴性"没找到"）
            files, truncated_scan = _collect_scan_files(search_path)
        else:
            # Glob pattern：全量匹配（不再 [:30] 截断），仅按命中数截断结果
            matched = glob_mod.glob(search_path)
            txt_exts = {'.py', '.c', '.h', '.txt', '.md', '.json', '.xml', '.html', '.js', '.ts', '.css', '.yaml', '.yml', '.ini', '.cfg', '.conf', '.sh', '.bat'}
            matched.sort(key=lambda x: (os.path.splitext(x)[1] not in txt_exts, x))
            files = matched

        flag = 0 if case_sensitive else re.IGNORECASE
        results = []
        hit_limit = False
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(pattern, line, flag):
                            # 结果里的文件标识必须是"模型能当参数再喂回来"的写法：
                            # 工作区/产出内的给句柄，其余给绝对路径 —— 原来的
                            # relpath(fp, BASE_DIR) 是项目根相对路径，模型照抄当参数
                            # 会被按工作区解析，必然找不到（当年就踩过）。
                            rel = _ref_path(fp)
                            results.append(f"{rel}:{i}:{line.rstrip()[:200]}")
                            if len(results) >= max_results:
                                hit_limit = True
                                break
                if hit_limit:
                    break
            except Exception:
                continue
        if not results:
            return {"error": f"在 {path} 中没找到 '{pattern}'"}
        out = {"matches": len(results), "results": results, "pattern": pattern,
               "scanned_files": len(files)}
        if hit_limit:
            out["truncated"] = True
            out["hint"] = f"命中已达上限 {max_results} 条，结果被截断；可传 max_results 调大或缩小 path 范围"
        if truncated_scan:
            out["truncated_scan"] = True
            out["hint"] = ((out.get("hint", "") + " ").strip() +
                           f" 扫描文件数达上限 {_GREP_MAX_SCAN_FILES}，覆盖可能不全").strip()
        return out
    except Exception as e:
        return {"error": f"搜索失败: {e}"}


# ── 文件修改历史回溯工具 ──────────────────────────────

@register_tool(
    name="file_history",
    description="查看某文件的修改历史链（write_file/edit_file 自动记录）。返回每次修改的记录id、工具、时间、hash。用 file_restore 可恢复任意版本。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对或绝对）"
            },
            "limit": {
                "type": "integer",
                "description": "最多返回几条（默认20，从新到旧）",
                "default": 20
            }
        },
        "required": ["path"]
    }
)
def file_history(path: str, limit: int = 20):
    try:
        from db import get_db
        import time as _time
        full = _safe_path(path)
        db = get_db()
        rows = db._fetchall(
            "SELECT id, tool, before_hash, after_hash, created_at "
            "FROM file_edit_history WHERE file_path = ? ORDER BY id DESC LIMIT ?",
            (full, int(limit))
        )
        if not rows:
            return {"path": full, "history": [], "note": "无修改记录（只记录 write_file/edit_file 的修改）"}
        history = []
        for r in rows:
            history.append({
                "record_id": r["id"],
                "tool": r["tool"],
                "time": _time.strftime("%m-%d %H:%M", _time.localtime(r["created_at"])),
                "before_hash": r["before_hash"] or "(新建)",
                "after_hash": r["after_hash"],
            })
        return {"path": full, "history": history,
                "hint": "file_restore(path, record_id) 可恢复到该记录之前的状态"}
    except Exception as e:
        return {"error": f"查询历史失败: {e}"}


@register_tool(
    name="file_restore",
    description="把文件恢复到某次修改之前的状态（用 file_history 查 record_id）。恢复前会先记录当前内容，可再次恢复回来。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对或绝对）"
            },
            "record_id": {
                "type": "integer",
                "description": "file_history 返回的记录id，恢复到该记录之前的内容"
            }
        },
        "required": ["path", "record_id"]
    }
)
def file_restore(path: str, record_id: int):
    try:
        from db import get_db
        full = _safe_path(path)
        db = get_db()
        row = db._fetchone(
            "SELECT before_content, before_hash FROM file_edit_history WHERE id = ? AND file_path = ?",
            (int(record_id), full)
        )
        if not row:
            return {"error": f"记录 {record_id} 不存在或不属于该文件，先用 file_history 查看"}
        if row["before_content"] is None:
            return {"error": "该记录是文件创建操作，之前没有内容可恢复。如需删除文件请另行处理。"}
        # 记录当前内容（恢复前的快照），以便恢复错了还能回来
        _cur = None
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    _cur = f.read()
            except Exception:
                pass
        os.makedirs(os.path.dirname(full) or '.', exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(row["before_content"])
        _record_file_edit(full, _cur, "file_restore",
                          operation={"restore_from_record": int(record_id)})
        return {"result": "ok", "path": full,
                "restored_to_before_record": int(record_id),
                "note": "已记录恢复前内容，如需撤销可再查 file_history"}
    except Exception as e:
        return {"error": f"恢复失败: {e}"}
