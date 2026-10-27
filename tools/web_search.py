#!/usr/bin/env python3
"""Web search — pluggable engine registry (Bing → DuckDuckGo), free tier only.

Hermes-style lessons applied (no paid APIs):
- engine registry instead of hardcoded if/else chain
- TTL cache + in-flight de-dup (identical concurrent queries share one request)
- honest failure: distinguish "no results" from "all engines failed"
"""
import json, re, threading, time, urllib.request, urllib.parse
from .registry import register_tool, stable_state

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
BING_CDN_DOMAINS = ("r.bing.com", "th.bing.com", "www.bing.com")
TIMEOUT = 10
CACHE_TTL = 600          # seconds, same query within TTL returns cached
MAX_RESULTS_CAP = 10     # HTML scraping: beyond ~10 the value drops sharply
RESULT_BUCKETS = (5, 10)  # snap max_results to sensible buckets

# ------------------------------------------------ paid engine config (db meta)
# 付费搜索引擎（DeepSeek Search API）配置的读取口，全在 db meta 里：
#   web_search_paid_enabled = "1"/"0"     默认 0 = 完全走免费链路
#   deepseek_search_key     = sk-...
#   deepseek_search_base    = https://api.deepseek.com
# 设计原则：配置读失败/模块缺失一律当"未开启"，绝不让设置项把搜索整体搞挂。
PAID_META_ENABLED = "web_search_paid_enabled"
PAID_META_KEY = "deepseek_search_key"
PAID_META_BASE = "deepseek_search_base"
DEFAULT_PAID_BASE = "https://api.deepseek.com"
PAID_TIMEOUT = 20
PAID_COST_PER_1K = 0.75   # USD / 千次，仅用于提示文案

def paid_config() -> dict:
    """读付费引擎配置。任何异常都返回 enabled=False。"""
    cfg = {"enabled": False, "key": "", "base": DEFAULT_PAID_BASE}
    try:
        from db import get_db
        db = get_db()
        cfg["enabled"] = (db.get_meta(PAID_META_ENABLED) or "0").strip() == "1"
        cfg["key"] = (db.get_meta(PAID_META_KEY) or "").strip()
        base = (db.get_meta(PAID_META_BASE) or "").strip()
        if base:
            cfg["base"] = base.rstrip("/")
    except Exception:
        cfg["enabled"] = False
    return cfg

def paid_ready() -> bool:
    """开启且配了 key 才算就绪。"""
    c = paid_config()
    return bool(c["enabled"] and c["key"])

# 余额查询：GET {base}/user/balance（官方文档口径，跟聊天 key 同一把）
# 返回 balance_infos[0] 的 currency / total_balance / granted_balance / topped_up_balance。
# 面板会反复刷新，加个 60s 内存缓存，别把官方接口当时钟敲。
BALANCE_TTL = 60
_balance_cache = stable_state("deepseek_balance_cache", dict)   # {"at": ts, "data": {...}}
_balance_lock = stable_state("deepseek_balance_lock", threading.Lock)

def _dig(obj, path: str):
    """按 a.b.0.c 取值，任一步缺失返回 None。"""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur

def paid_balance(key: str = "", base: str = "", force: bool = False) -> dict:
    """查 DeepSeek 余额。任何失败都返回 ok=False + 人话 error，绝不抛异常。
    key/base 不传就用 db 里已保存的配置。"""
    cfg = paid_config()
    k = (key or cfg["key"] or "").strip()
    b = (base or cfg["base"] or DEFAULT_PAID_BASE).rstrip("/")
    if not k:
        return {"ok": False, "error": "未配置 API Key"}
    ck = f"{b}|{k[-6:]}"
    now = time.time()
    if not force:
        with _balance_lock:
            hit = _balance_cache.get(ck)
        if hit and now - hit["at"] < BALANCE_TTL:
            return hit["data"]
    url = f"{b}/user/balance"
    data = None
    try:
        import httpx
        r = httpx.get(url, headers={"Authorization": f"Bearer {k}", "Accept": "application/json"},
                      timeout=PAID_TIMEOUT)
        status = r.status_code
        try:
            body = r.json()
        except Exception:
            body = None
        if status != 200:
            data = {"ok": False, "status": status, "error": _paid_err_reason(status, body)}
        else:
            infos = (body or {}).get("balance_infos") or []
            first = infos[0] if infos else {}
            total = first.get("total_balance")
            data = {
                "ok": True,
                "is_available": bool((body or {}).get("is_available")),
                "currency": first.get("currency") or "CNY",
                "total": total,
                "total_num": (float(total) if str(total or "").replace(".", "", 1).isdigit() else None),
                "granted": first.get("granted_balance"),
                "topped_up": first.get("topped_up_balance"),
                "all": infos,
            }
    except Exception as e:
        data = {"ok": False, "error": f"网络请求失败（{type(e).__name__}: {e}）"}
    with _balance_lock:
        _balance_cache[ck] = {"at": now, "data": data}
    return data

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

# ---------------------------------------------------------------- paid engine
def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = PAID_TIMEOUT):
    """POST JSON，返回 (status, body_dict|None, err)。不抛异常。"""
    try:
        import httpx
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = None
        return r.status_code, body, None
    except Exception as e:
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={**headers, "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=max(5, timeout - 2)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(raw), None
                except Exception:
                    return resp.status, None, "non-json response"
        except Exception as e2:
            return None, None, f"{type(e).__name__}: {e2}"

def _paid_err_reason(status, body) -> str:
    """把 HTTP 状态翻成人话，尤其 402 = 余额不足。"""
    detail = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("type") or ""
        elif err:
            detail = str(err)
        detail = detail or str(body.get("message") or body.get("detail") or "")
    detail = (detail or "").strip()[:160]
    labels = {401: "key 无效或未授权", 402: "余额不足，需先充值",
              403: "无权限", 408: "超时", 429: "限流，稍后重试"}
    if status in labels:
        base = labels[status]
    elif status and status >= 500:
        base = f"服务端错误 {status}"
    elif status is None:
        base = "网络请求失败"
    else:
        base = f"HTTP {status}"
    return f"{base}（{detail}）" if detail else base

def search_paid(query: str, n: int):
    """调 DeepSeek /v1/tools/search。返回 (results|None, error_reason|None)。"""
    cfg = paid_config()
    if not cfg["enabled"]:
        return None, "未开启付费引擎"
    if not cfg["key"]:
        return None, "未配置 API Key"
    url = f"{cfg['base']}/v1/tools/search"
    payload = {"query": query[:128], "count": max(1, min(int(n or 5), 50)),
               "need_content": False, "ori_question": query[:128]}
    status, body, err = _http_post_json(
        url, payload, {"Authorization": f"Bearer {cfg['key']}"})
    if err:
        return None, f"网络请求失败（{err}）"
    if status != 200:
        return None, _paid_err_reason(status, body)
    items = (body or {}).get("results") or []
    out = []
    for it in items[:n]:
        url_i = (it.get("url") or "").strip()
        title = (it.get("title") or "").strip()
        if not url_i or not title:
            continue
        snip = (it.get("snippet") or it.get("summary") or "").strip()
        out.append({"title": title, "url": url_i, "snippet": snip,
                    "engine": "deepseek", "date": it.get("date"),
                    "site_name": it.get("site_name")})
    if not out:
        return None, "接口返回空结果（查询无命中）"
    return out, None


def _search_one(engine: str, q: str, n: int):
    """Run one engine. Returns (results|None, error_reason|None)."""
    if engine == "deepseek":
        return search_paid(q, n)
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
# ⚠️ stable_state：hot_reload 会 reload 本模块，缓存/锁/单飞表若被重建，会把同一份
# 状态拆到两把锁下（dict 并发读写报错），单飞也会失守（同 key 跑两个请求）。
_cache = stable_state("web_search_cache", dict)
_cache_lock = stable_state("web_search_cache_lock", threading.Lock)
_inflight = stable_state("web_search_inflight", dict)
_inflight_guard = stable_state("web_search_inflight_guard", threading.Lock)

def _cache_key(q: str, n: int) -> str:
    # 付费开关参与 key：开关切换后不吃旧缓存（否则开了还用免费结果）
    tag = "paid" if paid_ready() else "free"
    return f"{q.strip().lower()}|{n}|{tag}"

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
    description="搜索互联网获取最新信息。自动尝试 Bing → DuckDuckGo 多引擎；设置里开启付费引擎（DeepSeek Search）时优先用它。",
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
    """Search the web. Paid engine (if enabled & keyed) goes first, then the free
    ladder. Returns list of results, or dict with {"error": ...} when every engine
    failed (never a silent empty list)."""
    n = _snap_results(max_results)
    key = _cache_key(query, n)

    hit = _cache_get(key)
    if hit is not None:
        return hit

    tried = []
    order = ("deepseek",) + ENGINE_ORDER if paid_ready() else ENGINE_ORDER

    def _run():
        for engine in order:
            results, reason = _search_one(engine, query, n)
            if results:
                return results
            tried.append(f"{engine}: {reason}")
        return {"error": "all engines failed", "engines_tried": tried}

    return _run_single_flight(key, _run)
