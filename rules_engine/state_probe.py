#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state_probe.py — 状态聚合层
将大眼框架的各 API 状态聚合成一个 JSON 快照，供规则引擎读取。

用法:
    python state_probe.py [--base http://127.0.0.1:9890] [--topic ID]

输出: 聚合后的 JSON 到 stdout，同时写 state_snapshot.json
"""
import argparse
import json
import os
import sys
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:9890"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE_DIR, "state_snapshot.json")


def fetch_json(base, path):
    url = base + path
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e), "_url": url}


def probe(base, topic_id=None):
    snap = {"_probe_time": None}

    # 系统信息
    sysinfo = fetch_json(base, "/api/system_status")
    snap["system"] = sysinfo if not sysinfo.get("_error") else None

    # 话题列表（任务名/时间）
    topics_raw = fetch_json(base, "/api/topics")
    topics = []
    if isinstance(topics_raw, dict) and "topics" in topics_raw:
        topics = topics_raw["topics"]
    elif isinstance(topics_raw, list):
        topics = topics_raw
    snap["topics"] = topics

    # 任务列表（含 DAG 快照）
    tasks_raw = fetch_json(base, "/api/tasks")
    tasks = []
    if isinstance(tasks_raw, dict) and "tasks" in tasks_raw:
        tasks = tasks_raw["tasks"]
    elif isinstance(tasks_raw, list):
        tasks = tasks_raw
    snap["tasks"] = tasks

    # 上下文用量
    usage = fetch_json(base, "/api/usage")
    snap["usage"] = usage if not usage.get("_error") else None

    # 工具书开关
    book = fetch_json(base, "/api/domain-book")
    snap["domain_book"] = book if not book.get("_error") else None

    # 重要事项
    matters = fetch_json(base, "/api/important-matters")
    snap["important_matters"] = matters if not matters.get("_error") else None

    # 消息数：默认取第一个话题（当前话题），也可显式指定
    if not topic_id and topics:
        topic_id = topics[0].get("id")
    if topic_id:
        msgs = fetch_json(base, "/api/messages?topic_id=" + topic_id)
        if msgs.get("_error"):
            snap["message_count"] = None
        elif isinstance(msgs, dict):
            cnt = msgs.get("count") or msgs.get("total") or msgs.get("message_count")
            if cnt is None and isinstance(msgs.get("messages"), list):
                cnt = len(msgs["messages"])
            snap["message_count"] = cnt if isinstance(cnt, int) else None
        elif isinstance(msgs, int):
            snap["message_count"] = msgs
        else:
            snap["message_count"] = None

    return snap


def summarize(snap, topic_id=None):
    """打印精简摘要，避免全量 JSON 撑爆上下文"""
    lines = []
    sysinfo = snap.get("system") or {}
    lines.append(f"system: pid={sysinfo.get('pid')} port={sysinfo.get('port')}")

    topics = snap.get("topics") or []
    if topic_id:
        topic = next((t for t in topics if str(t.get("id", "")).startswith(topic_id)), None)
    else:
        topic = topics[0] if topics else None
    if topic:
        lines.append(f"topic: id={topic.get('id')} title={topic.get('title')!r}")
    lines.append(f"topics_total: {len(topics)}")

    tasks = snap.get("tasks") or []
    lines.append(f"tasks_total: {len(tasks)}")
    if tasks:
        lines.append(f"tasks: 含 dag_snapshot")

    usage = snap.get("usage") or {}
    lines.append(f"usage: total_tokens={usage.get('total_tokens')} last_req={usage.get('last_request_tokens')}")

    book = snap.get("domain_book") or {}
    active = book.get("active_pages") or []
    lines.append(f"domain_book: active={active}")

    matters = snap.get("important_matters") or {}
    ms = matters.get("matters") if isinstance(matters, dict) else None
    lines.append(f"important_matters: {len(ms) if isinstance(ms, list) else 'n/a'}")
    lines.append(f"message_count: {snap.get('message_count')}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--topic", default=None, help="指定话题ID以拉取消息数")
    ap.add_argument("--full", action="store_true", help="打印全量 JSON（默认只打印摘要）")
    args = ap.parse_args()

    snap = probe(args.base, args.topic)

    # 全量写入文件
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)

    if args.full:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print(summarize(snap, args.topic))


if __name__ == "__main__":
    main()
