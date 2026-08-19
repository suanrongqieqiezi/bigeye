#!/usr/bin/env python3
"""模式B — Python 原生文件搜索。

原理: 扫盘建索引 → 缓存到磁盘 → 内存匹配。
比起系统遍历，优势在缓存命中后秒回。
"""
from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path
from typing import List

from ..core import SearchEngine, SearchResult, FileCache, register_engine


class NativeEngine(SearchEngine):
    """Python 原生文件搜索引擎。"""

    name = "native"

    # 单次扫描上限：防止无 root 时全盘扫死
    _SCAN_MAX_FILES = 30000        # 最多扫 3 万条（再多对用户也没意义）
    _SCAN_TIMEOUT_SEC = 8          # 最多跑 8 秒（别让用户等）
    _SCAN_MAX_DEPTH = 10           # 最多下 10 层（跳过 node_modules 深坑）

    def __init__(self, cache_ttl: float = 60):
        """
        Args:
            cache_ttl: 缓存有效期（秒），默认 60s 内不重新扫盘
        """
        self._cache = FileCache()
        self._cache_ttl = cache_ttl
        # 内存索引: root -> [(path, size, modified, is_dir)]
        self._memory_index: dict[str, list[tuple]] = {}

    def _scan(self, root: str) -> list[dict]:
        """扫盘，返回文件列表 dict。用 os.scandir 替代 rglob 以加速遍历。"""
        files = []
        root_path = Path(root)
        if not root_path.exists():
            return files

        # 不扫的目录（Windows 系统目录 + 已知的大深坑）
        _SKIP = {
            "System Volume Information", "$Recycle.Bin", "Windows",
            "Program Files", "Program Files (x86)", "ProgramData",
            "Recovery", "$WinREAgent", "Config.Msi", "$SysReset",
            "node_modules", ".git", "__pycache__", ".cache",
            "AppData\\Local\\Temp", "AppData\\Local\\Microsoft\\Windows\\INetCache",
        }

        def _should_skip(name: str, full_path: str) -> bool:
            if name in _SKIP:
                return True
            # 隐藏目录也跳过（点开头）
            if name.startswith(".") and name not in (".", ".."):
                return True
            return False

        deadline = time.time() + self._SCAN_TIMEOUT_SEC
        try:
            # 用 os.scandir 做 DFS，比 rglob 更可控
            stack = [(str(root_path), 0)]
            while stack and len(files) < self._SCAN_MAX_FILES:
                if time.time() > deadline:
                    break

                dir_path, depth = stack.pop()
                if depth > self._SCAN_MAX_DEPTH:
                    continue

                try:
                    with os.scandir(dir_path) as it:
                        for entry in it:
                            if len(files) >= self._SCAN_MAX_FILES:
                                break
                            if time.time() > deadline:
                                break

                            if _should_skip(entry.name, entry.path):
                                continue

                            try:
                                stat = entry.stat(follow_symlinks=False)
                            except (OSError, PermissionError):
                                continue

                            files.append({
                                "path": entry.path,
                                "size": stat.st_size,
                                "modified": stat.st_mtime,
                                "is_dir": entry.is_dir(),
                            })

                            if entry.is_dir() and not entry.is_symlink():
                                stack.append((entry.path, depth + 1))
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            pass
        return files

    def _load_or_scan(self, root: str) -> list[dict]:
        """从缓存加载或重新扫描。"""
        cached = self._cache.get(root, max_age=self._cache_ttl)
        if cached is not None:
            return cached

        files = self._scan(root)
        self._cache.set(root, files)
        return files

    def search(
        self,
        pattern: str,
        root: str = "",
        max_results: int = 100,
        file_only: bool = False,
        dir_only: bool = False,
    ) -> List[SearchResult]:
        # 确定搜索根
        roots = self._resolve_roots(root)

        results = []
        for r in roots:
            files = self._load_or_scan(r)
            for f in files:
                if len(results) >= max_results:
                    break
                # 类型过滤
                if file_only and f["is_dir"]:
                    continue
                if dir_only and not f["is_dir"]:
                    continue

                # 匹配：fnmatch 通配符 + 子串
                name = Path(f["path"]).name
                if fnmatch.fnmatch(name, pattern) or pattern.lower() in f["path"].lower():
                    results.append(SearchResult(
                        path=f["path"],
                        name=name,
                        size=f["size"],
                        modified=f["modified"],
                        is_dir=f["is_dir"],
                        match=pattern,
                    ))

            if len(results) >= max_results:
                break

        return results[:max_results]

    def _resolve_roots(self, root: str) -> list[str]:
        """解析搜索根目录。空 root 时只扫 C 盘 + 有缓存的其他盘。"""
        if root:
            # 指定目录
            root = os.path.abspath(root)
            if os.path.isdir(root):
                return [root]
            return []

        # 空=先扫 C 盘（95% 的文件都在 C 盘）
        roots = []
        if os.path.isdir("C:\\"):
            roots.append("C:\\")

        # 有缓存的其他盘也带上（之前扫过，缓存命中就不重新扫了）
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if os.path.isdir(drive):
                cached = self._cache.get(drive, max_age=self._cache_ttl)
                if cached is not None:
                    roots.append(drive)

        return roots or [os.path.abspath("/")]

    def count(self, pattern: str, root: str = "") -> int:
        """计数比 search 省内存——不构造 result 对象。"""
        roots = self._resolve_roots(root)
        total = 0
        for r in roots:
            files = self._load_or_scan(r)
            for f in files:
                name = Path(f["path"]).name
                if fnmatch.fnmatch(name, pattern) or pattern.lower() in f["path"].lower():
                    total += 1
        return total


# ── 自动注册 ──────────────────────────────────────────
register_engine("native", NativeEngine)
