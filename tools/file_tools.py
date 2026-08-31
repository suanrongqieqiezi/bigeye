#!/usr/bin/env python3
"""File operations for AI — read, write, search files."""
import os
import re
import sys
import threading
from .registry import register_tool

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ws = threading.local()


def set_workspace(path):
    """Set workspace for current thread. Relative paths resolve against this."""
    _ws.path = path


def set_allow_outside(allow):
    """Set whether AI may access paths outside the workspace/project root.
    Controlled by user via frontend toggle. Default False (locked to workspace)."""
    _ws.allow_outside = bool(allow)


def _get_allow_outside():
    return getattr(_ws, 'allow_outside', False)


def _is_within_dir(full_path, dir_path):
    """Check if full_path is inside dir_path (case-insensitive on Windows)."""
    a = os.path.abspath(full_path)
    b = os.path.abspath(dir_path)
    if os.name == 'nt':
        return os.path.normcase(a).startswith(os.path.normcase(b))
    return a.startswith(b)


def _safe_path(path):
    """Resolve path relative to workspace (if set), else project root.
    Always normalizes via abspath to close path traversal."""
    # Windows: 'C:'（无反斜杠）是当前目录相对路径，补成 'C:\' 才是盘符根
    if os.name == 'nt' and len(path) == 2 and path[1] == ':':
        path = path + '\\'
    if os.path.isabs(path):
        return os.path.abspath(path)
    base = getattr(_ws, 'path', None) or BASE_DIR
    return os.path.abspath(os.path.join(base, path))


# ── 文件修改历史记录钩子 ──────────────────────────────
# write_file/edit_file 写入成功后自动调用，记录改前快照到 file_edit_history。
# 失败静默——历史记录不能阻塞主业务。

def _record_file_edit(full_path, before_content, tool, operation=None):
    """记录一次文件修改。before_content=None 表示新建文件。"""
    try:
        import hashlib, json, time as _time
        from db import get_db
        db = get_db()
        topic_id = db.get_active_topic_id() or ""
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
    """Build an AI-actionable error when a path is out of bounds."""
    ws = getattr(_ws, 'path', None)
    boundary = ws or BASE_DIR
    parts = [
        f'路径超出允许范围: "{path}" → "{full_path}"',
        f'当前工作区: {boundary}',
    ]
    if for_write:
        parts.append('如需写入项目目录外的文件，请设置 confirm=true 重新调用')
        parts.append('或用相对路径写到工作区内')
    else:
        parts.append('请使用工作区内的相对路径访问文件')
        parts.append('如需访问外部目录，请先通过 /api/workspace 调整工作区')
    return {
        "error": "路径超出允许范围",
        "path": path,
        "resolved": full_path,
        "workspace": boundary,
        "hint": " | ".join(parts[1:]),
    }


def _check_boundary(full_path, for_write=False):
    """Check path is within workspace or project root.
    Returns (is_allowed, error_dict_or_None).

    用户可通过前端开关控制是否允许 AI 跨出工作区：
    - allow_outside=True：读写均放行（用户授权）
    - allow_outside=False（默认）：读写均限制在工作区/项目根目录内
    """
    if _get_allow_outside():
        return True, None
    ws = getattr(_ws, 'path', None)
    in_workspace = ws and _is_within_dir(full_path, ws)
    in_project = _is_within_dir(full_path, BASE_DIR)
    if in_workspace or in_project:
        return True, None
    return False, _boundary_error(full_path, full_path, for_write=for_write)


@register_tool(
    name="read_file",
    description="读文件或列目录。已阅文件默认返回思考+hash（不返回原文），看原文传 read_full=true。相对路径基于工作区。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件或目录路径。相对路径基于工作区，绝对路径直接用"
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
            return {"error": f"文件不存在: {path}"}
        if os.path.isdir(full):
            items = os.listdir(full)
            return {
                "path": path,
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
        header = f"[{path} L{start+1}-{end}/{total}]"
        return {
            "path": path,
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
                "description": "文件路径，相对路径基于工作区，绝对路径直接用"
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
        os.makedirs(os.path.dirname(full) or '.', exist_ok=True)
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
            "path": path,
            "resolved": full,
            "chars": len(content),
        }
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
            }
        },
        "required": ["pattern", "path"]
    }
)
def grep(pattern: str, path: str, case_sensitive: bool = False):
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
        if os.path.isfile(search_path):
            files = [search_path]
        elif os.path.isdir(search_path):
            # Walk directory
            for root, dirs, fnames in os.walk(search_path):
                for fn in fnames:
                    files.append(os.path.join(root, fn))
                    if len(files) >= 30:
                        break
                if len(files) >= 30:
                    break
        else:
            # Glob pattern
            matched = glob_mod.glob(search_path)
            # Filter down to 30 matches, preferring text files
            txt_exts = {'.py', '.c', '.h', '.txt', '.md', '.json', '.xml', '.html', '.js', '.ts', '.css', '.yaml', '.yml', '.ini', '.cfg', '.conf', '.sh', '.bat'}
            matched.sort(key=lambda x: (os.path.splitext(x)[1] not in txt_exts, x))
            files = matched[:30]

        flag = 0 if case_sensitive else re.IGNORECASE
        results = []
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(pattern, line, flag):
                            rel = os.path.relpath(fp, BASE_DIR)
                            results.append(f"{rel}:{i}:{line.rstrip()[:200]}")
                            if len(results) >= 30:
                                break
                if len(results) >= 30:
                    break
            except Exception:
                continue
        if not results:
            return {"error": f"在 {path} 中没找到 '{pattern}'"}
        return {"matches": len(results), "results": results, "pattern": pattern}
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
