---
name: "搜索 + 抓取标准工作流"
description: "搜索 + 抓取标准工作流"
triggers: ["需要搜索互联网最新信息"]
---

# 搜索 + 抓取标准工作流

## 什么时候用
需要获取互联网上的最新信息、新闻、数据、文档时。

## 当前网络环境（已验证 2026-07-21）
| 目标 | 状态 | 备注 |
|------|------|------|
| Bing | ✅ 通 | 需加 User-Agent（Mozilla/5.0），curl 默认不带UA会302 |
| DuckDuckGo (html版) | ❌ 超时 | 本机环境下 DDG 不可用 |
| Google | ❌ 被墙 | 直连不通 |
| Baidu | ✅ 通 | 可做中文搜索备选 |
| web_search 工具 | ✅ 通 | 优先走Bing，自动fallback到DDG（DDG超时也不影响） |
| web_fetch 工具 | ✅ 通 | urllib→Scrapling 两级fallback |

## 标准步骤

### 1. web_search（搜索）
- 先用 `web_search` 搜关键词（走Bing）
- **中文关键词在Bing上经常跑偏，搜不到立刻换百度**（`web_fetch https://www.baidu.com/s?wd=关键词`）
- 一次搜不到改关键词重试（不同措辞、不同语言）
- 最多重试 3 次
- 搜索工具本身的超时/失败不一定是工具坏了——Bing 302跳转可能导致curl返回size=0，加 -L 和 UA 即可

### 2. web_fetch（抓取内容）
- 搜索结果里有URL就抓具体内容
- `web_fetch` 自动用 urllib → Scrapling 两级fallback
- 一个网站抓不到就换另一个来源
- Baidu搜索结果可以用 `https://www.baidu.com/s?wd=关键词` 抓

### 3. 读内容 + 回答
- 从抓取的内容中提取关键信息
- 不要凭记忆回答——记忆可能过时

## 搜索结果存档规则
- web_search 找到有用的方案/列表/关键信息后，**立即 remember() 存死**
- 不依赖上下文活着。上下文整理后失忆=活该
- 搜索结果属于一次性内容，不留文件摘要，但关键结论进记忆碎片

## 注意事项
- 不要跳过搜索直接凭记忆回答
- 搜索失败时换个关键词/语言重试，不要放弃
- 如果所有搜索都超时，诚实告诉用户当前网络不可用
- **本机有Python 3.11**（路径：`C:\Users\35854\AppData\Local\Programs\Python\Python311\python.exe`），之前文档写的"无Python"是错的，已修正
- 搜索工具代码在 `tools/web_search.py`，Bing→DDG 双引擎自动fallback
- 中文搜不到别死磕Bing，直接用百度抓取
