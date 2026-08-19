#!/usr/bin/env python3
"""模式A — Everything es.exe CLI 适配器。

原理: subprocess 调 es.exe，直接利用 Everything 的 MFT 索引，
毫秒级返回。比模式B快几个数量级。

依赖: 需安装 Everything (https://www.voidtools.com) 并启用 es.exe。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import List

from ..core import SearchEngine, SearchResult, register_engine


class EverythingEngine(SearchEngine):
    """Everything es.exe 搜索引擎。"""

    name = "everything"

    def __init__(self, es_path: str | None = None):
        """
        Args:
            es_path: es.exe 路径。None=自动查找。
        """
        self._es_path = es_path or self._find_es()

    @staticmethod
    def _find_es() -> str:
        """找 es.exe。"""
        # 常见位置
        candidates = [
            os.environ.get("ES_PATH", ""),
            r"C:\Program Files\Everything\es.exe",
            r"C:\Program Files (x86)\Everything\es.exe",
            r"C:\Tools\Everything\es.exe",
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        # 试试 PATH
        for p in os.environ.get("PATH", "").split(";"):
            c = os.path.join(p, "es.exe")
            if os.path.isfile(c):
                return c
        raise FileNotFoundError(
            "es.exe 未找到。安装 Everything 后把 es.exe 放 PATH，"
            "或设置环境变量 ES_PATH"
        )

    def _run_es(self, args: list[str]) -> str | None:
        """执行 es.exe，返回 stdout。"""
        try:
            r = subprocess.run(
                [self._es_path] + args,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _parse_output(self, output: str, pattern: str) -> list[SearchResult]:
        """解析 es.exe 输出为 SearchResult 列表。"""
        results = []
        for line in output.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # es.exe 默认每行一个路径
            p = Path(line)
            try:
                stat = p.stat(follow_symlinks=False)
                size = stat.st_size
                modified = stat.st_mtime
            except (OSError, PermissionError):
                size = -1
                modified = 0
            results.append(SearchResult(
                path=line,
                name=p.name,
                size=size,
                modified=modified,
                is_dir=p.is_dir() if p.exists() else False,
                match=pattern,
            ))
        return results

    def search(
        self,
        pattern: str,
        root: str = "",
        max_results: int = 100,
        file_only: bool = False,
        dir_only: bool = False,
    ) -> List[SearchResult]:
        args = [f"-n{max_results}"]

        # 类型过滤
        if file_only:
            args.append("-f")
        elif dir_only:
            args.append("-d")

        # 搜索路径
        if root:
            args.extend(["-p", os.path.abspath(root)])

        # 搜索词
        args.append(pattern)

        output = self._run_es(args)
        if not output:
            return []

        return self._parse_output(output, pattern)

    def count(self, pattern: str, root: str = "") -> int:
        """es.exe 原生支持计数。"""
        args = ["-c"]  # count mode
        if root:
            args.extend(["-p", os.path.abspath(root)])
        args.append(pattern)
        output = self._run_es(args)
        if output:
            try:
                return int(output.strip())
            except ValueError:
                pass
        return 0

    def is_available(self) -> bool:
        """检查 es.exe 是否可用。"""
        try:
            r = subprocess.run(
                [self._es_path, "-?"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
            )
            return r.returncode == 0
        except Exception:
            return False


# ── 自动注册 ──────────────────────────────────────────
register_engine("everything", EverythingEngine)
