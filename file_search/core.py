#!/usr/bin/env python3
"""file_search core — 搜索引擎抽象 + 统一结果格式 + 缓存。"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional


# ── 统一结果 ──────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class SearchResult:
    """统一的搜索结果格式。"""
    path: str          # 完整路径
    name: str          # 文件名
    size: int          # 字节数，-1 表示未知
    modified: float    # 时间戳，0 表示未知
    is_dir: bool       # 是否是目录
    match: str         # 命中的匹配字符串

    def __str__(self) -> str:
        return self.path


# ── 抽象引擎 ──────────────────────────────────────────

class SearchEngine(ABC):
    """搜索引擎抽象。所有适配器实现这个接口。"""

    name: str = "abstract"

    @abstractmethod
    def search(
        self,
        pattern: str,
        root: str = "",
        max_results: int = 100,
        file_only: bool = False,
        dir_only: bool = False,
    ) -> List[SearchResult]:
        """搜索文件。

        Args:
            pattern: 搜索关键词或通配符 (*.py, config* 等)
            root: 搜索根目录，空串=全盘
            max_results: 最大返回数
            file_only: 只搜文件
            dir_only: 只搜目录
        """
        ...

    def count(self, pattern: str, root: str = "") -> int:
        """快速计数（默认走 search 再 len，子类可重写优化）。"""
        return len(self.search(pattern, root, max_results=99999))


# ── 持久缓存（模式B用） ─────────────────────────────────

class FileCache:
    """文件列表持久缓存，避免每次重复扫盘。

    存成 JSON，结构:
    {
        "root": "C:/",
        "scanned_at": 1234567890.0,
        "files": [
            {"path": "C:/a.txt", "size": 100, "modified": ...},
            ...
        ]
    }
    """

    def __init__(self, cache_dir: str | None = None):
        if cache_dir is None:
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("HOME") or "."
            cache_dir = os.path.join(base, "file_search_cache")
        self._cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _key(self, root: str) -> str:
        # 把盘符/路径哈希成文件名
        safe = root.replace(":", "").replace("/", "_").replace("\\", "_") or "default"
        return os.path.join(self._cache_dir, f"idx_{safe}.json")

    def get(self, root: str, max_age: float = 30) -> list[dict] | None:
        """取缓存，超过 max_age 秒返回 None。"""
        path = self._key(root)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            age = time.time() - data.get("scanned_at", 0)
            if age > max_age:
                return None
            return data.get("files", [])
        except (json.JSONDecodeError, KeyError):
            return None

    def set(self, root: str, files: list[dict]) -> None:
        """写入缓存。"""
        path = self._key(root)
        data = {"root": root, "scanned_at": time.time(), "files": files}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def clear(self, root: str = "") -> None:
        """清除缓存。root 空则清所有。"""
        if root:
            path = self._key(root)
            if os.path.isfile(path):
                os.remove(path)
        else:
            for f in os.listdir(self._cache_dir):
                if f.startswith("idx_"):
                    os.remove(os.path.join(self._cache_dir, f))

    @property
    def cache_dir(self) -> str:
        return self._cache_dir


# ── 全局入口 ──────────────────────────────────────────

_SEARCH_ENGINES: dict[str, type[SearchEngine]] = {}


def register_engine(name: str, cls: type[SearchEngine]) -> None:
    """注册搜索引擎实现。"""
    _SEARCH_ENGINES[name] = cls


def get_search_engine(mode: str = "auto") -> SearchEngine:
    """获取搜索引擎实例。

    Args:
        mode: "auto"=自动选最佳, "everything"=模式A, "native"=模式B
    """
    if mode == "auto":
        # 如果有 Everything 且 es.exe 可用就走它
        if "everything" in _SEARCH_ENGINES:
            try:
                eng = _SEARCH_ENGINES["everything"]()
                if eng.is_available():
                    return eng
            except Exception:
                pass
        # 否则走原生
        if "native" in _SEARCH_ENGINES:
            return _SEARCH_ENGINES["native"]()
        raise RuntimeError("没有可用的搜索引擎。先 import 适配器再调用。")
    if mode in _SEARCH_ENGINES:
        return _SEARCH_ENGINES[mode]()
    raise ValueError(f"未知模式: {mode}，可用: {list(_SEARCH_ENGINES.keys())}")


def search(
    pattern: str,
    root: str = "",
    max_results: int = 100,
    mode: str = "auto",
    file_only: bool = False,
    dir_only: bool = False,
) -> List[SearchResult]:
    """一键搜索。"""
    engine = get_search_engine(mode)
    return engine.search(
        pattern=pattern,
        root=root,
        max_results=max_results,
        file_only=file_only,
        dir_only=dir_only,
    )
