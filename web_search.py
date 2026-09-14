#!/usr/bin/env python3
"""
Web Search — direct HTTP search via Bing RSS / Sogou HTML.
No API key needed. Mimics a normal browser.
"""
import re
import html as htmlmod
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime
import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
}

TIMEOUT = 15


def _clean(text: str) -> str:
    """Strip tags and decode HTML entities."""
    text = re.sub(r"<[^>]+>", "", text)
    return htmlmod.unescape(text).strip()


def _parse_bing_rss(xml_text: str) -> list[dict]:
    """Parse Bing RSS search results."""
    results = []
    try:
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return results
        for item in channel.findall("item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pubdate = item.findtext("pubDate", "")
            if title and link:
                results.append({
                    "title": _clean(title),
                    "url": link,
                    "snippet": _clean(desc)[:300],
                    "date": pubdate,
                })
    except ET.ParseError:
        pass
    return results


def _parse_sogou(html: str) -> list[dict]:
    """Extract search results from Sogou HTML, skipping recommendations."""
    results = []
    raw_blocks = re.split(r'<div\s[^>]*class="(?:vrwrap|rb)"[^>]*>', html)

    for block in raw_blocks[1:]:
        m = re.search(
            r'<h3[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            block, re.DOTALL
        )
        if not m:
            continue
        href = m.group(1)
        title = _clean(m.group(2))
        if not title or len(title) < 3:
            continue
        if href.startswith("/link?") or href.startswith("?") or href.startswith("javascript"):
            continue

        url = "https:" + href if href.startswith("//") else href

        snippet = ""
        for pat in [
            r'<(?:p|div)\s[^>]*class="[^"]*(?:star-wiki|str_info|space-txt|abstract|str-text)[^"]*"[^>]*>(.*?)</(?:p|div)>',
            r'<p\s[^>]*class="[^"]*str[^"]*"[^>]*>(.*?)</p>',
            r'<div\s[^>]*class="[^"]*(?:fb|space|str|abstract)[^"]*"[^>]*>(.*?)</div>',
        ]:
            sm = re.search(pat, block, re.DOTALL)
            if sm:
                snippet = _clean(sm.group(1))[:300]
                break

        if any(r["url"] == url for r in results):
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= 10:
            break
    return results


def search(query: str, *, engine: str = "bing", count: int = 10) -> list[dict]:
    """
    Search the web.

    Args:
        query: Search query string.
        engine: "bing" (RSS, reliable) or "sogou" (HTML, may hit anti-bot).
        count: Max results to return.

    Returns:
        List of dicts: {title, url, snippet, date?}
    """
    if engine == "bing":
        url = f"https://cn.bing.com/search?format=rss&q={quote(query)}"
    elif engine == "sogou":
        url = f"https://www.sogou.com/web?query={quote(query)}"
    else:
        raise ValueError(f"Unknown engine: {engine}")

    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()

    if engine == "bing":
        results = _parse_bing_rss(resp.text)
    else:
        results = _parse_sogou(resp.text)

    return results[:count]


def search_text(query: str, **kwargs) -> str:
    """
    Search and return formatted text (for AI consumption).
    """
    results = search(query, **kwargs)
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    {r['url']}")
        if r.get("date"):
            lines.append(f"    {r['date']}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Python httpx tutorial"
    print(search_text(q))
