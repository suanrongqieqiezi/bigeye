#!/usr/bin/env python3
"""hotspot — B站/抖音热点雷达（借鉴 dsh-hotspot 的三个机制）。

三个机制：
  1. 多源双层回退：官方公开接口直连为主 → 第三方聚合兜底；
  2. 流传度榜：统计「一个词被多少条不同榜单条目复用」×「跨天出现」×「平台覆盖」；
  3. 跨天差分：每次抓取落盘快照，和上一次/昨日对比出 新进/掉出/名次升降。

流传度公式（照搬，可解释整数）：
    流传分 = 复用条目数 + 2 × (出现天数 − 1) + 3 × (平台数 − 1)
  只用「短语」（B站热搜词 + 抖音热榜条目），视频不参与 —— 视频是作品，不是被复用的表达。

数据落盘：data/hotspot/snapshots/YYYY-MM-DD.json
失败降级：全部源失败时读最近一份快照，返回 stale=True + 快照日期。

用法：
    hotspot(mode="all")        # 榜单 + 流传榜 + 趋势
    hotspot(mode="hot")        # 只看榜单
    hotspot(mode="spread")     # 只看流传度榜
    hotspot(mode="trend")      # 只看跨天差分
"""
import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta

from .registry import register_tool

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data", "hotspot")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ─────────────────────────── 抓取层 ───────────────────────────

def _http(url, referer="https://www.bilibili.com/", timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _num(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def fetch_bili_hotkey():
    """B站热搜广场（短语）"""
    raw = _http("https://api.bilibili.com/x/web-interface/search/square?limit=50")
    d = json.loads(raw)
    lst = (d.get("data") or {}).get("trending", {}).get("list") or []
    items = []
    for i, it in enumerate(lst, 1):
        kw = (it.get("keyword") or "").strip()
        if not kw:
            continue
        items.append({
            "id": "bili-kw:" + kw, "source": "bili", "kind": "keyword",
            "rank": i, "title": kw, "url": "",
            "heat": _num(it.get("heat_score")), "author": "", "category": "",
        })
    if not items:
        raise RuntimeError("B站热搜返回空")
    return {"provider": "bili-search-square", "label": "B站热搜", "kind": "keyword", "items": items}


def fetch_douyin_hot():
    """抖音热榜（短语）—— 免鉴权实测可用"""
    url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
           "?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1")
    raw = _http(url, referer="https://www.douyin.com/")
    d = json.loads(raw)
    wl = (d.get("data") or {}).get("word_list") or []
    items = []
    for i, it in enumerate(wl, 1):
        w = (it.get("word") or "").strip()
        if not w:
            continue
        items.append({
            "id": "dy-hot:" + w, "source": "douyin", "kind": "hot",
            "rank": i, "title": w, "url": "https://www.douyin.com/hot/" + str(it.get("sentence_id") or ""),
            "heat": _num(it.get("hot_value")), "author": "",
            "category": it.get("label") or "",
        })
    if not items:
        raise RuntimeError("抖音热榜返回空")
    return {"provider": "douyin-hot-search", "label": "抖音热榜", "kind": "hot", "items": items}


def fetch_bili_popular():
    """B站热门（视频）"""
    raw = _http("https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1")
    d = json.loads(raw)
    lst = (d.get("data") or {}).get("list") or []
    items = []
    for i, it in enumerate(lst, 1):
        items.append({
            "id": "bili-bv:" + str(it.get("bvid") or ""), "source": "bili", "kind": "video",
            "rank": i, "title": it.get("title") or "", "url": it.get("short_link_v2") or "",
            "heat": _num((it.get("stat") or {}).get("view")),
            "author": (it.get("owner") or {}).get("name") or "",
            "category": it.get("tname") or "",
        })
    if not items:
        raise RuntimeError("B站热门返回空")
    return {"provider": "bili-popular", "label": "B站热门", "kind": "video", "items": items}


def fetch_douyin_fallback():
    """第三方兜底：60s 抖音榜"""
    raw = _http("https://60s.viki.moe/v2/douyin", referer="https://www.baidu.com/")
    d = json.loads(raw)
    payload = d.get("data")
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("list") or payload.get("word_list") or payload.get("items") or []
    else:
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    items = []
    for i, it in enumerate(rows, 1):
        w = it.get("word") or it.get("title") or it.get("name") or ""
        if not w:
            continue
        items.append({
            "id": "dy-hot:" + w, "source": "douyin", "kind": "hot", "rank": i,
            "title": w, "url": it.get("url") or "",
            "heat": _num(it.get("hot_value") or it.get("hot") or it.get("score")),
            "author": "", "category": "",
        })
    if not items:
        raise RuntimeError("60s 抖音兜底返回空")
    return {"provider": "60s-viki-moe", "label": "抖音热榜(第三方)", "kind": "hot", "items": items}


# 槽位 → provider 优先级列表（第一层官方直连，第二层第三方兜底）
SLOTS = {
    "bili_keywords": [fetch_bili_hotkey],
    "douyin_hot": [fetch_douyin_hot, fetch_douyin_fallback],
    "bili_videos": [fetch_bili_popular],
}


def run_slot(providers):
    attempts = []
    for fn in providers:
        t0 = time.time()
        try:
            res = fn()
            cost = int((time.time() - t0) * 1000)
            attempts.append({"provider": res["provider"], "ok": True, "count": len(res["items"]), "ms": cost})
            res["attempts"] = attempts
            return res, attempts
        except Exception as e:
            cost = int((time.time() - t0) * 1000)
            attempts.append({"provider": fn.__name__, "ok": False,
                             "error": f"{type(e).__name__}: {e}", "ms": cost})
    raise RuntimeError("槽位全部 provider 失败: " + json.dumps(attempts, ensure_ascii=False))


def collect():
    """抓全部槽位，返回 {slot: {provider,label,kind,items,attempts}} + 失败原因"""
    sources, errors = {}, []
    for slot, providers in SLOTS.items():
        try:
            res, _ = run_slot(providers)
            sources[slot] = res
        except Exception as e:
            errors.append({"slot": slot, "error": str(e)})
    return sources, errors


# ─────────────────────────── 流传度算法 ───────────────────────────

STOPWORDS = {
    "请查收", "请收好", "一定要", "你知道吗", "是什么", "怎么办", "真的会",
    "建议", "提醒", "注意", "警惕", "攻略", "指南", "盘点", "合集",
    "中国", "美国", "日本", "韩国", "俄罗斯", "英国", "法国", "德国", "印度",
    "全球", "世界", "全国", "国际", "官方", "网友", "现场", "记者", "媒体",
    "最新", "今日", "今年", "去年", "目前", "近日", "当天", "昨天", "今天",
    "视频", "直播", "报道", "新闻", "消息", "数据", "结果", "原因", "影响",
    "男子", "女子", "老人", "孩子", "学生", "家长", "市民", "居民", "员工",
    "公司", "企业", "市场", "政府", "国家", "警方", "玩家", "观众", "粉丝",
    "团队", "活动", "时间", "问题", "情况", "地方", "事情", "方式", "内容",
    "一个", "我们", "你们", "他们", "这样", "那样", "现在", "已经", "可以",
    "B站", "b站", "抖音", "微博", "快手", "小红书", "知乎", "淘宝", "微信",
    "视频号", "bilibili", "douyin", "weibo", "央视频", "央视",
    # 内容通用词：出现在标题里但不能当选题钩子
    "新歌", "回归", "挑战", "上线", "官宣", "首发", "首播", "直播中",
    "大秀", "新专辑", "演唱会", "综艺", "正片", "花絮", "预告",
    # 疑问/句式残片：LCS 抄到的高频疑问词，没有选题价值
    "为何", "如何", "什么", "怎么", "哪些", "有多", "到底", "竟然",
    "真的", "一下", "一起", "之后", "之前", "时候", "不会", "不能", "没有",
    # 领域类别词（保留事件词，仅排掉纯类别）
    "穿搭", "美食", "热搜", "热榜", "排行",
    "UP主", "up主", "一口气看完", "合集视频", "搞笑视频",
}
MAX_TERM_LEN = 8
CJK = re.compile(r"[\u4e00-\u9fa5]")
# 标点/空白：出现即不是词
PUNCT = re.compile(r"[\s,，。.、!！?？:：;；\"'“”‘’()（）\[\]【】<>《》/\\|~·@#$%^&*+=_]")


def _ok_term(t):
    t = (t or "").strip()
    if len(t) < 2 or len(t) > MAX_TERM_LEN:
        return False
    if PUNCT.search(t):
        return False
    if t.isdigit():
        return False
    if t in STOPWORDS:
        return False
    if CJK.search(t):
        # 混着中文却以数字收尾/开头的是 LCS 蹭出的残片（如「定档1」）
        if t[0].isdigit() or t[-1].isdigit():
            return False
        if re.search(r"\d", t):
            parts = [p for p in re.split(r"\d+", t) if p]
            if any(len(p) < 2 for p in parts):
                return False
        return True
    # 纯拉丁/数字：2 字缩写（DA / MV / V）碰撞率太高，要求 ≥3 且含字母
    if len(t) < 3 or not re.search(r"[A-Za-z]", t):
        return False
    # 大小写混写的三字母词（Spa ← SpaceX/Spark）也是碰撞，只收全大写或 ≥4 位
    if len(t) == 3 and t != t.upper():
        return False
    return True


def _lcs(a, b):
    """最长公共子串（极大公共子串法，避免 n-gram 碎成错位片段）"""
    if not a or not b:
        return ""
    best, end, prev = 0, 0, [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, end = cur[j], i
        prev = cur
    return a[end - best:end] if best >= 2 else ""


def candidate_terms(phrases):
    """从今日短语条目里抽出候选词：两两求极大公共子串 + 短语自身（≥3字）"""
    cands = set()
    for i in range(len(phrases)):
        t = phrases[i].strip()
        if len(t) >= 3 and _ok_term(t):
            cands.add(t)
        for j in range(i + 1, len(phrases)):
            s = _lcs(t, phrases[j].strip()).strip()
            if s and _ok_term(s):
                cands.add(s)
    return cands


def min_usage(term):
    """2 字纯中文片段容易偶然重合，要求被更多条目复用"""
    return 3 if re.fullmatch(r"[\u4e00-\u9fa5]{2}", term) else 2


def phrase_items(snapshot):
    """流传度只看短语：B站热搜词 + 抖音热榜，不含视频"""
    out = []
    for slot in ("bili_keywords", "douyin_hot"):
        src = (snapshot.get("sources") or {}).get(slot) or {}
        for it in src.get("items") or []:
            out.append(it)
    return out


def build_spread(snapshots, limit=40):
    """snapshots: [(date_str, snapshot)] 按日期升序。返回流传度榜。"""
    if not snapshots:
        return {"rows": [], "days": 0}
    today_items = phrase_items(snapshots[-1][1])
    today_texts = [it["title"] for it in today_items]
    cands = candidate_terms(today_texts)

    hist = []
    for d, snap in snapshots:
        hist.append((d, [it["title"] for it in phrase_items(snap)]))

    rows = []
    for term in cands:
        uses = [it for it in today_items if term in it["title"]]
        if len(uses) < min_usage(term):
            continue
        days = [d for d, texts in hist if any(term in t for t in texts)]
        platforms = sorted({it["source"] for it in uses})
        score = len(uses) + 2 * (len(days) - 1) + 3 * (len(platforms) - 1)
        rows.append({
            "term": term, "score": score, "uses": len(uses),
            "days": len(days), "platforms": platforms,
            "members": [{"title": it["title"], "source": it["source"], "rank": it["rank"]}
                        for it in uses[:12]],
        })
    rows.sort(key=lambda r: (-r["score"], -r["uses"], r["term"]))
    # 去冗余：若某词是另一个更高分词的子串（`DA` ⊆ `KDA回归`、`瑞士轮` ⊆ `S16瑞士轮`），
    # 它没带来新信息，直接剔除，避免同一件事占满榜单。
    kept = []
    for r in rows:
        if any(r["term"] != k["term"] and (r["term"] in k["term"] or k["term"] in r["term"])
               for k in kept):
            continue
        kept.append(r)
    return {"rows": kept[:limit], "candidates": len(cands), "days": len(snapshots)}


# ─────────────────────────── 快照与差分 ───────────────────────────

def _snap_path(d):
    return os.path.join(SNAP_DIR, d + ".json")


def save_snapshot(snapshot):
    """落盘今日快照；写入前把上一版（可能是同一天的上一次抓取）存为 prev.json，
    这样同一天跑第二次就能看到真实差分，而不是只能等明天。"""
    os.makedirs(SNAP_DIR, exist_ok=True)
    d = snapshot["date"]
    p = _snap_path(d)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("capturedAt") != snapshot.get("capturedAt"):
                with open(os.path.join(DATA_DIR, "prev.json"), "w", encoding="utf-8") as f:
                    json.dump(old, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    with open(p, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)


def load_prev_snapshot():
    """上一份快照（同日或跨日），用于差分。"""
    p = os.path.join(DATA_DIR, "prev.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def load_recent_snapshots(n=7):
    """按日期升序取最近 n 天快照（含今天）"""
    if not os.path.isdir(SNAP_DIR):
        return []
    files = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".json"))
    out = []
    for f in files[-n:]:
        try:
            with open(os.path.join(SNAP_DIR, f), encoding="utf-8") as fh:
                out.append((f[:-5], json.load(fh)))
        except Exception:
            pass
    return out


def diff_ranks(cur_items, prev_items, top=15):
    """同一 slot 内按 id 对比名次：新进 / 掉出 / 升降"""
    cur = {it["id"]: it for it in cur_items}
    prev = {it["id"]: it for it in prev_items}
    new = [{"title": cur[i]["title"], "rank": cur[i]["rank"]} for i in cur if i not in prev]
    gone = [{"title": prev[i]["title"], "rank": prev[i]["rank"]} for i in prev if i not in cur]
    moved = []
    for i in cur:
        if i in prev:
            delta = prev[i]["rank"] - cur[i]["rank"]  # 正数=上升
            if delta:
                moved.append({"title": cur[i]["title"], "from": prev[i]["rank"],
                              "to": cur[i]["rank"], "delta": delta})
    new.sort(key=lambda x: x["rank"])
    gone.sort(key=lambda x: x["rank"])
    moved.sort(key=lambda x: -abs(x["delta"]))
    return {"new": new[:top], "gone": gone[:top], "moved": moved[:top]}


def build_trend(cur_snap, prev_snap):
    if not prev_snap:
        return {"available": False, "reason": "没有更早的快照可比（首次抓取只留基线）"}
    same_day = prev_snap.get("date") == cur_snap.get("date")
    out = {"available": True, "compare_with": prev_snap.get("date"),
           "compare_at": prev_snap.get("capturedAt"), "same_day": same_day, "slots": {}}
    for slot in ("bili_keywords", "douyin_hot", "bili_videos"):
        cur = ((cur_snap.get("sources") or {}).get(slot) or {}).get("items") or []
        prev = ((prev_snap.get("sources") or {}).get(slot) or {}).get("items") or []
        if cur and prev:
            out["slots"][slot] = diff_ranks(cur, prev)
    return out


# ─────────────────────────── 工具入口 ───────────────────────────

def _fmt_hot(snap):
    lines = []
    for slot, label in (("douyin_hot", "抖音热榜"), ("bili_keywords", "B站热搜"),
                        ("bili_videos", "B站热门")):
        src = (snap.get("sources") or {}).get(slot)
        if not src:
            continue
        lines.append(f"\n【{label}】via {src['provider']} · {len(src['items'])} 条")
        for it in src["items"][:10]:
            lines.append(f"  {it['rank']:>2}. {it['title'][:38]}  (热度 {it['heat']})")
    return "\n".join(lines)


def _fmt_spread(sp):
    if not sp["rows"]:
        return "\n【流传度榜】无结果（候选词被阈值过滤光，可能今日条目太少）"
    lines = [f"\n【流传度榜】候选 {sp['candidates']} 词 / 样本 {sp['days']} 天"]
    for i, r in enumerate(sp["rows"][:15], 1):
        lines.append(f"  {i:>2}. {r['term']}  分 {r['score']} = 复用{r['uses']}"
                     f" + 跨天{r['days']} + 平台{'/'.join(r['platforms'])}")
    return "\n".join(lines)


def _fmt_trend(tr):
    if not tr.get("available"):
        return "\n【趋势】" + tr.get("reason", "")
    lines = [f"\n【趋势】对比 {tr['compare_with']}{' 同日上次抓取( ' + str(tr.get('compare_at')) + ' )' if tr.get('same_day') else ''}"]
    name = {"bili_keywords": "B站热搜", "douyin_hot": "抖音热榜", "bili_videos": "B站热门"}
    for slot, d in tr["slots"].items():
        lines.append(f"  · {name.get(slot, slot)}：新进 {len(d['new'])} / 掉出 {len(d['gone'])} / 变动 {len(d['moved'])}")
        for x in d["new"][:3]:
            lines.append(f"      新进#{x['rank']} {x['title'][:30]}")
        for x in d["moved"][:3]:
            arrow = "↑" if x["delta"] > 0 else "↓"
            lines.append(f"      {arrow}{abs(x['delta'])} {x['title'][:30]} ({x['from']}→{x['to']})")
    return "\n".join(lines)


@register_tool(
    name="hotspot",
    description=(
        "热点雷达：抓 B站热搜/抖音热榜/B站热门，算「流传度榜」（一个词被多少条不同榜单条目复用，"
        "跨天+跨平台加权），并与昨日快照差分出 新进/掉出/名次升降。用于内容选题、判断什么词已扩散。"
        "mode: all=全给 / hot=只看榜单 / spread=只看流传度 / trend=只看趋势。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["all", "hot", "spread", "trend"],
                     "description": "all=榜单+流传+趋势 / hot=榜单 / spread=流传度 / trend=趋势"},
            "top": {"type": "integer", "description": "每榜返回条数，默认 10"},
            "days": {"type": "integer", "description": "流传度回溯天数，默认 7"},
        },
    },
)
def hotspot(mode: str = "all", top: int = 10, days: int = 7):
    try:
        t0 = time.time()
        today = date.today().isoformat()
        sources, errors = collect()
        snapshot = {
            "date": today,
            "capturedAt": datetime.now().isoformat(timespec="seconds"),
            "sources": sources,
            "errors": errors,
        }

        stale = False
        if not sources:
            hist = load_recent_snapshots(1)
            if not hist:
                return {"error": "所有数据源失败且无本地快照", "errors": errors}
            snapshot = hist[-1][1]
            stale = True

        if not stale:
            save_snapshot(snapshot)

        historic = [x for x in load_recent_snapshots(days) if x[0] <= snapshot["date"]]
        if not historic or historic[-1][0] != snapshot["date"]:
            historic.append((snapshot["date"], snapshot))
        prev = load_prev_snapshot()

        parts, result = [], {"date": snapshot["date"], "stale": stale, "errors": errors,
                             "attempts": {k: v.get("attempts") for k, v in sources.items()}}
        if mode in ("all", "hot"):
            parts.append(_fmt_hot(snapshot))
            result["hot"] = {slot: ({"provider": s["provider"], "count": len(s["items"])}
                                    ) for slot, s in sources.items()}
        if mode in ("all", "spread"):
            sp = build_spread(historic, limit=top)
            parts.append(_fmt_spread(sp))
            result["spread"] = sp["rows"][:top]
        if mode in ("all", "trend"):
            tr = build_trend(snapshot, prev)
            parts.append(_fmt_trend(tr))
            result["trend"] = tr

        took = int((time.time() - t0) * 1000)
        head = f"[hotspot {snapshot['date']}{' · STALE 本地快照' if stale else ''} · {took}ms]"
        if errors:
            head += "\n⚠ 部分源失败: " + "; ".join(f"{e['slot']}: {e['error'][:80]}" for e in errors)
        result["ms"] = took
        result["display"] = head + "\n".join(parts)
        return result
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ────────── 挂到折叠分组 misc ──────────
# registry.py 不能被 hot_reload 重载（reload 会清空 _tools 字典），
# 所以线上要让它发现自己，只能在导入时直接改内存里的 TOOL_GROUPS。
try:
    from . import registry as _reg

    _groups = getattr(_reg, "FOLDED_TOOL_GROUPS", None) or getattr(_reg, "TOOL_GROUPS", None)
    if isinstance(_groups, dict):
        _misc = _groups.setdefault("misc", {"description": "其他工具", "tools": []})
        if "hotspot" not in _misc.setdefault("tools", []):
            _misc["tools"].append("hotspot")
except Exception:
    pass
