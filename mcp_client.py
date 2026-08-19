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

SERVER_FILE = "mcp_servers.json"

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
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(base, SERVER_FILE))
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), SERVER_FILE))
    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return {"servers": {}}


def load_mcp_servers():
    """Start all configured MCP servers and discover tools. Idempotent."""
    global _mcp_loaded
    with _mcp_lock:
        if _mcp_loaded:
            return len(_mcp_servers)
        config = _load_config()
        for name, cfg in config.get("servers", {}).items():
            if name in _mcp_servers:
                continue
            srv = MCPServer(name, cfg)
            if srv.start():
                _mcp_servers[name] = srv
        _mcp_loaded = True
        return len(_mcp_servers)


def get_mcp_tool_defs():
    """Get OpenAI-compatible tool definitions for all MCP tools."""
    defs = []
    for name, srv in _mcp_servers.items():
        for tool in srv.tools:
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            defs.append({
                "function": {
                    "name": f"mcp_{name}__{tool['name']}",
                    "description": f"[MCP:{name}] {tool.get('description', '')}",
                    "parameters": schema,
                }
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
