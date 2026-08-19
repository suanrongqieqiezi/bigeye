#!/usr/bin/env python3
"""Python code verification tools — syntax check and structured execution.

Closes the "write → hope" loop: AI writes code, checks syntax, fixes,
runs, reads structured errors, fixes again.
"""
import os
import re
import subprocess
from .registry import register_tool
from .file_tools import _safe_path, _check_boundary


def _parse_traceback(stderr: str) -> dict:
    """Parse Python traceback into structured data the AI can act on.

    Returns {"frames": [...], "error_type": str, "error_message": str}
    Each frame: {file, line, function, source}
    """
    frames = []
    error_type = ""
    error_message = ""

    # Extract frames: File "path", line N, in func\n    source_code
    frame_pattern = re.compile(
        r'  File "(.+?)", line (\d+)(?:, in (.+?))?\n(    .+?)(?=\n  File "|\n  \S|\n\S|$)',
        re.DOTALL
    )
    for m in frame_pattern.finditer(stderr):
        source_raw = m.group(4)
        # Take only the source line, skip caret markers (lines starting with spaces then ^ or ~)
        source_lines = source_raw.strip().split('\n')
        source = source_lines[0].strip() if source_lines else ""
        frames.append({
            "file": m.group(1),
            "line": int(m.group(2)),
            "function": m.group(3) or "",
            "source": source,
        })

    # Extract final error line: ErrorType: message
    err_match = re.search(r'\n(\w+(?:Error|Warning|Exception)): (.+?)(?:\n|$)', stderr)
    if err_match:
        error_type = err_match.group(1)
        error_message = err_match.group(2).strip()

    return {
        "frames": frames,
        "error_type": error_type,
        "error_message": error_message,
    }


@register_tool(
    name="check_python",
    description="检查 Python 文件语法（不执行）。返回结构化错误（文件/行号/函数/错误信息）。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要检查的 Python 文件路径（相对或绝对）"
            }
        },
        "required": ["path"]
    }
)
def check_python(path: str):
    """Syntax-check a Python file via py_compile. No execution, no side effects."""
    try:
        full = _safe_path(path)
        allowed, err = _check_boundary(full)
        if not allowed:
            return err
        if not os.path.isfile(full):
            return {"error": f"文件不存在: {path}"}

        result = subprocess.run(
            ["py", "-m", "py_compile", full],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
            cwd=os.path.dirname(full) or ".",
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

        if result.returncode == 0:
            return {"ok": True, "path": path, "message": "语法检查通过"}

        tb = _parse_traceback(result.stderr)
        output = {
            "ok": False,
            "path": path,
            "error_type": tb["error_type"],
            "error_message": tb["error_message"],
            "frames": tb["frames"],
        }
        if tb["frames"]:
            output["hint"] = "根据 frames 中的 file/line/source 定位并修复，修复后重新调用 check_python 验证"
        else:
            output["raw_stderr"] = result.stderr.strip()[:500]
            output["hint"] = "无法解析 traceback，查看 raw_stderr 定位问题"
        return output
    except subprocess.TimeoutExpired:
        return {"error": "语法检查超时"}
    except Exception as e:
        return {"error": f"语法检查失败: {e}"}


@register_tool(
    name="run_python",
    description="执行 Python 文件。返回 stdout/stderr/exit_code，自动解析 traceback。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要执行的 Python 文件路径"
            },
            "args": {
                "type": "array",
                "description": "命令行参数（可选）",
                "items": {"type": "string"},
                "default": []
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 30",
                "default": 30
            }
        },
        "required": ["path"]
    }
)
def run_python(path: str, args: list = None, timeout: int = 30):
    """Execute a Python file and return structured output."""
    try:
        full = _safe_path(path)
        allowed, err = _check_boundary(full)
        if not allowed:
            return err
        if not os.path.isfile(full):
            return {"error": f"文件不存在: {path}"}

        cmd = ["py", full] + (args or [])
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            cwd=os.path.dirname(full) or ".",
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

        stdout = result.stdout[:8000] if result.stdout else ""
        stderr = result.stderr[:4000] if result.stderr else ""

        resp = {
            "ok": result.returncode == 0,
            "path": path,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

        if result.returncode != 0:
            tb = _parse_traceback(stderr)
            resp["error_type"] = tb["error_type"]
            resp["error_message"] = tb["error_message"]
            resp["frames"] = tb["frames"]
            if tb["frames"]:
                resp["hint"] = "根据 frames 中的 file/line/source 定位并修复，用 edit_file 修改后重新 run_python 验证"
            else:
                resp["hint"] = f"执行失败 (exit_code={result.returncode})，检查 stderr 定位问题"

        return resp
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "path": path,
            "error": f"执行超时 ({timeout}s)",
            "hint": "代码可能死循环或耗时过长，检查逻辑或增加 timeout",
        }
    except Exception as e:
        return {"error": f"执行失败: {e}"}
