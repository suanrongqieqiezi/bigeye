#!/usr/bin/env python3
"""Web page fetching tool — read web content with multiple backends."""
import urllib.request
import urllib.error
import re
from .registry import register_tool

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
TIMEOUT = 20


def _try_urllib(url: str, timeout: int = TIMEOUT) -> str | None:
    """Try fetching via urllib. Returns text or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=timeout)
        content_type = resp.headers.get("Content-Type", "")
        raw = resp.read()
        charset = "utf-8"
        for part in content_type.split(";"):
            if "charset" in part.lower():
                charset = part.split("=")[-1].strip()
                break
        return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 503, 429):
            return None  # blocked → fallback
        return None
    except Exception:
        return None


def _try_requests(url: str, timeout: int = TIMEOUT) -> str | None:
    """Try fetching via requests library."""
    try:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA}, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 100:
            return r.text
    except Exception:
        pass
    return None


def _try_httpx(url: str, timeout: int = TIMEOUT) -> str | None:
    """Try fetching via httpx."""
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": UA})
        if r.status_code == 200 and len(r.text) > 100:
            return r.text
    except Exception:
        pass
    return None


def _try_scrapling(url: str, timeout: int = TIMEOUT) -> str | None:
    """Try fetching via Scrapling (bypasses Cloudflare etc)."""
    try:
        from scrapling.fetchers import Fetcher
        page = Fetcher.get(url, timeout=timeout)
        if page and page.status == 200:
            # .text is a property returning visible text
            if hasattr(page, 'text') and page.text and len(page.text.strip()) > 50:
                return page.text
            # Fallback: raw body or html_content
            if hasattr(page, 'body') and page.body:
                raw = page.body
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", errors="replace")
                return raw
            if hasattr(page, 'html_content') and page.html_content:
                return page.html_content
        return None
    except Exception:
        return None


def _html_to_text(html: str) -> str:
    """Strip HTML tags, limit length, clean entities."""
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'&#\d+;', ' ', text)
    if len(text) > 8000:
        text = text[:8000] + f"\n\n... [截断，原文共 {len(text)} 字符]"
    return text


@register_tool(
    name="web_fetch",
    description="读取网页内容。传入URL返回网页文本。自动尝试多种抓取方式，支持绕过Cloudflare反爬。查看文档/文章/API时使用。",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要读取的网页URL"}
        },
        "required": ["url"]
    }
)
def web_fetch(url: str):
    """Fetch a web page — httpx → requests → urllib → Scrapling fallback chain."""
    # Try 1: httpx (modern, fast)
    httpx_html = _try_httpx(url)
    if httpx_html is not None:
        text = _html_to_text(httpx_html)
        if len(text) > 80:
            return f"[{url}]\n{text}"

    # Try 2: requests (handles more sites)
    req_html = _try_requests(url)
    if req_html is not None:
        text = _html_to_text(req_html)
        if len(text) > 80:
            return f"[{url}]\n{text}"

    # Try 3: urllib (built-in, no deps)
    urllib_html = _try_urllib(url)
    if urllib_html is not None:
        text = _html_to_text(urllib_html)
        if len(text) > 80:
            return f"[{url}]\n{text}"

    # Try 4: Scrapling (bypasses Cloudflare, handles JS-heavy pages)
    scrapling_html = _try_scrapling(url)
    if scrapling_html is not None:
        text = _html_to_text(scrapling_html)
        if len(text) > 80:
            return f"[{url}] (via Scrapling)\n{text}"

    # All failed
    return f"抓取失败: {url} —— 所有抓取方式均无法获取该页面。试试 web_search 搜索关键词找到其他来源。"
