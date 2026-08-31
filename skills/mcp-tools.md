---
name: "mcp-tools"
description: "连接社区 MCP 工具服务器，扩展 AI 能力。不需要写代码。"
triggers: ["需要接入MCP服务器扩展工具"]
---

# mcp-tools
> 连接社区 MCP 工具服务器，扩展 AI 能力。不需要写代码。

## 什么是 MCP

Model Context Protocol — AI 工具的标准协议。Anthropic 定义，Cursor、Claude Desktop 等都在用。

MCP 服务器是独立的进程，AI 通过标准协议调用它们提供的工具。社区有海量现成的 MCP server：

- `@anthropic/mcp-server-filesystem` — 安全文件访问
- `@anthropic/mcp-server-github` — GitHub 操作
- `@anthropic/mcp-server-postgres` — 数据库查询
- `@anthropic/mcp-server-puppeteer` — 浏览器控制
- 更多: https://github.com/modelcontextprotocol/servers

## 配置方法

在项目根目录创建 `mcp_servers.json`：

```json
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
      "env": {"GITHUB_TOKEN": "你的token"}
    }
  }
}
```

- `command`: 启动命令（npx、python、node 等）
- `args`: 命令行参数
- `env`: 环境变量（API keys 等）

## 使用步骤

1. **创建配置**: `write_file("mcp_servers.json", 配置内容)`
2. **热加载**: `hot_reload` — MCP 工具会以 `mcp_服务器名__工具名` 的格式出现在工具列表中
3. **直接调用**: AI 会像使用内置工具一样使用 MCP 工具

## 常用 MCP 服务器

| 功能 | 命令 |
|------|------|
| 文件系统 | `npx -y @anthropic/mcp-server-filesystem /允许的目录` |
| GitHub | `npx -y @anthropic/mcp-server-github` (需 GITHUB_TOKEN) |
| PostgreSQL | `npx -y @anthropic/mcp-server-postgres postgresql://...` |
| 浏览器 | `npx -y @anthropic/mcp-server-puppeteer` |
| Brave搜索 | `npx -y @anthropic/mcp-server-brave-search` (需 BRAVE_API_KEY) |
| Slack | `npx -y @anthropic/mcp-server-slack` (需 SLACK_BOT_TOKEN) |
| 内存 | `npx -y @anthropic/mcp-server-memory` |

## Python MCP 服务器

也可以用 Python 写自己的 MCP server：

```python
# my_server.py
from mcp.server import Server
server = Server("my-tools")

@server.tool()
def hello(name: str) -> str:
    return f"Hello, {name}!"
```

配置: `{"command": "python", "args": ["my_server.py"]}`
