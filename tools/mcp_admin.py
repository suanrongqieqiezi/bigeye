#!/usr/bin/env python3
"""MCP 装载/卸载管理。

为什么单独做成 tools/ 下模块：
  server.py 里那段 hot_reload 是 import 时定义死的，改了源码也不会换掉内存里的
  旧函数；而 tools/*.py 每次 hot_reload 都会 importlib.reload，是真能换掉的。
  所以把「重载 MCP」放在这里，靠 hot_reload 就能生效，不用重启大眼。

本模块 import 时会做两件事：
  1) 把 reload_mcp_servers 挂进 misc 分组，让 execute_advanced_tool 找得到它
     （不能叫 mcp_* —— registry 见到这个前缀就转发给 MCP 子进程，永远调不到）
     （registry.py 不会被 reload，所以只能就地改它那个 dict）
  2) 如果 mcp_servers.json 里配了 server 但内存里没起，自动起一次
"""
import importlib

from .registry import register_tool, FOLDED_TOOL_GROUPS


def _load_mcp():
    """重载 mcp_client（换掉内存里旧代码）并按配置装/卸 server。"""
    import mcp_client
    importlib.reload(mcp_client)
    n = mcp_client.load_mcp_servers(reload=True)
    return mcp_client, n


@register_tool(
    "reload_mcp_servers",
    "按 mcp_servers.json 重载 MCP 服务器：新增的启动、配置变了的重启、删掉的停掉。"
    "改完 mcp_servers.json 后调它，不用重启大眼。返回装了几台、各有什么工具。"
)
def reload_mcp_servers():
    try:
        client, n = _load_mcp()
        cfg = (client._load_config() or {}).get("servers", {}) or {}
        return {
            "server数": n,
            "配置里的服务器": list(cfg.keys()),
            "servers": client.list_mcp_servers(),
            "工具": [d["name"] for d in client.get_mcp_tool_defs()],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# 1) 挂进 misc 分组（就地改 dict，reload registry 会被跳过，所以必须这么做）
#    顺手剔除旧名 mcp_reload —— registry.execute_tool 用 startswith("mcp_")
#    判断是不是 MCP 子进程工具，叫这名字会被直接转发走，永远走到"未知 MCP 工具"。
_misc = FOLDED_TOOL_GROUPS.get("misc")
if _misc is not None:
    _misc["tools"] = [t for t in _misc["tools"] if t != "mcp_reload"]
    if "reload_mcp_servers" not in _misc["tools"]:
        _misc["tools"].append("reload_mcp_servers")

# 2) 配置里有 server 但没起 → 起一次。
#    只在「有配置、没装满」时动手，避免每次热加载都重启一遍子进程。
try:
    import mcp_client as _c
    _cfg = (_c._load_config() or {}).get("servers", {}) or {}
    _want = set(_cfg.keys())
    _have = set(_c._mcp_servers.keys())
    if _want and _want != _have:
        _load_mcp()
except Exception:
    pass
