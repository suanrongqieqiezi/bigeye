# -*- coding: utf-8 -*-
"""audit_check — 动作前的坑指纹核查（框架常驻工具版，待注册生效）

在执行写/删/改类敏感动作前调用，输入即将执行的动作描述文本，
比对事件账本中的历史坑指纹，命中则返回归因类+教训。

生效步骤（对应 skills/create-new-tool.md）:
  1. 本文件已在 tools/ 目录
  2. server.py import 区加: import tools.audit_guard
  3. 重启服务器

账本路径为必传参数（v1 诚实设计：工具不猜"当前任务"，由调用方给出）。
"""
import json
import re
from pathlib import Path

from tools.registry import register_tool

# 运行时挂进折叠分组（内存 dict 就地追加，幂等）。
# 为什么不靠 registry.py 文件里的静态列表：热重载刻意跳过 registry（reload 会清空 _tools），
# 所以活进程只能由本模块 import 时自己挂名；重启后 server.py 静态 import 也会执行这段，不冲突。
from tools import registry as _reg
_misc_group = _reg.FOLDED_TOOL_GROUPS.get("misc")
if _misc_group and "audit_check" not in _misc_group["tools"]:
    _misc_group["tools"].append("audit_check")

_BUILTIN = {
    "write_file/read_file:相对路径含workspace前缀": [
        r"(write_file|read_file|edit_file)\s*\(\s*['\"](?:workspace[/\\]|data[/\\]missions)",
        r"data[/\\]missions[/\\][0-9a-f]{6,}[/\\].*[/\\]data[/\\]missions",
    ],
}


@register_tool(
    name="audit_check",
    description="动作前的坑指纹核查：输入即将执行的动作文本（如 write_file(self_audit/x.md)），比对事件账本历史坑，命中返回归因类与教训。写/删/改类敏感操作前建议调用。",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "即将执行的动作描述文本"},
            "ledger_path": {"type": "string", "description": "事件账本JSON路径（必传，如当前任务工作区 self_audit/事件账本.json）"},
        },
        "required": ["action", "ledger_path"],
    },
)
def audit_check(action: str, ledger_path: str):
    try:
        ledger = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"账本读取失败: {e}"}

    fps = {}
    for r in ledger.get("records", []):
        fp = (r.get("fingerprint") or "").strip()
        if not fp:
            continue
        e = fps.setdefault(fp, {"classes": set(), "lessons": [], "count": 0})
        e["classes"].add(r.get("class", "?"))
        if r.get("lesson"):
            e["lessons"].append(r["lesson"])
        e["count"] += 1

    hits = []
    for fp, entry in fps.items():
        pats = [fp[3:]] if fp.startswith("re:") else _BUILTIN.get(fp, [])
        for p in pats:
            if re.search(p, action, re.IGNORECASE):
                hits.append({
                    "fingerprint": fp,
                    "classes": sorted(entry["classes"]),
                    "count": entry["count"],
                    "lesson": entry["lessons"][0] if entry["lessons"] else "",
                })
                break
    return {"ok": True, "hit": bool(hits), "hits": hits, "fingerprints_total": len(fps)}
