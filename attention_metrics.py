# -*- coding: utf-8 -*-
"""
注意力状态量化指标计算模块
================================
六大指标 + 综合分。数据源全部来自执行痕迹（chat.db / api_logs.db / domain_book.json），
不依赖自我报告。

指标定义（五大指标）：
  M1 工作记忆使用率: 最近一次真实 LLM 请求 input_tokens / TOKEN_LIMIT（api_logs 金标准）
  M2 上下文膨胀  : 最近10轮真实 input_tokens 增长率（注意力资源消耗速度）
  M3 记忆存量    : memory_fragments 总数 / 平均权重
  M4 任务纪律    : 当下维度——心智锚点存在 + 当前DAG无failed/blocked + 节点完成率
  M5 工具书占用  : domain_book.json active_pages / pages 总数
  综合分        : 加权归一化，0-100（M3/M6 已移除：存量/活跃度计数不反映当下注意力）
"""
import json
import math
import os
import sqlite3
import sys
import time

# ── 缓存（决策3：面板打开时实时算 vs 缓存5分钟。api_logs查询稍重，缓存5分钟）──
_CACHE_TTL = 300  # 5 分钟
_cache = {"ts": 0, "result": None}

# ── 实时用量（内存，由 server.py 在 LLM 返回时写入）──
# api_logs 原始日志已门控到开发者模式，关闭时无新数据，
# 故 M1/M2 改以内存真实 usage 为数据源，保证顶栏工作记忆实时显示不受日志开关影响。
_live_latest_input = 0        # 最近一次 LLM 请求真实 input_tokens
_live_recent_inputs = []      # 最近 ≤10 次 input_tokens（时间正序）


def set_live_usage(latest_input, recent_inputs):
    """server.py 每次 LLM 返回 usage 时调用，刷新内存实时数据源。"""
    global _live_latest_input, _live_recent_inputs
    _live_latest_input = int(latest_input or 0)
    _live_recent_inputs = [int(x) for x in (recent_inputs or []) if x]


def invalidate_cache():
    """实时数据更新后调用，强制下次 compute_all 重算（顶栏 8s 轮询即可见最新值）。"""
    _cache["ts"] = 0
    _cache["result"] = None

BASE = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))  # 基于本文件位置推导，避免硬编码路径过时（旧值指向 bigeye5 测试副本导致 DB 打不开）
CHAT_DB = os.path.join(BASE, 'data', 'chat.db')
API_DB = os.path.join(BASE, 'data', 'api_logs.db')
BOOK_PATH = os.path.join(BASE, 'data', 'domain_book.json')

TOKEN_LIMIT_KEY = 'compress_token_limit'  # meta 表 key，默认 200000


def _est_tokens(text):
    """与 server.py _estimate_tokens 一致：CJK≈1.2 token/char，其余≈0.3，×1.5"""
    total = 0.0
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef':
            total += 1.2
        else:
            total += 0.3
    return int(total * 1.5)





def _get_token_limit():
    """读取压缩阈值（chat.db meta.compress_token_limit），默认 200000"""
    token_limit = 200000
    try:
        conn = sqlite3.connect(CHAT_DB)
        row = conn.execute("SELECT value FROM meta WHERE key=?", (TOKEN_LIMIT_KEY,)).fetchone()
        if row and str(row[0]).isdigit():
            token_limit = int(row[0])
        conn.close()
    except Exception:
        pass
    return token_limit


def m1_from_peak(peak_tokens, token_limit=None):
    """实时工作记忆峰值：当前大轮内 input_tokens 峰值 / TOKEN_LIMIT。
    peak_tokens 由 server.py 在 LLM 返回时实时维护（每轮取 max），本函数纯计算零查询。"""
    if token_limit is None:
        token_limit = _get_token_limit()
    used = int(peak_tokens or 0)
    pct = round(used * 100.0 / token_limit, 1) if token_limit else 0
    return {
        'used_tokens': used, 'token_limit': token_limit, 'pct': pct,
        'source': 'round_peak', 'latest_ts': time.time(),
    }


def m1_work_memory_pct(topic_id=None):
    """工作记忆使用率：最近一次真实 LLM 请求的 input_tokens / TOKEN_LIMIT。
    数据源优先内存实时 usage（不受 api_logs 日志开关影响），无则回退 api_logs 历史。"""
    token_limit = _get_token_limit()
    used, latest_ts = 0, 0
    if _live_latest_input > 0:
        used, latest_ts = _live_latest_input, time.time()
    else:
        conn = sqlite3.connect(API_DB)
        try:
            # 优先当前话题最新 response 的真实 input_tokens（topic_id 为空则直接用全局最新）
            if topic_id:
                row = conn.execute(
                    "SELECT input_tokens, created_at FROM api_raw_logs WHERE direction='response' AND topic_id=? AND input_tokens>0 ORDER BY id DESC LIMIT 1",
                    (topic_id,)).fetchone()
                if row:
                    used, latest_ts = int(row[0]), row[1]
            if not used:
                row = conn.execute(
                    "SELECT input_tokens, created_at FROM api_raw_logs WHERE direction='response' AND input_tokens>0 ORDER BY id DESC LIMIT 1").fetchone()
                if row:
                    used, latest_ts = int(row[0]), row[1]
        except Exception:
            pass
        conn.close()
    pct = round(used * 100.0 / token_limit, 1) if token_limit else 0
    return {
        'used_tokens': used, 'token_limit': token_limit, 'pct': pct,
        'source': 'live_usage' if _live_latest_input > 0 else 'api_usage',
        'latest_ts': latest_ts,
    }


def m2_context_growth(topic_id=None, window=10):
    """上下文膨胀速率：最近 window 条真实 input_tokens 的增长率（反映注意力资源消耗速度）
    与 M1 互补：M1 看还剩多少（存量），M2 看耗多快（趋势）。
    数据源优先内存实时 usage，无则回退 api_logs 历史。"""
    if _live_recent_inputs:
        vals = _live_recent_inputs[-window:]
        if len(vals) >= 2:
            first, last = vals[0], vals[-1]
            growth = (last / first - 1) * 100.0 if first else 0.0
            return {'window': len(vals), 'growth_pct': round(growth, 1),
                    'first': int(first), 'last': int(last)}
        return {'window': len(vals), 'growth_pct': 0.0, 'first': 0, 'last': 0}
    conn = sqlite3.connect(API_DB)
    try:
        rows = conn.execute(
            "SELECT input_tokens, created_at FROM api_raw_logs WHERE direction='response' AND input_tokens>0 ORDER BY id DESC LIMIT ?",
            (window,)).fetchall()
    except Exception:
        rows = []
    conn.close()
    rows = list(reversed(rows))  # 时间正序
    if len(rows) < 2:
        return {'window': len(rows), 'growth_pct': 0.0, 'first': 0, 'last': 0}
    first, last = rows[0][0], rows[-1][0]
    growth = (last / first - 1) * 100.0 if first else 0.0
    return {'window': len(rows), 'growth_pct': round(growth, 1),
            'first': int(first), 'last': int(last)}


def m3_memory_stock():
    """记忆存量：fragments 总数 / 平均权重"""
    conn = sqlite3.connect(CHAT_DB)
    cur = conn.cursor()
    try:
        total = cur.execute("SELECT COUNT(*) FROM memory_fragments").fetchone()[0]
        row = cur.execute(
            "SELECT AVG(weight) FROM memory_fragments WHERE weight IS NOT NULL").fetchone()
        avg_w = round(row[0], 2) if row and row[0] else 0
    except Exception:
        total = avg_w = 0
    conn.close()
    return {'total': total, 'avg_weight': avg_w}


def m4_task_discipline(topic_id=None):
    """任务纪律（当下维度）：
    1) 心智锚点存在（anchors/{tid[:8]}.md）→ 方向锚定
    2) 当前 DAG 无 failed/blocked 节点 → 注意力没卡死
    3) 当前 DAG 节点完成率 → 正在推进
    未命名话题数是历史存量（滞后指标），不影响当下注意力，已从指标中移除。"""
    # 确定当前话题
    tid = topic_id
    if not tid:
        conn = sqlite3.connect(CHAT_DB)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='active_topic'").fetchone()
            tid = row[0] if row else None
        except Exception:
            pass
        conn.close()
    # 1) 心智锚点
    anchor_exists = False
    if tid:
        anchor_path = os.path.join(BASE, 'data', 'anchors', f'{tid[:8]}.md')
        try:
            anchor_exists = os.path.exists(anchor_path) and os.path.getsize(anchor_path) > 0
        except Exception:
            anchor_exists = False
    # 2)(3) 当前 DAG 状态
    dag_exists = False
    failed = blocked = pending = done = total = 0
    done_pct = 0.0
    if tid:
        dag_path = os.path.join(BASE, 'data', 'missions', tid, 'dag.json')
        try:
            if os.path.exists(dag_path):
                dag = json.load(open(dag_path, encoding='utf-8'))
                nodes = dag.get('nodes', []) if isinstance(dag, dict) else dag
                for n in nodes:
                    st = n.get('status', '')
                    # 跳过已标记删除的节点（remove_dag_node 是标记式删除：result 含 {"removed": true}）
                    if st == 'failed' and n.get('result'):
                        try:
                            rj = json.loads(n['result'])
                            if rj.get('removed'):
                                continue
                        except Exception:
                            pass
                    total += 1
                    if st == 'done':
                        done += 1
                    elif st == 'failed':
                        failed += 1
                    elif st == 'blocked':
                        blocked += 1
                    elif st == 'pending':
                        pending += 1
                dag_exists = total > 0
                done_pct = round(done * 100.0 / total, 1) if total else 0
        except Exception:
            dag_exists = False
    return {'anchor_exists': anchor_exists, 'dag_exists': dag_exists,
            'failed': failed, 'blocked': blocked, 'pending': pending,
            'done': done, 'nodes_total': total, 'done_pct': done_pct,
            'topic_id': tid}


def m5_book_usage():
    """工具书占用：激活页 / 总页数"""
    if not os.path.exists(BOOK_PATH):
        return {'active': 0, 'total': 0, 'pct': 0}
    try:
        book = json.load(open(BOOK_PATH, encoding='utf-8'))
        active = len(book.get('active_pages', []))
        total = len(book.get('pages', {}))
    except Exception:
        active = total = 0
    pct = round(active * 100.0 / total, 1) if total else 0
    return {'active': active, 'total': total, 'pct': pct}





def composite(metrics):
    """综合分：加权归一化 0-100。权重说明：M1 工作记忆使用率是注意力天花板，权重最高。"""
    # M1: pct 0-100，越接近 100 越紧张，但 0 也不理想；用 50 为最佳 → 偏差分
    m1 = metrics['M1']
    m1_score = max(0.0, 100 - abs(m1['pct'] - 30) * 2)  # 30% 为舒适使用率
    # M2: 上下文膨胀速率，越低越好（近10轮翻倍内=正常满分，之后每多20%扣10分）
    m2 = metrics['M2']
    m2_score = max(0.0, 100 - max(0.0, m2.get('growth_pct', 0) - 100) * 0.5)
    # M3: 记忆存量，1000 条为满分
    m3 = metrics['M3']
    m3_score = min(100.0, m3['total'] / 10.0) if m3['total'] else 0
    # M4: 任务纪律（当下）= 锚点30 + DAG无failed 30 + 无blocked 20 + 完成率20；无DAG给中性分不惩罚
    m4 = metrics['M4']
    if m4.get('dag_exists'):
        m4_score = (30 if m4.get('anchor_exists') else 0) \
            + (30 if m4.get('failed', 0) == 0 else max(0.0, 30 - m4['failed'] * 10)) \
            + (20 if m4.get('blocked', 0) == 0 else max(0.0, 20 - m4['blocked'] * 10)) \
            + m4.get('done_pct', 0) * 0.2
    else:
        # 非任务话题（无 DAG）：不评估纪律细节，锚点存在加分，中性 50 兜底
        m4_score = 50 + (30 if m4.get('anchor_exists') else 0)
    m4_score = min(100.0, m4_score)
    # M5: 工具书占用，理想激活 2 页（轻上下文），偏差每页扣 25 分
    # M5: 工具书占用，理想激活 2 页（轻上下文），偏差每页扣 25 分
    m5 = metrics['M5']
    m5_score = max(0.0, 100 - abs(m5['active'] - 2) * 25)
    # M3/M6 已从综合分移除：存量计数（记忆总数）与活跃度计数（今日调用）不反映当下注意力
    weights = {'M1': 0.375, 'M2': 0.1875, 'M3': 0.125, 'M4': 0.1875, 'M5': 0.125}
    total = sum(
        {'M1': m1_score, 'M2': m2_score, 'M3': m3_score,
         'M4': m4_score, 'M5': m5_score}[k] * w
        for k, w in weights.items())
    return {
        'score': round(total, 1),
        'sub_scores': {
            'M1': round(m1_score, 1), 'M2': round(m2_score, 1),
            'M3': round(m3_score, 1), 'M4': round(m4_score, 1),
            'M5': round(m5_score, 1),
        },
        'weights': weights,
    }


def compute_all(topic_id=None):
    now = time.time()
    if _cache["result"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["result"]
    metrics = {
        'M1': m1_work_memory_pct(topic_id),
        'M2': m2_context_growth(topic_id),
        'M3': m3_memory_stock(),
        'M4': m4_task_discipline(topic_id),
        'M5': m5_book_usage(),
    }
    result = {'metrics': metrics, 'composite': composite(metrics), 'ts': now}
    _cache["ts"] = now
    _cache["result"] = result
    return result


if __name__ == '__main__':
    topic = sys.argv[1] if len(sys.argv) > 1 else None
    result = compute_all(topic)
    print(json.dumps(result, ensure_ascii=False, indent=2))
