#!/usr/bin/env python3
"""Web search — pluggable engine registry (Bing → DuckDuckGo), free tier only.

Hermes-style lessons applied (no paid APIs):
- engine registry instead of hardcoded if/else chain
- TTL cache + in-flight de-dup (identical concurrent queries share one request)
- honest failure: distinguish "no results" from "all engines failed"
"""
import json, re, threading, time, urllib.request, urllib.parse
from .registry import register_tool

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
BING_CDN_DOMAINS = ("r.bing.com", "th.bing.com", "www.bing.com")
TIMEOUT = 10
CACHE_TTL = 600          # seconds, same query within TTL returns cached
MAX_RESULTS_CAP = 10     # HTML scraping: beyond ~10 the value drops sharply
RESULT_BUCKETS = (5, 10)  # snap max_results to sensible buckets

# ---------------------------------------------------------------- fetch

def _fetch(url: str, timeout: int = TIMEOUT) -> str | None:
    """Fetch URL via httpx, fallback urllib."""
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": UA})
        if r.status_code == 200 and len(r.text) > 100:
            return r.text
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=max(1, timeout - 2))
        raw = resp.read().decode("utf-8", errors="replace")
        if raw and len(raw) > 100:
            return raw
    except Exception:
        pass
    return None

# ---------------------------------------------------------------- extractors

def _clean_text(s: str) -> str:
    s = s.replace('&ensp;', ' ').replace('&nbsp;', ' ')
    s = re.sub(r'&#\d+;', ' ', s)
    s = re.sub(r'&[a-z]+;', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def _is_bing_cdn(url: str) -> bool:
    """Check if URL is Bing's own CDN / favicon / tracking."""
    return any(domain in url.lower() for domain in BING_CDN_DOMAINS)

def _extract_bing(html: str, n: int) -> list:
    """Extract up to n search results from Bing HTML."""
    out = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', html, re.DOTALL):
        block = m.group()
        h2_m = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
        if not h2_m:
            continue
        link_m = re.search(r'<a\s+[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', h2_m.group(1), re.DOTALL)
        if not link_m:
            continue
        url = link_m.group(1)
        title = _clean_text(link_m.group(2))
        if _is_bing_cdn(url) or not title or len(title) < 3:
            continue
        snip = ""
        cap_m = re.search(r'<div class="b_caption"[^>]*>(.*?)</div>', block, re.DOTALL)
        if cap_m:
            p_m = re.search(r'<p[^>]*>(.*?)</p>', cap_m.group(1), re.DOTALL)
            if p_m:
                snip = _clean_text(p_m.group(1))
        out.append({"title": title, "url": url, "snippet": snip})
        if len(out) >= n:
            break
    return out

def _extract_ddg(html: str, n: int) -> list:
    """Extract up to n search results from DuckDuckGo HTML."""
    out = []
    for u, t, s in re.findall(
            r'class="result__body".*?class="result__title".*?href="(.*?)".*?>(.*?)</a>.*?class="result__snippet".*?>(.*?)</',
            html, re.DOTALL)[:n]:
        title = _clean_text(t)
        snippet = _clean_text(s)
        if title and u:
            out.append({"title": title, "url": u, "snippet": snippet})
    return out

def _extract_ddg_lite(html: str, n: int) -> list:
    """Extract from lite.duckduckgo.com (plain table layout)."""
    out = []
    for m in re.finditer(r"<a rel=\"nofollow\" href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>", html, re.DOTALL):
        url, title = m.group(1), _clean_text(m.group(2))
        if not title or _is_bing_cdn(url):
            continue
        out.append({"title": title, "url": url, "snippet": ""})
        if len(out) >= n:
            break
    return out

def _extract_generic(html: str, n: int) -> list:
    """Generic fallback: any <a> tag with a URL."""
    out = []
    seen = set()
    for u, t in re.findall(r'<a\s+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
        ct = _clean_text(t)
        if ct and u not in seen and len(ct) > 5 and not _is_bing_cdn(u) and '.css' not in u:
            seen.add(u)
            out.append({"title": ct, "url": u, "snippet": ""})
            if len(out) >= n:
                break
    return out

# ---------------------------------------------------------------- engine registry
# Each engine: dict(build=url_builder, extract=list of (extractor, label) tried in order)

def _url_bing(q: str, n: int) -> str:
    return f"https://www.bing.com/search?q={urllib.parse.quote(q)}&count={n}"

def _url_ddg(q: str, n: int) -> str:
    return f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}"

def _url_ddg_lite(q: str, n: int) -> str:
    return f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(q)}"

_ENGINES = {
    "bing": {"build": _url_bing,
             "extract": [(_extract_bing, "bing"), (_extract_generic, "generic")]},
    "ddg": {"build": _url_ddg,
            "extract": [(_extract_ddg, "ddg"), (_extract_generic, "generic")]},
    "ddg-lite": {"build": _url_ddg_lite,
                 "extract": [(_extract_ddg_lite, "ddg-lite"), (_extract_generic, "generic")]},
}

ENGINE_ORDER = ("bing", "ddg", "ddg-lite")   # free tier ladder

def _search_one(engine: str, q: str, n: int):
    """Run one engine. Returns (results|None, error_reason|None)."""
    spec = _ENGINES[engine]
    html = _fetch(spec["build"](q, n), timeout=TIMEOUT)
    if not html:
        return None, "fetch failed / blocked"
    for extractor, label in spec["extract"]:
        results = extractor(html, n)
        if results:
            return results, None
    return None, "page fetched but no results parsed"

# ---------------------------------------------------------------- cache + single-flight

_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()
_inflight: dict[str, threading.Lock] = {}
_inflight_guard = threading.Lock()

def _cache_key(q: str, n: int) -> str:
    return f"{q.strip().lower()}|{n}"

def _cache_get(key: str):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        if hit:
            del _cache[key]
    return None

def _cache_put(key: str, results: list):
    with _cache_lock:
        _cache[key] = (time.time(), results)

def _run_single_flight(key: str, fn):
    """Concurrent identical queries wait on the same lock — one real request."""
    with _inflight_guard:
        lock = _inflight.setdefault(key, threading.Lock())
    holder = lock.acquire(blocking=False)
    try:
        if holder:
            # we are the leader: do the real work
            result = fn()
            if isinstance(result, list):
                _cache_put(key, result)
            return result
        # follower: wait for leader, then read cache
        with lock:
            pass
        hit = _cache_get(key)
        return hit if hit is not None else fn()  # leader failed; try ourselves
    finally:
        if holder:
            lock.release()
            with _inflight_guard:
                if _inflight.get(key) is lock and not lock.locked():
                    _inflight.pop(key, None)

# ---------------------------------------------------------------- main tool

def _snap_results(n: int) -> int:
    n = max(1, min(int(n or 5), MAX_RESULTS_CAP))
    for b in RESULT_BUCKETS:
        if n <= b:
            return b
    return n

@register_tool(
    name="web_search",
    description="搜索互联网获取最新信息。自动尝试 Bing → DuckDuckGo 多引擎。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "返回数量 (默认5)", "default": 5}
        },
        "required": ["query"]
    }
)
def web_search(query: str, max_results: int = 5):
    """Search the web via free engines. Returns list of results, or dict with
    {"error": ...} when every engine failed (never a silent empty list)."""
    n = _snap_results(max_results)
    key = _cache_key(query, n)

    hit = _cache_get(key)
    if hit is not None:
        return hit

    tried = []

    def _run():
        for engine in ENGINE_ORDER:
            results, reason = _search_one(engine, query, n)
            if results:
                return results
            tried.append(f"{engine}: {reason}")
        return {"error": "all engines failed", "engines_tried": tried}

    return _run_single_flight(key, _run)
