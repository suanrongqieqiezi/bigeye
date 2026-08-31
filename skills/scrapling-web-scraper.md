---
name: "scrapling-web-scraper"
description: "web_fetch 已内置 Scrapling 反爬。不要手动调 Scrapling API。"
triggers: ["网页抓取被403或503拦截", "想手写爬虫前先看这个"]
---

# scrapling-web-scraper
> web_fetch 已内置 Scrapling 反爬。不要手动调 Scrapling API。

## 正确用法

**直接用 `web_fetch(url)`**。内部已做 urllib → Scrapling 自动降级：

- urllib 成功 → 直接返回
- urllib 被 403/503/429 拦截 → 自动切 Scrapling Fetcher
- Scrapling 也失败 → 返回错误提示

不要自己 import scrapling 写爬虫代码。`web_fetch` 就是封装好的。

## 如果 web_fetch 失败

先看返回的错误信息：
- `"抓取失败: xxx —— urllib 和 Scrapling 都无法获取"` → 页面确实无法访问
- 错误原因可能是：网站全面封锁、需要登录、纯 JS 渲染

**不要**尝试装浏览器驱动、配 API key、改 adapter——这些不是你的工作范围。

## 替代方案

`web_fetch` 不行时：
1. 用 `web_search` 搜索关键词，找其他来源
2. 用 `bash` + `curl` 尝试不同 User-Agent
3. 承认拿不到，换个角度解决用户问题

## Scrapling 当前配置

- 版本：v0.4.11
- Fetcher 模式：纯 HTTP（无浏览器依赖）
- 不需要 API key、不需要 .env、不需要额外安装
