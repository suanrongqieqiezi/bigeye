#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_rules.py — 规则匹配引擎
读取 state_snapshot.json（或实时采集），按 rules.json 匹配条件，输出提示清单。

用法:
    python run_rules.py [--base http://127.0.0.1:9890] [--topic ID] [--probe]
        --probe  先实时采集状态再匹配（默认读已有快照）
"""
import argparse
import json
import os
import re
import sys

from state_probe import probe

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_FILE = os.path.join(BASE_DIR, "rules.json")
SNAPSHOT_FILE = os.path.join(BASE_DIR, "state_snapshot.json")


def load_rules():
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["rules"]


def safe_eval(condition, env):
    """安全求值条件表达式。限制：只允许布尔/比较/算术/成员运算。"""
    # 白名单：只允许这些操作符和标识符
    allowed_names = set(env.keys())
    # 用 compile 做语法检查，禁止属性访问/调用/导入
    try:
        code = compile(condition, "<rule>", "eval")
        # 检查 AST 里没有危险节点
        import ast
        tree = ast.parse(condition, mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Call, ast.Attribute, ast.Import, ast.ImportFrom)):
                return False, f"危险表达式被拒绝: {condition}"
    except SyntaxError as e:
        return False, f"语法错误: {e}"

    try:
        result = eval(condition, {"__builtins__": {}}, env)
        return True, result
    except Exception as e:
        return False, f"求值异常: {e}"


def build_env(snap, topic_id=None):
    """从快照提取规则可用的变量环境。"""
    env = {}

    # 话题（当前或第一个）
    topics = snap.get("topics") or []
    env["topics_count"] = len(topics)
    if topic_id:
        topic = next((t for t in topics if t.get("id") == topic_id or str(t.get("id", "")).startswith(topic_id)), None)
    else:
        topic = topics[0] if topics else None
    env["topic_title"] = (topic or {}).get("title", "")
    env["topic_id"] = (topic or {}).get("id", "")

    # 任务（匹配话题）
    tasks = snap.get("tasks") or []
    env["tasks_count"] = len(tasks)
    task = None
    if topic and topic.get("id"):
        task = next((t for t in tasks if t.get("topic_id") == topic["id"]), None)
    env["task_status"] = (task or {}).get("status", "")
    env["task_user_request"] = (task or {}).get("user_request", "")

    # DAG 节点数
    dag_nodes = 0
    dag_edges = 0
    if task:
        dag_snap = task.get("dag_snapshot")
        if isinstance(dag_snap, str):
            try:
                dag_snap = json.loads(dag_snap)
            except Exception:
                dag_snap = None
        if isinstance(dag_snap, dict):
            nodes = dag_snap.get("nodes") or []
            edges = dag_snap.get("edges") or []
            dag_nodes = len(nodes)
            dag_edges = len(edges)
    env["dag_nodes"] = dag_nodes
    env["dag_edges"] = dag_edges

    # 节点名称列表（用于检测黑盒节点/粒度）
    node_names = []
    if task:
        dag_snap = task.get("dag_snapshot")
        if isinstance(dag_snap, str):
            try:
                dag_snap = json.loads(dag_snap)
            except Exception:
                dag_snap = None
        if isinstance(dag_snap, dict):
            nodes = dag_snap.get("nodes") or []
            for n in nodes:
                if isinstance(n, dict):
                    nm = n.get("title") or n.get("text") or n.get("name") or n.get("label") or ""
                else:
                    nm = str(n)
                if nm:
                    node_names.append(nm)
    env["node_names"] = node_names
    # 黑盒节点：名称含 执行核心/执行任务/处理/干活 等泛化词，或节点数<=4且含"执行"类
    blackbox = [nm for nm in node_names if any(k in nm for k in ["执行核心", "核心任务", "执行任务", "干活", "处理所有", "搞定", "完成所有"])]
    env["blackbox_nodes"] = blackbox
    env["blackbox_count"] = len(blackbox)

    # 消息数
    mc = snap.get("message_count")
    if isinstance(mc, dict):
        env["msg_count"] = mc.get("count", 0) or mc.get("total", 0) or 0
    elif isinstance(mc, int):
        env["msg_count"] = mc
    else:
        env["msg_count"] = 0

    # 上下文用量：无 limit/当前水位字段，仅历史累计。context_ratio 无数据源，置 None（规则里用 total_tokens 绝对阈值）
    usage = snap.get("usage") or {}
    total = usage.get("total_tokens", 0)
    env["total_tokens"] = total if isinstance(total, (int, float)) else 0
    env["last_request_tokens"] = usage.get("last_request_tokens", 0) or 0
    env["context_ratio"] = None

    # 工具书
    book = snap.get("domain_book") or {}
    pages = book.get("pages") or {}
    active = book.get("active_pages") or []
    env["active_pages"] = active
    env["active_pages_count"] = len(active)
    # 常驻页白名单
    CORE = {"core_rules", "mission_workflow"}
    extra = [p for p in active if p not in CORE]
    env["extra_pages_open"] = len(extra)
    env["extra_pages"] = ",".join(extra)

    return env


def format_hint(hint, env):
    """替换提示里的 {var} 占位符"""
    try:
        return hint.format(**env)
    except Exception:
        # 占位符失败时保留原样
        return hint


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9890")
    ap.add_argument("--topic", default=None)
    ap.add_argument("--probe", action="store_true", help="实时采集状态")
    args = ap.parse_args()

    if args.probe:
        snap = probe(args.base, args.topic)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    else:
        if not os.path.exists(SNAPSHOT_FILE):
            print("未找到快照文件，请先运行: python state_probe.py")
            sys.exit(1)
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            snap = json.load(f)

    rules = load_rules()
    env = build_env(snap, args.topic)

    hits = []
    for rule in rules:
        ok, result = safe_eval(rule["condition"], env)
        if ok and result:
            hits.append({
                "id": rule["id"],
                "name": rule["name"],
                "severity": rule.get("severity", "medium"),
                "hint": format_hint(rule["hint"], env),
                "action": rule.get("action", ""),
            })

    # 按严重度排序
    sev_order = {"high": 0, "medium": 1, "low": 2}
    hits.sort(key=lambda h: sev_order.get(h["severity"], 3))

    # 输出
    print("=" * 50)
    print(f"规则引擎匹配结果: {len(hits)} 条触发")
    print("=" * 50)
    if not hits:
        print("一切正常，无规则触发。")
    for h in hits:
        print(f"\n[{h['severity'].upper()}] {h['name']} ({h['id']})")
        print(f"  提示: {h['hint']}")
        if h["action"]:
            print(f"  动作: {h['action']}")
    print("\n" + "=" * 50)

    # 同时写结果文件
    with open("rules_result.json", "w", encoding="utf-8") as f:
        json.dump({"hits": hits, "env": {k: v for k, v in env.items() if isinstance(v, (str, int, float, bool, list))}}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
