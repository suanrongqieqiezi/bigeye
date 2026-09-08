# -*- coding: utf-8 -*-
"""
OM 记忆工具 —— 秩学习规则的系统入口 (3 个工具，折叠进 misc 分组)

om_observe  : 记录一次先后/优劣观察 (surprise 门控决定是否真的写入)
om_compare  : 查询两个条目谁在先/谁优 (带 margin 置信度)
om_stats    : 各域写入率/surprise率/权重范数 (漂移探测器: surprise 长期不归零=域漂移)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.registry import register_tool
from tools.order_memory_core import DOMAINS, get_om

# 工作区路径解析(bash工具的工作区=任务workspace，OM用绝对路径，不受影响)


@register_tool(
    name="om_observe",
    description=(
        "向OM秩序记忆记录一次观察。timeline域: item_a在item_b之前发生；"
        "preference域: item_a比item_b更优/更重要。surprise门控：当前打分已符合预期则不写入"
        "(省写入、抗重复噪声)，违反才更新。返回是否写入+违反幅度。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "domain": {"type": "string", "enum": ["timeline", "preference"],
                       "description": "关系域"},
            "item_a": {"type": "string", "description": "条目a(先发生/更优)"},
            "item_b": {"type": "string", "description": "条目b(后发生/更次)"},
            "force": {"type": "boolean", "description": "无视门控强制写入(默认false)",
                      "default": False},
        },
        "required": ["domain", "item_a", "item_b"],
    },
)
def om_observe(domain: str, item_a: str, item_b: str, force: bool = False):
    om = get_om(domain)
    r = om.observe(item_a, item_b, force=force)
    om.save()
    return {"domain": domain, **r,
            "n_updates": om.n_updates, "n_observed": om.n_observed}


@register_tool(
    name="om_compare",
    description=(
        "查询OM秩序记忆：两个条目谁在先(timeline)/谁更优(preference)。"
        "margin是置信度：随训练增长，大margin=高确信。未学过的对margin≈0，慎用结论。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "domain": {"type": "string", "enum": ["timeline", "preference"],
                       "description": "关系域"},
            "item_a": {"type": "string", "description": "条目a"},
            "item_b": {"type": "string", "description": "条目b"},
        },
        "required": ["domain", "item_a", "item_b"],
    },
)
def om_compare(domain: str, item_a: str, item_b: str):
    om = get_om(domain)
    return {"domain": domain, **om.compare(item_a, item_b)}


@register_tool(
    name="om_stats",
    description=(
        "OM秩序记忆统计：各域事件数/写入率/surprise率/权重范数。"
        "surprise率长期不归零=该域持续漂移(回放需求信号)；权重范数暴涨=学习失稳信号。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "domain": {"type": "string", "enum": ["timeline", "preference"],
                       "description": "只看某域，不传看全部", "default": None},
        },
        "required": [],
    },
)
def om_stats(domain: str = None):
    domains = [domain] if domain else list(DOMAINS)
    out = {}
    for d in domains:
        om = get_om(d)
        st = om.event_stats()
        out[d] = {**st,
                  "write_rate": round(st["writes"] / st["events"], 3) if st["events"] else 0.0,
                  "surprise_rate": round(st["surprises"] / st["events"], 3) if st["events"] else 0.0,
                  "weight_norm": om.weight_norm(),
                  "n_updates": om.n_updates}
    return out
