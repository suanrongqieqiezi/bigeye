# -*- coding: utf-8 -*-
"""非任务对话的 LLM 用量台账（整理 / 收敛 / 反思 / 记忆网络）。

为什么要单独记一本账
--------------------
对话那条路已经算得很清楚了：server._accumulate_cost() → topic.total_cost，
全局汇总落 meta.total_cost_cny / meta.total_tokens。
但后台那批活儿 —— 自我备忘整理、后台自动收敛、反思回路 —— **没有 topic**，
也不属于任何一场对话。钱是真花出去的，可它一分都不进任何一条账单，
用户在界面上也就永远看不到"这些看不见的活儿一共花了多少"。

所以这里单独记：一个写口 record()，一个读口 stats()。
落 meta.mind_spend（JSON 一行）。后台调用频率很低（一次整理也就几轮到十几轮），
每笔一次 DB 写，负担可以忽略。

计价：复用 server._cost_cny()，**不在这里再抄一份价格表** ——
价格表只许有一处（server.py 顶部 _PRICING_* / _ZHIPU_PRICING），
再抄一份的结果就是改价时漏改一边、两边对不上（项目里已经栽过很多次）。
拿不到 server 模块时（例如在独立探针脚本里 import 本模块），
token 照记、金额记 0 并把这笔计入 unpriced —— 界面能老实说"有几笔没计价"，
而不是悄悄按 0 算进总额骗人。
"""
import json
import threading
import time

from db import get_db

_KEY = "mind_spend"

# kind 只允许这几个：写错了就是拼写事故，不如让它显眼地错。
KINDS = ("整理", "收敛", "反思", "记忆网络", "其他")

_LOCK = threading.RLock()


def _num(v):
    try:
        return int(float(v or 0))
    except Exception:
        return 0


def _usage_tokens(usage):
    """把各家协议的 usage 归一成 (输入, 输出, 缓存命中)。

    OpenAI 是 prompt_tokens / completion_tokens，
    Anthropic 归一后是 input_tokens / output_tokens（llm._norm_usage_anthropic）。
    两边都读，谁在就取谁 —— 少一边不至于整笔记成 0。
    """
    u = usage if isinstance(usage, dict) else {}
    inp = _num(u.get("prompt_tokens")) or _num(u.get("input_tokens"))
    out = _num(u.get("completion_tokens")) or _num(u.get("output_tokens"))
    hit = _num(u.get("prompt_cache_hit_tokens")) or _num(u.get("cache_hit_tokens"))
    if hit > inp:
        hit = inp
    return inp, out, hit


def _price_cny(inp, out, hit, model):
    """复用 server 的精确估价。返回 (金额, 是否算出来了)。"""
    import sys
    for name in ("__main__", "server"):
        mod = sys.modules.get(name)
        fn = getattr(mod, "_cost_cny", None)
        if callable(fn):
            try:
                return float(fn(inp, out, hit, model)), True
            except Exception:
                pass
    return 0.0, False


def _blank():
    return {
        "calls": 0, "tokens_in": 0, "tokens_out": 0, "cny": 0.0,
        "unpriced": 0, "since": 0, "last_ts": 0, "last_model": "",
        "by_kind": {},
    }


def _load():
    try:
        raw = get_db().get_meta(_KEY)
        doc = json.loads(raw) if raw else None
        if not isinstance(doc, dict):
            return _blank()
        out = _blank()
        out.update(doc)
        if not isinstance(out.get("by_kind"), dict):
            out["by_kind"] = {}
        return out
    except Exception:
        return _blank()


def _save(doc):
    try:
        get_db().set_meta(_KEY, json.dumps(doc, ensure_ascii=False))
        return True
    except Exception:
        return False


def record(kind, model="", usage=None, input_tokens=0, output_tokens=0):
    """记一笔后台调用。不抛异常 —— 记账失败绝不能把整理/反思本身弄死。"""
    try:
        inp, out, hit = _usage_tokens(usage)
        inp = inp or _num(input_tokens)
        out = out or _num(output_tokens)
        if inp <= 0 and out <= 0:
            # 全 0（缓存命中不报 usage 的源）也算一次调用，但别污染 token 合计。
            pass
        k = kind if kind in KINDS else "其他"
        cny, priced = _price_cny(inp, out, hit, model)
        now = int(time.time())
        with _LOCK:
            doc = _load()
            if not doc.get("since"):
                doc["since"] = now
            doc["calls"] = int(doc.get("calls") or 0) + 1
            doc["tokens_in"] = int(doc.get("tokens_in") or 0) + inp
            doc["tokens_out"] = int(doc.get("tokens_out") or 0) + out
            doc["cny"] = round(float(doc.get("cny") or 0) + cny, 6)
            if not priced:
                doc["unpriced"] = int(doc.get("unpriced") or 0) + 1
            doc["last_ts"] = now
            doc["last_model"] = model or doc.get("last_model") or ""
            bk = doc["by_kind"].setdefault(
                k, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cny": 0.0})
            bk["calls"] = int(bk.get("calls") or 0) + 1
            bk["tokens_in"] = int(bk.get("tokens_in") or 0) + inp
            bk["tokens_out"] = int(bk.get("tokens_out") or 0) + out
            bk["cny"] = round(float(bk.get("cny") or 0) + cny, 6)
            _save(doc)
        return True
    except Exception:
        return False


def stats():
    """给界面看的合计。键永远在（没记过就是 0 + since=0），前端不用判空。"""
    doc = _load()
    tok_in = int(doc.get("tokens_in") or 0)
    tok_out = int(doc.get("tokens_out") or 0)
    kinds = []
    for k, v in (doc.get("by_kind") or {}).items():
        kinds.append({
            "kind": k,
            "calls": int(v.get("calls") or 0),
            "tokens": int(v.get("tokens_in") or 0) + int(v.get("tokens_out") or 0),
            "cny": round(float(v.get("cny") or 0), 4),
        })
    kinds.sort(key=lambda x: -x["tokens"])
    return {
        "calls": int(doc.get("calls") or 0),
        "tokens": tok_in + tok_out,
        "tokens_in": tok_in,
        "tokens_out": tok_out,
        "cny": round(float(doc.get("cny") or 0), 4),
        "unpriced": int(doc.get("unpriced") or 0),
        "since": int(doc.get("since") or 0),
        "last_ts": int(doc.get("last_ts") or 0),
        "last_model": doc.get("last_model") or "",
        "kinds": kinds,
    }
