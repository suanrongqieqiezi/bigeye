#!/usr/bin/env python3
"""
Extractor — LLM-driven entity, relation, causal, and epistemic extraction.

All extraction functions call an LLM with structured prompts and parse
the JSON output into the corresponding memory stores.

Pipeline:
  1. extract_entities(text) → [entity dicts] → entity_store.upsert
  2. extract_relations(text, entity_map) → [relation dicts] → relation_store.upsert_with_invalidation
  3. extract_epistemic(text) → 'experience'|'world'|'opinion'
  4. estimate_importance(text) → float(1-10)
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

# ── LLM call helper (mirrors memory/reflection.py) ──

def _llm_call(system_prompt, user_prompt, config=None):
    """Call LLM (non-streaming) and return response text. Inline to avoid circular dep."""
    config = config or {}
    base_url = config.get("base_url", "https://api.deepseek.com")
    api_key = config.get("api_key", "")
    model = config.get("model", "deepseek-chat")
    max_tokens = config.get("max_tokens", 1024)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    # Try config file if no api_key given
    if not api_key:
        _try_load_config(config)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        return body["choices"][0]["message"]["content"]
    except Exception as e:
        return ""


def _try_load_config(config):
    """Try to load model_config.json for API credentials."""
    for base in [
        os.path.dirname(os.path.abspath(__file__)),
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ]:
        cfg_path = os.path.join(base, "model_config.json")
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path) as f:
                    mc = json.load(f)
                if "api_key" in mc and mc["api_key"]:
                    config["api_key"] = mc["api_key"]
                if "base_url" in mc and mc["base_url"]:
                    config["base_url"] = mc["base_url"]
                if "model" in mc and mc["model"]:
                    config["model"] = mc["model"]
            except Exception:
                pass
            break


# ── Prompts ─────────────────────────────────────────

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

实体列表：{entities}

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


# ── Extraction functions ────────────────────────────

def extract_entities(text: str, config: dict = None) -> list[dict]:
    """Extract entities from text. Returns list of {name, type, aliases}."""
    if not text or len(text) < 10:
        return []
    response = _llm_call(ENTITY_EXTRACTION_PROMPT, text[:3000], config)
    try:
        entities = json.loads(response.strip())
        if isinstance(entities, list):
            return entities
    except (json.JSONDecodeError, AttributeError):
        pass
    return []


def extract_relations(text: str, entity_names: list[str],
                      config: dict = None) -> list[dict]:
    """Extract relations from text given known entity names.
    Returns list of {subject, predicate, object, object_value, edge_type, confidence, epistemic}.
    """
    if not text or len(text) < 20:
        return []
    if not entity_names:
        # Try extracting entities first
        entities = extract_entities(text, config)
        entity_names = [e["name"] for e in entities]
    if not entity_names:
        return []

    entity_list_str = ", ".join(entity_names)
    user_prompt = RELATION_EXTRACTION_PROMPT.format(entities=entity_list_str) + "\n\n文本:\n" + text[:3000]
    response = _llm_call(RELATION_EXTRACTION_PROMPT.format(entities=entity_list_str),
                         text[:3000], config)
    try:
        relations = json.loads(response.strip())
        if isinstance(relations, list):
            return relations
    except (json.JSONDecodeError, AttributeError):
        pass
    return []


def extract_epistemic(text: str, config: dict = None) -> str:
    """Classify text as 'experience' / 'world' / 'opinion'."""
    prompt = """判断下面这段记忆是哪种类型，只输出一个词：
- experience: 我亲身经历的事
- world: 客观世界的事实
- opinion: 我的主观判断/看法

文本：""" + text[:500]
    response = _llm_call("你是一个认识论分类器。", prompt, config)
    for tag in ("experience", "world", "opinion"):
        if tag in response.strip().lower():
            return tag
    return "experience"


def estimate_importance(text: str, config: dict = None) -> float:
    """Rate text importance on 1-10 scale."""
    prompt = """从1到10打分，这段记忆对一个人的自我认知有多重要？
10=改变人生的事件，7=重要记忆，5=日常，1=完全无关
只输出一个数字。

文本：""" + text[:500]
    response = _llm_call("你是一个记忆重要性评分器。", prompt, config)
    try:
        score = float(response.strip())
        return max(1.0, min(10.0, score))
    except (ValueError, AttributeError):
        return 5.0


def extract_all(text: str, config: dict = None) -> dict:
    """Full extraction pipeline: entities, relations, epistemic, importance.
    Returns:
      {"entities": [...], "relations": [...], "epistemic": "...", "importance": float}
    """
    entities = extract_entities(text, config)
    entity_names = [e["name"] for e in entities]
    relations = extract_relations(text, entity_names, config)
    epistemic = extract_epistemic(text, config)
    importance = estimate_importance(text, config)
    return {
        "entities": entities,
        "relations": relations,
        "epistemic": epistemic,
        "importance": importance,
    }
