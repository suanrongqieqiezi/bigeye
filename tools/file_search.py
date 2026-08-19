#!/usr/bin/env python3
"""File search — 双模式文件搜索工具（注册为内置工具）。

用法:
    file_search(pattern="*.py", root="C:/project")
    file_search(pattern="config", root="C:/", max_results=50, mode="auto")

模式:
    auto       — 有 Everything 就走它，没有就走原生
    everything — 调 es.exe（需安装 Everything）
    native     — Python 原生扫盘 + 缓存
"""
from .registry import register_tool

# 延迟导入，避免启动时加载所有适配器
_engine = None


def _get_engine(mode: str = "auto"):
    global _engine
    if _engine is None:
        # 导入适配器触发 register_engine
        import file_search.adapters  # noqa: F401
        from file_search import get_search_engine
        _engine = get_search_engine
    return _engine(mode)


@register_tool(
    name="file_search",
    description="搜索本地文件。双模式：有Everything走NTFS索引（毫秒级），没有就走原生扫描+缓存。支持通配符和子串匹配。",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "搜索关键词或通配符，如 '*.py'、'config'、'report*'"
            },
            "root": {
                "type": "string",
                "description": "搜索根目录，留空全盘搜索（Windows下扫所有盘符）",
                "default": ""
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回条数（默认100）",
                "default": 100
            },
            "mode": {
                "type": "string",
                "description": "搜索模式: auto=自动选最佳, everything=调es.exe(需安装), native=Python原生",
                "enum": ["auto", "everything", "native"],
                "default": "auto"
            },
            "file_only": {
                "type": "boolean",
                "description": "只搜文件",
                "default": False
            },
            "dir_only": {
                "type": "boolean",
                "description": "只搜目录",
                "default": False
            },
            "count_only": {
                "type": "boolean",
                "description": "只返回匹配数量（不返回文件列表）",
                "default": False
            }
        },
        "required": ["pattern"]
    }
)
def file_search(
    pattern: str,
    root: str = "",
    max_results: int = 100,
    mode: str = "auto",
    file_only: bool = False,
    dir_only: bool = False,
    count_only: bool = False,
):
    """搜索本地文件，毫秒级返回。"""
    try:
        engine = _get_engine(mode)
    except Exception as e:
        return {"error": f"搜索引擎初始化失败: {e}。试试 mode='native'。"}

    try:
        if count_only:
            count = engine.count(pattern, root=root)
            return {"count": count, "mode": engine.name}

        results = engine.search(
            pattern=pattern,
            root=root,
            max_results=max_results,
            file_only=file_only,
            dir_only=dir_only,
        )
        return {
            "mode": engine.name,
            "total": len(results),
            "results": [
                {
                    "path": r.path,
                    "name": r.name,
                    "size": r.size,
                    "modified": r.modified,
                    "is_dir": r.is_dir,
                }
                for r in results
            ],
        }
    except Exception as e:
        return {"error": f"搜索失败: {e}"}
