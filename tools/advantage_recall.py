# -*- coding: utf-8 -*-
"""advantage_recall — JitRL 检索端：按分数捞历史任务的优势/教训记录

背景（2026-10-27 补齐 JitRL 借鉴的"读"侧）：
- 写入侧 tools/task_advantage.py 已闭环：任务收尾 LLM 打分写进各任务工作区
  self_audit/事件账本.json（class=task_reflection，fingerprint 留空）。
- 检索缺口：audit_check 只命中 fingerprint 非空的记录，25 条打分记录全部不可见；
  任务启动也没有查经验的注入点。本模块补齐：任务开始/干敏感活前，
  按分数捞历史 advantage/lesson，喂给当次决策（JitRL 推理时查经验的降维借用）。

与 audit_check 的分工：
- audit_check = 动作前坑指纹核查（正则精确命中，记录需带 fingerprint）
- advantage_recall = 经验检索（按分数排序，读 task_reflection 打分记录）

设计（v1 诚实原则）：
- 不猜"当前任务"，不搞语义匹配（账本才 25 条，全量按分排序够用）；
- query 只做子串过滤，过滤词不藏私，返回里带 total 便于发现过滤过头；
- 任何失败只打日志返回空结果，绝不影响调用方主流程。

生效步骤（对应 skills/create-new-tool.md）:
  1. 本文件已在 tools/ 目录
  2. server.py import 区加: import tools.advantage_recall
  3. 重启服务器（或热重载）
"""
import json
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

from tools.registry import register_tool

# 运行时挂进折叠分组（与 audit_guard 同模式：内存 dict 就地追加，幂等）
from tools import registry as _reg
_misc_group = _reg.FOLDED_TOOL_GROUPS.get("misc")
if _misc_group and "advantage_recall" not in _misc_group["tools"]:
    _misc_group["tools"].append("advantage_recall")


def _iter_ledgers():
    """扫全部任务账本。账本路径与 task_advantage._ledger_path 同一约定。"""
    base = ROOT_DIR / "data" / "missions"
    if not base.exists():
        return
    for f in base.glob("*/workspace/self_audit/事件账本.json"):
        yield f


def load_reflections(min_score=None, query=None, limit=10):
    """读打分记录。返回 {records, total, scanned}；任何失败返回空骨架不抛。

    min_score: 只返回 score >= min_score 的记录；None 不过滤（含低分/无分）
    query:     子串过滤（对 advantage.text / lesson 匹配，不区分大小写）；None 不过滤
    limit:     最多返回条数（按分数从高到低；None 排最后的记录按时间新→旧）
    """
    empty = {"records": [], "total": 0, "scanned": 0}
    try:
        recs = []
        scanned = 0
        for f in _iter_ledgers():
            scanned += 1
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue  # 坏账本跳过，不中断
            topic = f.parts[-4]  # data/missions/<topic>/workspace/...
            for r in d.get("records", []):
                if not isinstance(r, dict) or r.get("class") != "task_reflection":
                    continue
                adv = r.get("advantage") or {}
                score = adv.get("score")
                text = (adv.get("text") or "").strip()
                lesson = (r.get("lesson") or "").strip()
                if min_score is not None and (score is None or score < min_score):
                    continue
                if query:
                    q = query.lower()
                    if q not in text.lower() and q not in lesson.lower():
                        continue
                recs.append({
                    "ts": r.get("ts"),
                    "topic": topic,
                    "score": score,
                    "advantage": text[:200],
                    "lesson": lesson[:300],
                })
        # 排序：有分的高→低；无分/低分沉底按时间新→旧
        recs.sort(key=lambda r: (
            -(r["score"] if isinstance(r["score"], int) else -1),
            -(r["ts"] or 0),
        ))
        return {"records": recs[: max(0, int(limit))], "total": len(recs), "scanned": scanned}
    except Exception as e:
        print(f"[advantage_recall] 失败(不影响主流程): {e}")
        return empty


@register_tool(
    name="advantage_recall",
    description="历史任务经验检索（JitRL 检索端）：按分数捞各任务收尾打分记录里的优势做法与教训。接到新任务、或干写/删/改类敏感操作前调用，看类似历史里哪个做法得分高、哪个坑别踩。账本少时全量看即可。",
    parameters={
        "type": "object",
        "properties": {
            "min_score": {"type": "integer", "description": "只看 score >= 该值的记录（如 8 = 只看高分实践）。不传=全量（低分教训也会带上）"},
            "query": {"type": "string", "description": "子串过滤，如 '打包' '判别性对照'。可选"},
            "limit": {"type": "integer", "description": "最多返回条数，默认10"},
        },
        "required": [],
    },
)
def advantage_recall(min_score=None, query=None, limit=10):
    return load_reflections(min_score=min_score, query=query, limit=limit)
