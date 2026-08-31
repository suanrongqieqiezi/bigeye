#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rule_worker.py — 规则引擎自动触发 worker（常驻）
================================================
机制：
  周期采集系统状态 → 按 rules.json 匹配 → 命中就把提醒写进框架的 reminders.json
  （at=now）→ 框架自带的 reminder_worker 每秒轮询到后自动注入对话（系统消息），
  注入后自动从 reminders.json 移除 → 提醒"冒出来"。

  已提醒的 (topic_id, rule_id) 记在 fired.json 里防止重复刷屏；
  规则不再命中（我处理完了）自动从 fired.json 移除 → 提醒自然消失，不再出现。

用法：
  python rule_worker.py --once          # 只跑一轮（测试用）
  python rule_worker.py --interval 60   # 常驻，每 60 秒检查一次
  python rule_worker.py --interval 300 --topics 3   # 只检查最近 3 个话题

依赖：同目录下 state_probe.py + run_rules.py（复用其采集与匹配逻辑）
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state_probe import probe          # noqa: E402
from run_rules import build_env, format_hint, load_rules, safe_eval  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 框架的 reminders.json 在 base_dir（与 server.py 同级），reminder_worker.init 用的 ROOT_DIR
FRAMEWORK_DIR = os.path.dirname(BASE_DIR)
REMINERS_FILE = os.path.join(FRAMEWORK_DIR, "reminders.json")
FIRED_FILE = os.path.join(BASE_DIR, "fired.json")

# 默认只检查最近更新的 N 个话题（避免历史噪音刷屏）
DEFAULT_TOPICS_CHECK = 3


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_msg_count(base, topic_id):
    """精确取某话题的消息数（/api/messages）"""
    try:
        with urllib.request.urlopen(base + "/api/messages?topic_id=" + topic_id, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict):
            cnt = data.get("count") or data.get("total") or data.get("message_count")
            if cnt is None and isinstance(data.get("messages"), list):
                cnt = len(data["messages"])
            return cnt if isinstance(cnt, int) else 0
        if isinstance(data, list):
            return len(data)
        return 0
    except Exception:
        return 0


def fire_reminder(topic_id, text):
    """写一条立即到期的提醒 → 框架 reminder_worker 自动注入对话并移除"""
    reminders = load_json(REMINERS_FILE, [])
    reminders.append({
        "id": uuid.uuid4().hex[:12],
        "topic_id": topic_id,
        "text": text,
        "at": time.time(),
    })
    save_json(REMINERS_FILE, reminders)
    print(f"[rule_worker] 注入提醒 -> {topic_id}: {text[:80]}")


def check_once(base, max_topics):
    """跑一轮：采集 → 匹配 → 触发/清理。返回触发的提醒数。"""
    snap = probe(base)
    rules = load_rules()
    fired = load_json(FIRED_FILE, [])  # [{topic_id, rule_id}]
    fired = [k for k in fired if k.get("topic_id") and k.get("rule_id")]

    # 按更新时间排序取最近 N 个话题
    topics = snap.get("topics") or []
    topics = sorted(topics, key=lambda t: t.get("updated_at") or t.get("created_at") or 0, reverse=True)
    topics = topics[:max_topics]

    fired_count = 0
    for topic in topics:
        tid = topic.get("id", "")
        if not tid:
            continue
        env = build_env(snap, tid)
        # 消息数精确取（build_env 只对 topics[0] 有效）
        env["msg_count"] = fetch_msg_count(base, tid)
        env["topic_title"] = topic.get("title", "")

        for rule in rules:
            ok, result = safe_eval(rule["condition"], env)
            key = {"topic_id": tid, "rule_id": rule["id"]}
            if ok and result:
                if key not in fired:
                    hint = format_hint(rule["hint"], env)
                    text = f"[规则提醒] {rule['name']}：{hint}"
                    if rule.get("action"):
                        text += f"（建议动作：{rule['action']}）"
                    fire_reminder(tid, text)
                    fired.append(key)
                    fired_count += 1
            else:
                # 规则不再命中 → 移除已提醒记录（处理完自动消失）
                fired = [k for k in fired if not (k.get("topic_id") == tid and k.get("rule_id") == rule["id"])]

    save_json(FIRED_FILE, fired)
    print(f"[rule_worker] 本轮完成：触发 {fired_count} 条，已提醒记录 {len(fired)} 条")
    return fired_count


def check_rules_for_topic(base, topic_id):
    """事件触发版：用户发消息瞬间检查单个话题，返回命中的提醒文本列表。

    与 check_once 的区别：
      - 只检查指定话题（当前对话），不扫最近 N 个话题
      - 只返回文本，不写 reminders.json / 不落库 → APP 看不到，不刷屏
      - 处理完（规则不再命中）提醒自然消失，无需 fired.json 去重
    """
    snap = probe(base, topic_id)
    rules = load_rules()
    env = build_env(snap, topic_id)
    hits = []
    for rule in rules:
        ok, result = safe_eval(rule["condition"], env)
        if ok and result:
            hint = format_hint(rule["hint"], env)
            text = f"[规则提醒] {rule['name']}：{hint}"
            if rule.get("action"):
                text += f"（建议动作：{rule['action']}）"
            hits.append(text)
    return hits


def start_in_thread(interval=60, max_topics=DEFAULT_TOPICS_CHECK, base="http://127.0.0.1:9890"):
    """启动后台守护线程（供 server.py 挂载常驻，同 reminder_worker 模式）。

    返回线程对象；线程每 interval 秒跑一轮 check_once，异常自动容错。
    服务尚未就绪时第一次轮询会失败，sleep 后重试即可。
    """
    import threading

    def _loop():
        while True:
            try:
                check_once(base, max_topics)
            except Exception as e:
                print(f"[rule_worker] 轮询异常: {e}")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="rule_worker")
    t.start()
    print(f"[rule_worker] background worker started (interval={interval}s, topics={max_topics})")
    return t


def main():
    ap = argparse.ArgumentParser(description="规则引擎自动触发 worker")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（测试用）")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒数（默认60）")
    ap.add_argument("--topics", type=int, default=DEFAULT_TOPICS_CHECK, help="检查最近N个话题（默认3）")
    ap.add_argument("--base", default="http://127.0.0.1:9890")
    args = ap.parse_args()

    if args.once:
        check_once(args.base, args.topics)
        return

    print(f"[rule_worker] 常驻启动：每 {args.interval}s 检查最近 {args.topics} 个话题")
    while True:
        try:
            check_once(args.base, args.topics)
        except Exception as e:
            print(f"[rule_worker] 轮询异常: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
