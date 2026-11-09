#!/usr/bin/env python3
"""
MCP (Model Context Protocol) client for 大眼X.

Connects to MCP servers via stdio (subprocess), discovers their tools,
and registers them as native AI tools — no code changes needed.

Config: mcp_servers.json in project root.
Format:
{
  "servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-server-filesystem", "."],
      "env": {}
    },
    "github": {
      "command": "npx", 
      "args": ["-y", "@anthropic/mcp-server-github"],
      "env": {"GITHUB_TOKEN": "..."}
    }
  }
}
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

# Windows 下不弹黑窗（MCP server 是长驻子进程，弹一次就一直在）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

SERVER_FILE = "mcp_servers.json"
# 模块版本标记：热加载时据此判断内存里的 mcp_client 是不是旧代码
_CFG_FIX_V = "2026-11-05-cfgpath"

# ── JSON-RPC helpers ──────────────────────────────────

def _rpc_request(method, params=None, rid=None):
    return {
        "jsonrpc": "2.0",
        "id": rid or str(uuid.uuid4())[:8],
        "method": method,
        "params": params or {},
    }

def _rpc_notification(method, params=None):
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    }

# ── MCP Server process ───────────────────────────────

class MCPServer:
    """Manages one MCP server subprocess."""

    def __init__(self, name, config):
        self.name = name
        self.command = config.get("command", "")
        self.args = config.get("args", [])
        self.env = config.get("env", {})
        self.process = None
        self.tools = []          # list of tool defs from server
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending = {}       # id -> threading.Event + result
        self._reader_thread = None
        self._initialized = False

    def start(self):
        if self.process and self.process.poll() is None:
            return True
        try:
            merged_env = {**os.environ, **self.env}
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                text=True,
                encoding="utf-8",
                creationflags=_NO_WINDOW,
            )
            self._reader_thread = threading.Thread(target=self._reader, daemon=True)
            self._reader_thread.start()
            # Initialize
            resp = self._call("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "大眼X", "version": "1.0"},
            })
            if not resp or "error" in resp:
                print(f"[mcp] {self.name}: init failed: {resp}")
                return False
            # Send initialized notification
            self._send(_rpc_notification("notifications/initialized"))
            # Discover tools
            tools_resp = self._call("tools/list")
            if tools_resp and "result" in tools_resp:
                self.tools = tools_resp["result"].get("tools", [])
                print(f"[mcp] {self.name}: {len(self.tools)} tools discovered")
            self._initialized = True
            return True
        except Exception as e:
            print(f"[mcp] {self.name}: failed to start: {e}")
            return False

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
            self.process = None
            self._initialized = False

    def _send(self, msg):
        if not self.process or self.process.poll() is not None:
            return
        try:
            line = json.dumps(msg, ensure_ascii=False) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except Exception:
            pass

    def _reader(self):
        """Read JSON-RPC responses from stdout."""
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = msg.get("id")
                if rid and rid in self._pending:
                    event = self._pending[rid][1]
                    self._pending[rid] = (msg, event)
                    event.set()
        except Exception:
            pass

    def _call(self, method, params=None, timeout=30):
        rid = str(uuid.uuid4())[:8]
        event = threading.Event()
        self._pending[rid] = (None, event)
        self._send(_rpc_request(method, params, rid))
        if event.wait(timeout):
            result, _ = self._pending.pop(rid, (None, None))
            return result
        self._pending.pop(rid, None)
        return {"error": "timeout"}

    def call_tool(self, tool_name, arguments, timeout=60):
        # 进程已死就别等满了：卸载后调用会白卡一个 timeout（实测 3s/60s）
        if not self.process or self.process.poll() is not None:
            return {"error": f"{self.name}: server 未运行"}
        resp = self._call("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout=timeout)
        if not resp:
            return {"error": f"{self.name}: 无响应"}
        if "error" in resp:
            return {"error": f"{self.name}: {resp['error']}"}
        result = resp.get("result", {})
        content = result.get("content", [])
        if not content:
            return {"result": str(result)}
        # Extract text from content blocks
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "resource":
                    texts.append(f"[resource: {block.get('resource', {})}]")
        return {"result": "\n".join(texts) if texts else str(result)}


# ── Global manager ────────────────────────────────────

_mcp_servers: dict[str, MCPServer] = {}
_mcp_loaded = False
_mcp_lock = threading.Lock()


def _load_config():
    """Find mcp_servers.json in project root or executable dir."""
    candidates = []
    here = os.path.dirname(os.path.abspath(__file__))
    # 源码运行时 __file__ 就在项目根；打包后可能在 _internal/ 子目录里
    candidates.append(os.path.join(here, SERVER_FILE))
    candidates.append(os.path.join(os.path.dirname(here), SERVER_FILE))
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), SERVER_FILE))
    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return {"servers": {}}


def load_mcp_servers(reload=False):
    """按 mcp_servers.json 对齐运行中的 server 集合。

    reload=False: 首次加载，已启动的直接跳过。
    reload=True : 重新读配置 —— 新增的启动、配置变了的重启、删掉的停掉。
                  热加载走这条路，否则装卸 server 不生效。
    """
    global _mcp_loaded
    with _mcp_lock:
        if _mcp_loaded and not reload:
            return len(_mcp_servers)

        config = _load_config()
        want = config.get("servers", {}) or {}

        # 1) 配置里没有的 → 停掉并摘除（卸载）
        for name in list(_mcp_servers.keys()):
            if name not in want:
                try:
                    _mcp_servers[name].stop()
                except Exception:
                    pass
                _mcp_servers.pop(name, None)
                print(f"[mcp] {name}: unloaded")

        # 2) 配置里有但没在跑的 → 启动；配置变了的 → 重启
        for name, cfg in want.items():
            old = _mcp_servers.get(name)
            if old is not None:
                if getattr(old, "config", None) == cfg:
                    continue          # 配置没变，保持原进程
                try:
                    old.stop()
                except Exception:
                    pass
                _mcp_servers.pop(name, None)
            srv = MCPServer(name, cfg)
            srv.config = cfg          # 存快照，供下次比对配置是否变化
            if srv.start():
                _mcp_servers[name] = srv

        _mcp_loaded = True
        return len(_mcp_servers)


def get_mcp_tool_defs():
    """Get OpenAI-compatible tool definitions for all MCP tools.

    返回扁平格式 {"name":..., "description":..., "parameters":...}，
    和 registry 里内置工具完全一致 —— 因为下游两处都这么吃：
      - llm._build_tools() 直接取 t["name"] / t["parameters"]
      - server./api/settings/tools 同样取 d.get("name")
    早先这里返回 {"function": {...}} 嵌套壳，导致 MCP 工具名被读成空串，
    设置面板显示 11 个空条目、模型也永远拿不到它们。
    """
    defs = []
    for name, srv in _mcp_servers.items():
        for tool in srv.tools:
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            full = f"mcp_{name}__{tool['name']}"
            defs.append({
                "name": full,
                "label": full,
                "description": f"[MCP:{name}] {tool.get('description', '')}",
                "parameters": schema,
            })
    return defs


def execute_mcp_tool(full_name, args):
    """Execute an MCP tool. full_name format: mcp_{server}__{tool}"""
    for sname, srv in _mcp_servers.items():
        prefix = f"mcp_{sname}__"
        if full_name.startswith(prefix):
            tool_name = full_name[len(prefix):]
            return srv.call_tool(tool_name, args)
    return {"error": f"未知 MCP 工具: {full_name}"}


def list_mcp_servers():
    """List running MCP servers and their tools."""
    result = {}
    for name, srv in _mcp_servers.items():
        result[name] = {
            "tools": [t["name"] for t in srv.tools],
            "running": srv.process is not None and srv.process.poll() is None,
        }
    return result
