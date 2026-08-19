#!/usr/bin/env python3
"""Bash execution tool for AI — with safety guardrails.

Dangerous operations require explicit confirmation.
The AI must acknowledge risk before destructive commands execute.
"""
import subprocess
import os
import re
import time
import threading
import queue
from collections import deque
from .registry import register_tool

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ws = threading.local()
_ctx = threading.local()   # runtime context: on_output callback, etc.


def set_workspace(path):
    """Set workspace for current thread. Used as default cwd when workdir is empty."""
    _ws.path = path


def set_allow_outside(allow):
    """Set whether AI may run commands with workdir outside the workspace.
    Controlled by user via frontend toggle. Default False."""
    _ws.allow_outside = bool(allow)


def _get_allow_outside():
    return getattr(_ws, 'allow_outside', False)


# ── Runtime context (for streaming output to UI) ──

def set_runtime_context(on_output=None, tool_id=None):
    """Set per-call runtime context.

    on_output: callable(tool_id, stream_type, chunk)
      - stream_type: 'stdout' | 'stderr' | 'begin' | 'end'
      - chunk: str (the content)
      Called on every line/block of output so the UI can stream updates.
    tool_id: identifier for this specific tool invocation
    """
    _ctx.on_output = on_output
    _ctx.tool_id = tool_id


def _emit(stream_type, chunk):
    """Fire on_output callback if set."""
    cb = getattr(_ctx, 'on_output', None)
    tid = getattr(_ctx, 'tool_id', None)
    if cb:
        try:
            cb(tid, stream_type, chunk)
        except Exception:
            pass


# ── Dangerous patterns that ALWAYS require confirmation ──
DANGEROUS_PATTERNS = [
    (r'\brm\s+-rf?\b', '递归删除文件'),
    (r'\brmdir\b', '删除目录'),
    (r'\bdel\s+/[fsq]', '强制删除文件'),
    (r'\bdd\s+if=', '磁盘写入'),
    (r'\bmkfs\.', '格式化文件系统'),
    (r'>\s*/dev/', '写入设备文件'),
    (r'\bshutdown\b', '关机'),
    (r'\breboot\b', '重启'),
    (r'\bkill\s+-9\b', '强制杀进程'),
    (r'\btaskkill\s+/[f]', '强制终止进程'),
    (r'\bformat\b', '格式化'),
    (r'\bchmod\s+777\b', '开放所有权限'),
    (r'\bcurl.*\|\s*(ba)?sh\b', '管道执行远程脚本'),
    (r'\bwget.*\|\s*(ba)?sh\b', '管道执行远程脚本'),
    (r'\bpip\s+uninstall\b', '卸载 Python 包'),
    (r'\bnpm\s+uninstall\b', '卸载 Node 包'),
    # Network dangerous
    (r'\biptables\b', '修改防火墙规则'),
    (r'\bnetsh\s+.*firewall', '修改 Windows 防火墙'),
]

# Patterns that are BLOCKED completely (never allowed)
BLOCKED_PATTERNS = [
    (r'\brm\s+-rf\s+/\b', '删除根目录'),
    (r'\brm\s+-rf\s+~\b', '删除用户目录'),
    (r'\brm\s+-rf\s+\$HOME\b', '删除 HOME'),
    (r'\bdel\s+/[fs]\s+C:\\\\', '删除 C 盘'),
    (r'\bformat\s+C:', '格式化 C 盘'),
]


def _check_danger(command):
    """Check command against dangerous patterns. Returns (is_dangerous, reasons)."""
    reasons = []
    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return 'blocked', [reason]
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            reasons.append(reason)
    if reasons:
        return 'dangerous', reasons
    return 'safe', []


def _stream_reader_pipe(pipe, out_list, q, stream_name, stop_event):
    """Background reader thread: read lines from pipe into a list + queue.

    Queue is for streaming to UI; list is for final stdout/stderr aggregation.
    """
    try:
        for raw_line in pipe:
            if stop_event.is_set():
                break
            line = raw_line if isinstance(raw_line, str) else raw_line.decode('utf-8', errors='replace')
            out_list.append(line)
            q.put((stream_name, line))
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _stream_consumer_thread(q, stop_event, on_output, tool_id, out_ringbuf, max_ring_lines=200):
    """Thread that consumes lines from queue and fires on_output callback.

    Also keeps a ring buffer of last N lines so we can return them at the end.
    Decoupled from readers so slow callbacks don't block the subprocess pipes.
    """
    try:
        while not stop_event.is_set():
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                continue
            stream_name, chunk = item
            out_ringbuf.append(chunk)
            if len(out_ringbuf) > max_ring_lines:
                out_ringbuf.popleft()
            if on_output:
                try:
                    on_output(tool_id, stream_name, chunk)
                except Exception:
                    pass
    except Exception:
        pass


@register_tool(
    name="bash",
    description="执行系统命令。危险操作（删文件/杀进程等）需 confirm=true。返回 stdout/stderr 和退出码。"
                "注意：修改文件内容请优先用 edit_file/write_file（有修改历史可回溯），"
                "bash 仅用于运行命令、安装依赖、编译等，不要用 bash 重定向/脚本来改文件。"
                "多行 Python 代码禁止用 python -c 内联（cmd 会逐行拆散导致输出为空），先 write_file 写脚本再执行。",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令"
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数 (默认120)",
                "default": 120
            },
            "workdir": {
                "type": "string",
                "description": "工作目录，默认项目根目录",
                "default": ""
            },
            "confirm": {
                "type": "boolean",
                "description": "确认执行危险操作。bash 返回警告后，设为 true 重新调用以强制执行",
                "default": False
            }
        },
        "required": ["command"]
    }
)
def bash(command: str, timeout: int = 120, workdir: str = "", confirm: bool = False):
    from .file_tools import _safe_path, _check_boundary
    # Resolve workdir through safe path + boundary check
    if workdir:
        cwd = _safe_path(workdir)
        # 用户开关开启时放行；否则边界检查
        if not _get_allow_outside():
            allowed, err = _check_boundary(cwd, for_write=True)
            if not allowed and not confirm:
                return err
    else:
        cwd = getattr(_ws, 'path', None) or BASE_DIR
    if not os.path.isdir(cwd):
        cwd = BASE_DIR
    level, reasons = _check_danger(command)
    if level == 'blocked':
        return {
            "error": "🚫 命令被永久阻止",
            "reasons": reasons,
            "hint": "此操作不可恢复，已被系统禁止",
            "output": "",
        }
    if level == 'dangerous' and not confirm:
        return {
            "warning": "⚠️ 危险操作需要确认",
            "reasons": reasons,
            "command": command,
            "hint": "如果确认要执行，请设置 confirm=true 重新调用",
            "output": "",
        }

    # ── Execute with streaming output ──
    # Windows: 切换控制台到 UTF-8，避免中文输出/文件写入乱码
    actual_cmd = command
    if os.name == 'nt':
        actual_cmd = 'chcp 65001 >nul 2>&1 && ' + command

    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'LANG': 'en_US.UTF-8'}
    # Windows: 用 CREATE_NEW_PROCESS_GROUP 让 taskkill /T 能 kill 整个进程树
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0

    def _kill_tree(pid):
        """Kill 整个进程树（Windows: taskkill /T /F, Unix: killpg）"""
        if os.name == 'nt':
            try:
                subprocess.run(
                    f'taskkill /PID {pid} /T /F',
                    shell=True, capture_output=True, timeout=10,
                )
            except Exception:
                pass
        else:
            import signal as _sig
            try:
                os.killpg(pid, _sig.SIGKILL)
            except Exception:
                pass

    on_output_cb = getattr(_ctx, 'on_output', None)
    tool_id = getattr(_ctx, 'tool_id', None)

    try:
        proc = subprocess.Popen(
            actual_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=cwd,
            env=env,
            creationflags=creationflags,
        )

        _emit('begin', f'$ {command}\n')

        stdout_lines = []
        stderr_lines = []
        output_queue = queue.Queue()
        stop_event = threading.Event()
        ringbuf = deque(maxlen=200)   # 流式保留最后 200 行（用于 UI 滚动展示）

        # 后台线程：分别读 stdout 和 stderr 管道，避免互相阻塞
        reader_threads = [
            threading.Thread(
                target=_stream_reader_pipe,
                args=(proc.stdout, stdout_lines, output_queue, 'stdout', stop_event),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_reader_pipe,
                args=(proc.stderr, stderr_lines, output_queue, 'stderr', stop_event),
                daemon=True,
            ),
        ]
        for t in reader_threads:
            t.start()

        # 消费线程：把队列里的行回调给 UI（decoupled，慢回调不阻塞管道）
        consumer = threading.Thread(
            target=_stream_consumer_thread,
            args=(output_queue, stop_event, on_output_cb, tool_id, ringbuf),
            daemon=True,
        )
        consumer.start()

        # 主循环：监控进程 + 超时 + 管道继承问题（孙子进程持有管道）
        deadline = time.time() + timeout
        exit_code = None
        timeout_hit = False
        while True:
            if proc.poll() is not None:
                exit_code = proc.returncode
                break
            if time.time() > deadline:
                timeout_hit = True
                _kill_tree(proc.pid)
                break
            time.sleep(0.2)

        # 给 reader 线程一小段时间排空管道（进程退出后可能还有尾数据）
        for t in reader_threads:
            t.join(timeout=1.0)
        stop_event.set()
        consumer.join(timeout=0.5)

        # 排空队列中剩余的块
        while not output_queue.empty():
            try:
                stream_name, chunk = output_queue.get_nowait()
                ringbuf.append(chunk)
                if on_output_cb:
                    try:
                        on_output_cb(tool_id, stream_name, chunk)
                    except Exception:
                        pass
            except queue.Empty:
                break

        stdout = ''.join(stdout_lines)
        stderr = ''.join(stderr_lines)

        if timeout_hit:
            _emit('end', f'\n[超时: {timeout}s，进程树已终止]\n')
            output_parts = []
            if stdout:
                output_parts.append(stdout.rstrip())
            if stderr:
                output_parts.append(f"--- stderr ---\n{stderr.rstrip()}")
            output = "\n".join(output_parts)[:10000]
            return {
                "error": f"命令超时 ({timeout}s)，进程树已终止",
                "stdout": stdout or "",
                "stderr": stderr or "",
                "output": output,
                "exit_code": -1,
            }

        _emit('end', f'\n[exit code: {exit_code}]\n')

        output_parts = []
        if stdout:
            output_parts.append(stdout.rstrip())
        if stderr:
            output_parts.append(f"--- stderr ---\n{stderr.rstrip()}")
        output = "\n".join(output_parts)[:10000]

        resp = {
            "stdout": stdout or "",
            "stderr": stderr or "",
            "output": output,
            "exit_code": exit_code,
        }
        if confirm:
            resp["confirmed"] = True
        return resp
    except Exception as e:
        return {"error": str(e), "output": ""}
