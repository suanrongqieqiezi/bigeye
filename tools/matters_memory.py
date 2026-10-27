# -*- coding: utf-8 -*-
"""任务重要信息 ↔ 记忆网络 桥接层（2026-10-26）。

背景（用户拍板）：
「重要事项」长期以 Block 1「对我很重要的事」注入到**每一个**任务的系统提示最前面。
实测全局段 15 条里 13 条是某一次具体任务的技术细节（"政策梳理须按约定板块分类"
"远程绘图前先确认工程"…），却每次对话都要读一遍 → 持续误导工作方向。
正式降级为「该任务重要的信息」：取消全局段，只跟随任务；
同时把每条信息镜像进记忆网络，让它在**语义相关时**仍能被检索到，
而不是靠"无条件注入"续命。

落库形态（为什么是 layer=knowledge / authority_level=0）：
- knowledge 层不走 core 的 dedup 全表扫描（core 7925 条逐条算余弦 ≈ 数秒，
  会卡住工具调用），任务信息不需要那层查重。
- authority_level=0 ⇒ 不进 recall_guaranteed() 保底（那个只认 >=1 的行为准则），
  也不进 story 常驻注入 —— 换句话说：**只有语义命中才浮出**，不占常驻名额。
- topic_id 填任务归属 ⇒ _five_factor_score 里同任务 ×1.2、跨任务 ×0.85。
  这就是「任务隔离 + 连接记忆网络」的平衡点：隔离靠加权，不靠硬墙，
  真相关时别的任务也碰得到。

失败策略：所有记忆维护都是 best-effort —— 抛异常只打日志，绝不影响信息本身
的存取（tools 层依赖 memory 层，记忆库坏了不该让任务信息也写不进去）。
"""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

TAG_TASK_MATTER = "任务重要信息"
_MID_PREFIX = "mid:"


def _store():
    """取记忆库单例；不可用返回 None（调用方据此跳过，不抛）。"""
    try:
        from memory.fragment_store import get_store
        return get_store()
    except Exception as e:
        print(f"[matters_memory] 记忆库不可用: {e}")
        return None


def _mid_tag(mid):
    return _MID_PREFIX + str(mid)


def _find_by_mid(store, mid):
    """按条目 id 找对应碎片。找不到返回 None。"""
    if not store or not mid:
        return None
    try:
        rows = store.find_by_tag(_mid_tag(mid), limit=1)
        return rows[0] if rows else None
    except Exception as e:
        print(f"[matters_memory] find_by_mid 失败 mid={mid}: {e}")
        return None


def mirror_add(tid, mid, content):
    """新增任务重要信息时镜像进记忆网络。返回 fragment id 或 None。"""
    store = _store()
    if not store or not content:
        return None
    try:
        fid = store.add(
            str(content)[:200],
            source="task_matter",
            topic_id=tid or None,
            tags=f"{TAG_TASK_MATTER},{_mid_tag(mid)}",
            layer="knowledge",
            authority_level=0,      # 关键：不进保底注入
            importance=7.0,
            epistemic="world",      # 任务给定的客观约束，不是我的经验判断
        )
        return fid
    except Exception as e:
        print(f"[matters_memory] mirror_add 失败 mid={mid}: {e}")
        return None


def mirror_update(mid, content, tid=None):
    """任务重要信息内容变更时同步碎片。找不到旧碎片就补一条。返回 fid 或 None。"""
    if not content:
        return None
    store = _store()
    if not store:
        return None
    try:
        row = _find_by_mid(store, mid)
        if row:
            if store.edit(row["id"], text=str(content)[:200],
                          tags=f"{TAG_TASK_MATTER},{_mid_tag(mid)}"):
                return row["id"]
            return None
        return mirror_add(tid, mid, content)
    except Exception as e:
        print(f"[matters_memory] mirror_update 失败 mid={mid}: {e}")
        return None


def mirror_remove(mid):
    """删除任务重要信息时把碎片一起下掉（记忆不该留下已撤销的约束）。"""
    store = _store()
    if not store:
        return False
    try:
        row = _find_by_mid(store, mid)
        if not row:
            return True                      # 本来就没有 = 已达目的
        return bool(store.delete(row["id"]))
    except Exception as e:
        print(f"[matters_memory] mirror_remove 失败 mid={mid}: {e}")
        return False


def recall_for_topic(tid, limit=20):
    """取回某任务在记忆网络里的重要信息（前端/审计用，不参与注入）。"""
    store = _store()
    if not store or not tid:
        return []
    try:
        rows = store.recall_by_topic(tid, limit=limit)
        return [r for r in rows if r.get("source") == "task_matter"]
    except Exception:
        return []
