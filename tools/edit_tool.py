#!/usr/bin/env python3
"""
Edit engine — line-level / fuzzy / patch editing for code files.
Replaces the old write_file-only approach with precise surgical edits.

Designed after OMPQ's edit engine (Node/TS), translated to Python with
progressive fallback chain and LSP hook points.

P0 — Core edit engine for 大眼X融合增强计划
"""
import difflib
import os
import re
import threading
from typing import Literal, Optional

from .registry import register_tool
from .file_tools import _safe_path, _check_boundary, _record_file_edit

# ── Types ─────────────────────────────────────────────

EditOpType = Literal["lines", "fuzzy", "regex", "patch"]


class EditOp:
    """Single edit operation."""
    __slots__ = ("op_type", "old_text", "new_text", "line_start", "line_end",
                  "context_before", "context_after", "old_start_line")

    def __init__(
        self,
        op_type: EditOpType,
        old_text: str = "",
        new_text: str = "",
        line_start: Optional[int] = None,
        line_end: Optional[int] = None,
        context_before: Optional[str] = None,
        context_after: Optional[str] = None,
        old_start_line: Optional[int] = None,
    ):
        self.op_type = op_type
        self.old_text = old_text
        self.new_text = new_text
        self.line_start = line_start
        self.line_end = line_end
        self.context_before = context_before
        self.context_after = context_after
        self.old_start_line = old_start_line


class EditResult:
    """Result of applying an edit."""
    __slots__ = ("success", "new_content", "changed_lines", "fallback_level",
                  "message", "diff")

    def __init__(self, success: bool, new_content: str = "",
                 changed_lines: list = None, fallback_level: int = 0,
                 message: str = "", diff: str = ""):
        self.success = success
        self.new_content = new_content
        self.changed_lines = changed_lines or []
        self.fallback_level = fallback_level
        self.message = message
        self.diff = diff


# ── Normalization ────────────────────────────────────

def _normalize_line(line: str) -> str:
    """Normalize line for fuzzy matching: strip trailing whitespace."""
    return line.rstrip()


def _normalize_for_fuzzy(text: str) -> str:
    """Aggressive normalization: lowercase, collapse whitespace, strip."""
    text = text.lower()
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _strip_comments(text: str) -> str:
    """Remove common comment markers for comment-blind matching."""
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        line = re.sub(r'^\s*//.*$', '', line)  # C-style
        line = re.sub(r'^\s*#.*$', '', line)    # Python
        line = re.sub(r'^\s*--.*$', '', line)   # SQL
        if line.strip():
            cleaned.append(line)
    return '\n'.join(cleaned)


def _compute_similarity(a: str, b: str) -> float:
    """Compute string similarity ratio (0-1)."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def _generate_diff(old_lines: list, new_lines: list) -> str:
    """Generate a unified diff string."""
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile='original', tofile='modified',
        lineterm=''
    )
    return '\n'.join(diff)


# ── Fallback Chain ───────────────────────────────────

class EditEngine:
    """
    Edit engine with progressive fallback (10 levels).

    Level 1:  Exact line number match
    Level 2:  Single line content match (trim + normalize)
    Level 3:  Context 2 lines (fuzzy)
    Level 4:  Context 4 lines
    Level 5:  Comment-stripped match
    Level 6:  Whitespace-agnostic match
    Level 7:  Small range search (5 lines)
    Level 8:  Large range search (entire function/block)
    Level 9:  Prompt user for confirmation
    Level 10: Fallback to content find (last resort)
    """

    def __init__(self, file_path: str, content: str = None):
        self.file_path = file_path
        self._lines = None
        if content is not None:
            self._lines = content.split('\n')

    def _read_lines(self) -> list:
        if self._lines is not None:
            return self._lines
        full = _safe_path(self.file_path)
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            # 标准化行尾：\r\n → \n，避免与 AI 提供的 old_text 行尾不一致导致匹配失败
            content = f.read().replace('\r\n', '\n').replace('\r', '\n')
        self._lines = content.split('\n')
        return self._lines

    def apply(self, ops: list[EditOp]) -> EditResult:
        lines = self._read_lines()
        current_lines = list(lines)
        changed = []
        max_level = 0

        # Apply ops in reverse order (bottom-up) to keep line numbers stable
        sorted_ops = sorted(
            enumerate(ops),
            key=lambda x: (x[1].line_start or 0) if x[1].op_type != 'patch' else 0,
            reverse=True
        )

        for op_idx, op in sorted_ops:
            level, result_lines, change_info = self._apply_one(current_lines, op, len(ops))
            max_level = max(max_level, level)
            if change_info:
                changed.append(change_info)
            current_lines = result_lines

        if not changed:
            return EditResult(
                success=False,
                message="No changes were applied.",
                fallback_level=max_level
            )

        new_content = '\n'.join(current_lines)
        diff_str = _generate_diff(lines, current_lines)

        return EditResult(
            success=True,
            new_content=new_content,
            changed_lines=changed,
            fallback_level=max_level,
            message=f"Applied {len(changed)} edit(s) (max fallback level: {max_level})",
            diff=diff_str
        )

    def _apply_one(self, lines: list, op: EditOp, total_ops: int) -> tuple:
        """Apply a single edit op with fallback chain. Returns (level, new_lines, change_info)."""
        if op.op_type == "lines":
            return self._apply_lines(lines, op)
        elif op.op_type == "regex":
            return self._apply_regex(lines, op)
        elif op.op_type == "patch":
            return self._apply_patch(lines, op)
        elif op.op_type == "fuzzy":
            return self._apply_fuzzy(lines, op)
        return (0, lines, None)

    def _apply_lines(self, lines: list, op: EditOp) -> tuple:
        """Exact line number match (Level 1)."""
        start = op.line_start
        end = op.line_end

        if start is None:
            return (0, lines, None)

        # Convert to 0-indexed
        start_idx = start - 1
        end_idx = end - 1 if end is not None else start_idx

        if start_idx < 0 or end_idx >= len(lines) or start_idx > end_idx:
            return (0, lines, None)

        # Level 1: exact match
        if op.old_text:
            old_text = op.old_text.rstrip('\n')
            actual_text = '\n'.join(lines[start_idx:end_idx + 1]).rstrip('\n')
            if old_text == actual_text:
                new_lines = list(lines)
                replacement = op.new_text.split('\n') if op.new_text else ['']
                new_lines[start_idx:end_idx + 1] = replacement
                return (1, new_lines, {
                    'op': 'replace', 'level': 1, 'start': start, 'end': end
                })

        # Level 2: single line content match (trim + normalize)
        if op.old_text:
            old_lines = op.old_text.split('\n')
            actual_slice = lines[start_idx:end_idx + 1]
            if len(old_lines) == len(actual_slice):
                all_match = True
                for o, a in zip(old_lines, actual_slice):
                    if _normalize_line(o) != _normalize_line(a):
                        all_match = False
                        break
                if all_match:
                    new_lines = list(lines)
                    replacement = op.new_text.split('\n') if op.new_text else ['']
                    new_lines[start_idx:end_idx + 1] = replacement
                    return (2, new_lines, {
                        'op': 'replace', 'level': 2, 'start': start, 'end': end
                    })

        # If line numbers are off but we have old_text, try to find it
        if op.old_text:
            return self._find_and_replace(lines, op)

        # Fallback: just replace by line number anyway
        new_lines = list(lines)
        replacement = op.new_text.split('\n') if op.new_text else ['']
        new_lines[start_idx:end_idx + 1] = replacement
        return (2, new_lines, {'op': 'replace', 'level': 2, 'start': start, 'end': end})

    def _apply_regex(self, lines: list, op: EditOp) -> tuple:
        """Regex replacement (Level 3-4)."""
        content = '\n'.join(lines)
        try:
            new_content = re.sub(op.old_text, op.new_text, content)
            if new_content != content:
                return (3, new_content.split('\n'), {
                    'op': 'regex', 'level': 3, 'pattern': op.old_text
                })
        except re.error:
            pass
        return (3, lines, None)

    def _apply_fuzzy(self, lines: list, op: EditOp) -> tuple:
        """Fuzzy context-based match (Levels 5-8)."""
        return self._find_and_replace(lines, op)

    def _apply_patch(self, lines: list, op: EditOp) -> tuple:
        """Apply unified diff patch (Level 4)."""
        if not op.old_text:
            return (0, lines, None)

        content = '\n'.join(lines)
        new_content = self._apply_unified_diff(content, op.old_text)
        if new_content is not None:
            return (4, new_content.split('\n'), {'op': 'patch', 'level': 4})
        return (4, lines, None)

    def _find_and_replace(self, lines: list, op: EditOp) -> tuple:
        """Progressive fallback search for old_text in lines (Levels 3-8)."""
        old_text = op.old_text
        if not old_text:
            return (0, lines, None)

        new_text = op.new_text
        content = '\n'.join(lines)
        old_normal = old_text.rstrip('\n')
        old_lines = old_normal.split('\n')
        n = len(old_lines)

        # ── Level 3: Context 2 lines (fuzzy) ──
        match = self._fuzzy_search(lines, old_lines, n, 2)
        if match is not None:
            return self._do_replace(lines, match, match + n, new_text, 3)

        # ── Level 4: Context 4 lines ──
        match = self._fuzzy_search(lines, old_lines, n, 4)
        if match is not None:
            return self._do_replace(lines, match, match + n, new_text, 4)

        # ── Level 5: Comment-stripped match ──
        match = self._fuzzy_search(lines, old_lines, n, 0, strip_comments=True)
        if match is not None:
            return self._do_replace(lines, match, match + n, new_text, 5)

        # ── Level 6: Whitespace-agnostic match ──
        match = self._whitespace_agnostic_search(lines, old_lines, n)
        if match is not None:
            return self._do_replace(lines, match, match + n, new_text, 6)

        # ── Level 7: Small range search ──
        match = self._range_search(lines, old_lines, n, range_size=5)
        if match is not None:
            return self._do_replace(lines, match, match + n, new_text, 7)

        # ── Level 8: Large range search ──
        match = self._range_search(lines, old_lines, n, range_size=50)
        if match is not None:
            return self._do_replace(lines, match, match + n, new_text, 8)

        # ── Level 10: Full content search (last resort) ──
        return self._full_content_search(content, old_normal, new_text, lines)

    def _fuzzy_search(self, lines: list, old_lines: list, n: int,
                      context_lines: int = 0,
                      strip_comments: bool = False,
                      threshold: float = 0.85) -> Optional[int]:
        """Fuzzy search for a block of lines. Returns start index or None."""
        if n > len(lines):
            return None

        best_start = None
        best_score = 0

        for start in range(len(lines) - n + 1):
            window = lines[start:start + n]

            if strip_comments:
                window_clean = _strip_comments('\n'.join(window)).split('\n')
                old_clean = _strip_comments('\n'.join(old_lines)).split('\n')
                if len(window_clean) != len(old_clean):
                    continue
                score = sum(
                    _compute_similarity(_normalize_for_fuzzy(w), _normalize_for_fuzzy(o))
                    for w, o in zip(window_clean, old_clean)
                ) / len(window_clean)
            else:
                score = sum(
                    _compute_similarity(_normalize_line(w), _normalize_line(o))
                    for w, o in zip(window, old_lines)
                ) / len(window)

            if score > best_score:
                best_score = score
                best_start = start

        if best_start is not None and best_score >= threshold:
            return best_start
        return None

    def _whitespace_agnostic_search(self, lines: list, old_lines: list, n: int) -> Optional[int]:
        """Search ignoring all whitespace differences."""
        if n > len(lines):
            return None

        for start in range(len(lines) - n + 1):
            window = lines[start:start + n]
            match = True
            for w, o in zip(window, old_lines):
                if _normalize_for_fuzzy(w) != _normalize_for_fuzzy(o):
                    match = False
                    break
            if match:
                return start
        return None

    def _range_search(self, lines: list, old_lines: list, n: int, range_size: int) -> Optional[int]:
        """Search in a sliding window of given range size."""
        if n > len(lines):
            return None

        step = max(1, range_size // 2)
        for search_start in range(0, max(1, len(lines) - n + 1), step):
            search_end = min(len(lines) - n + 1, search_start + range_size)
            for start in range(search_start, search_end):
                window = lines[start:start + n]
                if all(_normalize_line(w) == _normalize_line(o)
                       for w, o in zip(window, old_lines)):
                    return start
        return None

    def _full_content_search(self, content: str, old_normal: str,
                             new_text: str, lines: list) -> tuple:
        """Search entire content string for the old text (Level 10)."""
        # 标准化 old_text 行尾，与 content（已标准化）一致
        old_clean = old_normal.replace('\r\n', '\n').replace('\r', '\n')
        idx = content.find(old_clean)
        if idx >= 0:
            prefix = content[:idx]
            start_line = prefix.count('\n')
            new_lines = list(lines)
            replacement = new_text.split('\n') if new_text else ['']
            new_lines[start_line:start_line + len(old_clean.split('\n'))] = replacement
            return (10, new_lines, {'op': 'replace', 'level': 10, 'start': start_line + 1})

        # Even more aggressive: try line-by-line find by stripped content
        old_line_list = old_clean.split('\n')
        for start in range(len(lines) - len(old_line_list) + 1):
            window = lines[start:start + len(old_line_list)]
            if all(w.strip() == o.strip() for w, o in zip(window, old_line_list)):
                new_lines = list(lines)
                replacement = new_text.split('\n') if new_text else ['']
                new_lines[start:start + len(old_line_list)] = replacement
                return (10, new_lines, {'op': 'replace', 'level': 10, 'start': start + 1})

        return (10, lines, None)

    def _do_replace(self, lines: list, start: int, end: int,
                    new_text: str, level: int) -> tuple:
        """Perform the actual line replacement."""
        new_lines = list(lines)
        replacement = new_text.split('\n') if new_text else ['']
        new_lines[start:end] = replacement
        return (level, new_lines, {
            'op': 'replace', 'level': level, 'start': start + 1, 'end': end
        })

    @staticmethod
    def _apply_unified_diff(content: str, patch_text: str) -> Optional[str]:
        """Apply a unified diff to content."""
        try:
            from patch import fromstring
            patchset = fromstring(patch_text)
            if patchset:
                result = patchset.apply(content)
                return result
        except (ImportError, Exception):
            pass
        return None

    def write_result(self, result: EditResult) -> dict:
        """Write edit result to file and return output dict."""
        if not result.success:
            return {"error": result.message}

        full = _safe_path(self.file_path)
        os.makedirs(os.path.dirname(os.path.abspath(full)), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(result.new_content)
            if not result.new_content.endswith('\n'):
                f.write('\n')

        return {
            "result": "ok",
            "path": self.file_path,
            "changed_lines": result.changed_lines,
            "fallback_level": result.fallback_level,
            "message": result.message,
            "diff": result.diff,
        }


# ════════════════════════════════════════════════════════
# Tool Registration
# ════════════════════════════════════════════════════════

@register_tool(
    name="edit_file",
    description="精确编辑文件，支持行级/模糊/正则/patch四种模式。建议从fuzzy开始，失败自动降级。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要编辑的文件路径（相对或绝对）"
            },
            "operations": {
                "type": "array",
                "description": "编辑操作列表，按行号逆序执行（从下往上，保持行号稳定）",
                "items": {
                    "type": "object",
                    "properties": {
                        "op_type": {
                            "type": "string",
                            "enum": ["lines", "fuzzy", "regex", "patch"],
                            "description": "lines=按行号替换(需line_start/line_end,old_text验证内容); fuzzy=模糊匹配(提供old_text和new_text); regex=正则替换(old_text=模式); patch=统一diff(old_text=diff内容)"
                        },
                        "old_text": {
                            "type": "string",
                            "description": "要替换的旧文本"
                        },
                        "new_text": {
                            "type": "string",
                            "description": "替换后的新文本"
                        },
                        "line_start": {
                            "type": "integer",
                            "description": "起始行号（1-indexed，lines 模式必需）"
                        },
                        "line_end": {
                            "type": "integer",
                            "description": "结束行号（1-indexed，lines 模式可选，默认单行）"
                        }
                    },
                    "required": ["op_type"],
                    "optional": ["old_text", "new_text", "line_start", "line_end"]
                }
            }
        },
        "required": ["path", "operations"]
    }
)
def edit_file(path: str, operations: list):
    """Edit a file with one or more operations."""
    try:
        full = _safe_path(path)
        allowed, err = _check_boundary(full, for_write=True)
        if not allowed:
            return err
        if not os.path.exists(full):
            return {"error": f"文件不存在: {path}"}
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            # 标准化行尾，避免 \r\n 导致匹配失败
            content = f.read().replace('\r\n', '\n').replace('\r', '\n')

        engine = EditEngine(path, content)
        ops = []
        for op in operations:
            ops.append(EditOp(
                op_type=op.get("op_type", "fuzzy"),
                old_text=op.get("old_text", ""),
                new_text=op.get("new_text", ""),
                line_start=op.get("line_start"),
                line_end=op.get("line_end"),
            ))

        result = engine.apply(ops)
        out = engine.write_result(result)
        # 写入成功后记录修改历史（失败静默，不阻塞主业务）
        if isinstance(out, dict) and out.get("result") == "ok":
            _record_file_edit(_safe_path(path), content, "edit_file", operation=operations)
        return out

    except Exception as e:
        return {"error": f"编辑失败: {e}"}


@register_tool(
    name="edit_preview",
    description="预览编辑效果但不写入文件。参数与 edit_file 相同，返回 diff 预览。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径"
            },
            "operations": {
                "type": "array",
                "description": "编辑操作列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "op_type": {"type": "string", "enum": ["lines", "fuzzy", "regex", "patch"]},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "line_start": {"type": "integer"},
                        "line_end": {"type": "integer"},
                    },
                    "required": ["op_type"]
                }
            }
        },
        "required": ["path", "operations"]
    }
)
def edit_preview(path: str, operations: list):
    """Preview edit without writing to file."""
    try:
        full = _safe_path(path)
        allowed, err = _check_boundary(full)
        if not allowed:
            return err
        if not os.path.exists(full):
            return {"error": f"文件不存在: {path}"}
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            # 标准化行尾，避免 \r\n 导致匹配失败
            content = f.read().replace('\r\n', '\n').replace('\r', '\n')

        engine = EditEngine(path, content)
        ops = []
        for op in operations:
            ops.append(EditOp(
                op_type=op.get("op_type", "fuzzy"),
                old_text=op.get("old_text", ""),
                new_text=op.get("new_text", ""),
                line_start=op.get("line_start"),
                line_end=op.get("line_end"),
            ))

        result = engine.apply(ops)
        if result.success:
            return {
                "result": "preview",
                "path": path,
                "fallback_level": result.fallback_level,
                "message": result.message,
                "diff": result.diff,
                "changed_lines": result.changed_lines,
                "note": "这是预览，未写入。确认后调用 edit_file 写入。"
            }
        return {
            "result": "preview_failed",
            "message": result.message,
            "fallback_level": result.fallback_level,
        }

    except Exception as e:
        return {"error": f"预览失败: {e}"}
