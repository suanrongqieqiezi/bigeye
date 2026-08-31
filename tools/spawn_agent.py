#!/usr/bin/env python3
"""子代理工具 — 主 AI 规划步骤，子代理按步骤执行（内部消化模式）。

不创建子 topic，执行过程记录在父 topic 的 tool message args 里。
执行完返回摘要给主 AI，用户想看详情翻父 topic 的工具调用记录。

安全模型：白名单 + 最小权限 + 工作区隔离
- 默认只读：子代理只能用 read_file / file_search / web_search / bash(只读命令)
- 写操作需显式授权：allow_write=True + workspace 指定唯一可写目录
- 路径校验：子代理只能碰 workspace 内的文件，跨工作区直接拒绝
"""
import json
import time
import os
import re
from .registry import register_tool

# ── 默认只读工具集 ──
_READ_ONLY_TOOLS = {"read_file", "file_search", "web_search", "bash", "system_status", "current_topic"}

# ── 写工具集（需 allow_write=True 才放行）──
_WRITE_TOOLS = {"edit_file", "write_file", "delete_file", "rename_topic", "name_task"}

# ── bash 只读命令白名单（allow_write=False 时只允许这些）──
_BASH_READ_PATTERNS = [
    r"^\s*(dir|ls|type|cat|more|findstr|find|where|echo|pwd|cd\s)",
    r"^\s*(python|python3)\s+.*(?:\.py|\.json|\.txt|\.md)\s*$",
    r"^\s*(python|python3)\s+-c\s+",
    r"^\s*(curl|wget)\s+",
    r"^\s*(git)\s+(log|show|diff|status|branch|tag|describe)",
    r"^\s*(zipinfo|python\s+-m\s+zipfile\s+-l)",
    r"^\s*(certutil\s+-hashfile|python\s+-c\s+\"import\s+hashlib)",
    r"^\s*(tasklist|systeminfo|wmic\s+.*get)",
    r"^\s*(powershell\s+.*(?:Get-|Test-|Select-String))",
]

# ─ bash 危险命令（任何情况都禁止）──
_BASH_BANNED_PATTERNS = [
    r"\b(format|diskpart|rd\s+/s\s+/q|rmdir\s+/s\s+/q)\b",
    r"\b(del\s+/f\s+/s\s+/q|rm\s+-rf\s+/)\b",
    r"\b(shutdown|logoff|tskill|taskkill\s+/f\s+/im\s+explorer)\b",
    r"\b(net\s+user|net\s+localgroup|reg\s+add|reg\s+delete)\b",
    r"\b(icacls|takeown|attrib\s+-h)\b",
]


def _is_bash_read_only(command: str):
    """检查 bash 命令是否只读。返回 (是否只读, 拦截原因)。"""
    cmd = command.strip()
    for pat in _BASH_BANNED_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            return False, "危险命令被拦截"
    for pat in _BASH_READ_PATTERNS:
        if re.match(pat, cmd, re.IGNORECASE):
            return True, None
    return False, "非只读命令，需要 allow_write=True"


def _check_path_in_workspace(path: str, workspace: str) -> bool:
    """检查路径是否在工作区内。"""
    if not workspace:
        return False
    try:
        real_path = os.path.realpath(path)
        real_ws = os.path.realpath(workspace)
        return real_path.startswith(real_ws + os.sep) or real_path == real_ws
    except Exception:
        return False


def _extract_path_from_args(action: str, args: dict) -> str:
    """从工具参数中提取文件路径。"""
    if action == "bash":
        cmd = args.get("command", "")
        paths = re.findall(r'["\']([A-Za-z]:\\[^"\']+)["\']', cmd)
        return paths[0] if paths else ""
    elif action in ("read_file", "edit_file", "write_file", "delete_file"):
        return args.get("file_path", args.get("path", ""))
    return ""


@register_tool(
    name="spawn_agent",
    description=(
        "派子代理按步骤执行任务。主 AI 负责规划步骤，子代理按顺序执行，记录在当前任务内。"
        "安全模型：默认只读 + 工作区隔离（workspace=子代理唯一可写目录，不传=纯只读）。"
        "steps 必填：每项 {action:工具名, args:{...}, desc:'这步做什么'}。"
        "allow_write 默认 False，True 才允许写文件；max_turns 默认 10。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "工具名"},
                        "args": {"type": "object", "description": "工具参数"},
                        "desc": {"type": "string", "description": "这步做什么"},
                    },
                    "required": ["action", "args", "desc"],
                },
                "description": "步骤列表（必填）",
            },
            "background": {"type": "string", "description": "可选，背景信息"},
            "workspace": {"type": "string", "description": "可选，子代理唯一可写目录（不传=纯只读）"},
            "allow_write": {"type": "boolean", "description": "可选，默认 False（只读）。True 允许在 workspace 内写文件"},
            "max_turns": {"type": "integer", "description": "可选，最大步数（默认 10）", "minimum": 1, "maximum": 50},
        },
        "required": ["steps"],
    },
)
def spawn_agent(steps: list, background: str = "", workspace: str = "", allow_write: bool = False, max_turns: int = 10):
    from db import get_db
    from tools.registry import execute_tool

    db = get_db()
    parent_tid = db.get_active_topic_id()
    if not parent_tid:
        return "没有活跃任务，无法派子代理。"

    # 没传 workspace 时，默认用父任务的工作区
    if not workspace and allow_write:
        ws_rel = db.get_topic_meta(parent_tid, "workspace") or ""
        if ws_rel:
            import sys as _sys
            ROOT_DIR = os.path.dirname(_sys.executable) if getattr(_sys, 'frozen', False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            workspace = os.path.join(ROOT_DIR, ws_rel.replace("/", os.sep))

    results = []
    blocked_count = 0

    # 内部消化：执行日志收集到一个聚合字符串，最后一次写入父 topic
    log_lines = []

    for i, step in enumerate(steps[:max_turns]):
        action = step.get("action", "")
        args = step.get("args", {})
        desc = step.get("desc", "")

        # ─ 安全层 1：工具白名单 ─
        if action in _WRITE_TOOLS and not allow_write:
            err = f"写工具 {action} 被拦截（allow_write=False）"
            results.append({"step": i+1, "desc": desc, "action": action, "success": False, "error": err})
            blocked_count += 1
            log_lines.append(f"[步骤{i+1} 拦截] {err}")
            continue

        if action not in _READ_ONLY_TOOLS and action not in _WRITE_TOOLS:
            err = f"工具 {action} 不在子代理允许列表中"
            results.append({"step": i+1, "desc": desc, "action": action, "success": False, "error": err})
            blocked_count += 1
            log_lines.append(f"[步骤{i+1} 拦截] {err}")
            continue

        # ── 安全层 2：bash 命令只读检查 ──
        if action == "bash" and not allow_write:
            cmd = args.get("command", "")
            is_readonly, reason = _is_bash_read_only(cmd)
            if not is_readonly:
                err = f"bash 命令被拦截：{reason} | 命令：{cmd[:80]}"
                results.append({"step": i+1, "desc": desc, "action": action, "success": False, "error": err})
                blocked_count += 1
                log_lines.append(f"[步骤{i+1} 拦截] {reason}")
                continue

        # ── 安全层 3：路径校验（防跨工作区）──
        if workspace:
            path = _extract_path_from_args(action, args)
            if path and not _check_path_in_workspace(path, workspace):
                err = f"路径越界：{path} 不在 workspace {workspace} 内"
                results.append({"step": i+1, "desc": desc, "action": action, "success": False, "error": err})
                blocked_count += 1
                log_lines.append(f"[步骤{i+1} 拦截] {err}")
                continue

        # ── 执行工具 ──
        log_lines.append(f"[步骤{i+1}] {desc} | 工具：{action} | 参数：{json.dumps(args, ensure_ascii=False)[:200]}")

        try:
            result = execute_tool(action, args)
            result_str = str(result)
            if len(result_str) > 2000:
                result_str = result_str[:500] + "…[截断]…" + result_str[-300:]
            log_lines.append(f"  → 结果：{result_str}")

            results.append({"step": i+1, "desc": desc, "action": action,
                            "success": True, "result_preview": result_str[:200]})
        except Exception as e:
            err_msg = str(e)
            log_lines.append(f"  → 错误：{err_msg}")
            results.append({"step": i+1, "desc": desc, "action": action,
                            "success": False, "error": err_msg})

    # 把完整执行日志作为一条 tool message 写入父 topic（用户可翻看）
    try:
        full_log = "\n".join(log_lines)
        if len(full_log) > 8000:
            full_log = full_log[:4000] + "\n…[日志截断]…\n" + full_log[-2000:]
        db.add_message(
            parent_tid, "tool", full_log,
            args={"spawn_agent": True, "step_count": len(results),
                  "success": sum(1 for r in results if r.get("success")),
                  "blocked": blocked_count, "background": background[:200]},
            ts=time.time(),
        )
    except Exception:
        pass

    # 组装返回给主 AI 的摘要
    success_count = sum(1 for r in results if r.get("success"))
    fail_count = len(results) - success_count

    lines = [f"子代理执行完成：{success_count} 成功 / {fail_count} 失败 / 共 {len(results)} 步"]
    if blocked_count:
        lines.append(f"安全拦截：{blocked_count} 步被拦截")

    for r in results:
        status = "✅" if r.get("success") else "❌"
        lines.append(f"{status} 步骤{r['step']}: {r['desc']}")
        if r.get("success"):
            lines.append(f"   结果：{r['result_preview'][:150]}")
        else:
            lines.append(f"   错误：{r.get('error', '未知错误')[:150]}")

    return "\n".join(lines)
