#!/usr/bin/env python3
"""
Central configuration for v4.0 autobiographical memory system.

Values can be overridden via environment variables (prefixed with DAYAN_).
All keys are OPTIONAL; defaults are provided.
"""

import os


def _env(key, default):
    return os.environ.get(f"DAYAN_{key}", default)


# ── Memory Layer (L3) ──────────────────────────────

# importance ≥ this value → pinned, never decay to archive
MEMORY_IMPORTANCE_PIN_THRESHOLD = float(_env("MEMORY_IMPORTANCE_PIN_THRESHOLD", "7.0"))
# days without recall before a fragment can be archived
MEMORY_ACTIVE_RECENCY_DAYS = int(_env("MEMORY_ACTIVE_RECENCY_DAYS", "30"))
# sqlite-vec virtual index enable flag
MEMORY_VEC_INDEX_ENABLED = _env("MEMORY_VEC_INDEX_ENABLED", "true").lower() == "true"
# days without mention before entity deactivation
MEMORY_ENTITY_ACTIVE_DAYS = int(_env("MEMORY_ENTITY_ACTIVE_DAYS", "180"))
# minimum confidence for relation extraction
MEMORY_RELATION_CONFIDENCE_MIN = float(_env("MEMORY_RELATION_CONFIDENCE_MIN", "0.5"))
# minimum confidence for causal edge insertion
MEMORY_CAUSAL_EDGE_CONFIDENCE_MIN = float(_env("MEMORY_CAUSAL_EDGE_CONFIDENCE_MIN", "0.7"))
# minimum confidence for causal propagation during recall
MEMORY_CAUSAL_PROPAGATION_MIN_CONFIDENCE = float(_env("MEMORY_CAUSAL_PROPAGATION_MIN_CONFIDENCE", "0.8"))
# epistemic classification enabled
MEMORY_EPISTEMIC_ENABLED = _env("MEMORY_EPISTEMIC_ENABLED", "true").lower() == "true"
# summary tree leaf period
MEMORY_SUMMARY_LAYER0_PERIOD = _env("MEMORY_SUMMARY_LAYER0_PERIOD", "month")
# same-layer sibling count threshold for promotion
MEMORY_SUMMARY_PROMOTE_THRESHOLD = int(_env("MEMORY_SUMMARY_PROMOTE_THRESHOLD", "3"))
# recall parameters
MEMORY_RECALL_TOP_K = int(_env("MEMORY_RECALL_TOP_K", "5"))
MEMORY_RECALL_THRESHOLD = float(_env("MEMORY_RECALL_THRESHOLD", "0.5"))
MEMORY_WEIGHT_CAP = float(_env("MEMORY_WEIGHT_CAP", "3.0"))
MEMORY_DECAY_HALF_LIFE_DAYS = int(_env("MEMORY_DECAY_HALF_LIFE_DAYS", "30"))
MEMORY_RECALL_ENTITY_BONUS = float(_env("MEMORY_RECALL_ENTITY_BONUS", "1.5"))

# ── Task Execution Layer (L0-L2) ───────────────────

V4_ATTENTION_WINDOW_BLOCKS = int(_env("V4_ATTENTION_WINDOW_BLOCKS", "5"))
V4_ATTENTION_TOKEN_BUDGET = int(_env("V4_ATTENTION_TOKEN_BUDGET", "32000"))
V4_QUESTION_THRESHOLD = int(_env("V4_QUESTION_THRESHOLD", "5"))
V4_BLOCKER_IMMEDIATE = _env("V4_BLOCKER_IMMEDIATE", "true").lower() == "true"
V4_UNCERTAIN_TIMEOUT_SEC = int(_env("V4_UNCERTAIN_TIMEOUT_SEC", "30"))
V4_NODE_MAX_RETRIES = int(_env("V4_NODE_MAX_RETRIES", "3"))
V4_ROUNDTRIP_PARALLEL = _env("V4_ROUNDTRIP_PARALLEL", "true").lower() == "true"
V4_SLIDING_CACHE_ENABLED = _env("V4_SLIDING_CACHE_ENABLED", "true").lower() == "true"
V4_SEDIMENT_ON_TASK_END = _env("V4_SEDIMENT_ON_TASK_END", "true").lower() == "true"
V4_STICKY_SIBLINGS = _env("V4_STICKY_SIBLINGS", "true").lower() == "true"
