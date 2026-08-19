#!/usr/bin/env python3
"""
Prompt templates for memory layer — entity extraction, relation extraction,
causal chain detection, epistemic classification, importance scoring.

Used by: memory/extractor.py, memory/relation_store.py, memory/reflection.py
"""

ENTITY_EXTRACTION_PROMPT = """你是一个实体抽取专家。从以下文本中抽取所有有意义的命名实体。

输出 JSON 数组，每个元素包含：
{
  "name": "实体名称（规范化后的标准名称）",
  "type": "实体类型（person/place/object/concept/event）",
  "aliases": ["别名列表（包括原文中的不同表述）"]
}

要求：
- 只抽有信息量的实体（人名、地名、项目名、专有名词、重要概念）
- 规范化名称：去除多余空格，统一大小写
- 同一实体的不同表述归入 aliases
- 无实体则输出空数组 []
- 只输出 JSON，不要额外文字"""

RELATION_EXTRACTION_PROMPT = """你是一个关系抽取专家。从以下文本中抽取实体之间的语义关系。

已知实体列表：{entities}

输出 JSON 数组，每个元素包含：
{{
  "subject": "主语实体名称（必须来自实体列表）",
  "predicate": "谓语/关系描述",
  "object": "宾语实体名称（或为空，如果宾语不是已知实体）",
  "object_value": "宾语文字值（当object为空时使用）",
  "edge_type": "关系类型（fact/causal/temporal）",
  "confidence": 置信度 0.0-1.0,
  "epistemic": "认识论类型（experience/world/opinion）"
}}

edge_type 说明：
- fact: 状态/动作/属性（默认）
- causal: 因果关系（A导致B/因为A所以B）
- temporal: 纯时序关系（A先于B/A发生在B之前）

confidence 说明：
- 文本明确表述 → 0.8+
- 文本暗示 → 0.5-0.7
- 弱推断 → 0.3-0.4

要求：
- 只抽有信息量的关系
- causal 边必须原文有明确因果词（因为/所以/导致/使得/因此/于是）
- 无关系则输出空数组 []
- 只输出 JSON，不要额外文字"""

CAUSAL_CHAIN_PROMPT = """你是一个因果链发现专家。分析以下对话/反思文本，找出有因果联系的事件。

输出 JSON 数组，每个元素描述一条因果链：
{{
  "cause": "原因事件/条件",
  "effect": "结果事件/后果",
  "confidence": 置信度 0.0-1.0,
  "evidence": "原文支持证据（精确引用原文）"
}}

要求：
- causal 边必须原文有明确因果词（因为/所以/导致/使得/因此/于是/之所以）
- confidence ≥ 0.7 才应输出
- 无因果链输出空数组 []
- 只输出 JSON，不要额外文字"""

IMPORTANCE_SCORING_PROMPT = """从1到10打分，这段记忆对一个人的自我认知有多重要？
10 = 改变人生/身份的事件
9 = 重大成就/失败
8 = 重要关系变化
7 = 重要的学习/发现
6 = 有价值的反馈
5 = 一般日常
4 = 琐事
3-1 = 几乎不重要

只输出一个数字（1-10之间的整数）："""

EPISTEMIC_CLASSIFICATION_PROMPT = """判断下面这段记忆是哪种类型，只输出一个词：
- experience: 我亲身经历的事（我做过的、遇到过的）
- world: 客观世界的事实（他人告诉我的、查询到的信息）
- opinion: 我的主观判断/看法

只输出一个词：experience / world / opinion"""

SUMMARY_PROMPT = """为以下内容写一个简短的摘要（50-100字）。
保留关键事实：实体、数值、时间、因果关系。
用中文。"""

MONTHLY_SUMMARY_PROMPT = """将以下同一个月内的碎片记忆整合成一份月度摘要。
保留所有关键实体和重要事件，50-100字。
用中文。"""

QUARTERLY_SUMMARY_PROMPT = """将以下同一季度内的月度摘要整合成一份季度摘要。
保留所有关键实体和重要事件，50-100字。
用中文。"""
