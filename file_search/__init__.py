#!/usr/bin/env python3
"""file_search — 双模式文件搜索工具包。

用法:
    from file_search import search, get_search_engine
    
    # 自动选择最佳模式 (Everything > Native)
    results = search("*.py", root="C:/project")
    
    # 或手动指定
    engine = get_search_engine(mode="native")
    results = engine.search("config", root="C:/")
"""
from .core import SearchEngine, SearchResult, FileCache, search, get_search_engine

__all__ = ["SearchEngine", "SearchResult", "FileCache", "search", "get_search_engine"]
