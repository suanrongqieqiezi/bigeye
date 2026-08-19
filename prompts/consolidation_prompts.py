"""
观察整合提示词（吸取 hindsight 的 consolidation 协议）

hindsight（vectorize-io）的 consolidator 用 LLM 对记忆做四种动作：
  1. 创建新观察（create）    —— 碎片无匹配 observation → 提炼成新观察
  2. 更新旧观察（update）    —— 新碎片支持/补充已有观察 → 更新观察文本+证据数
  3. 合并重复（merge）       —— 两条观察讲同一件事 → 合并成一条
  4. 标记矛盾（contradict）  —— 两条观察互相冲突 → 标记矛盾边

本文件是纯文本协议（吸取的正是这部分），不依赖 hindsight 任何基础设施。
落点映射到已有 schema：
  observation        → memory_fragments layer="knowledge"
  proof_count        → raw_count
  source_memory_ids  → source_ids
  history            → memory_archive（合并前原文归档）
"""

OBSERVATION_DECISION_PROMPT = """你正在整理自己的长期记忆。下面是一条新碎片记忆，和若干条已有的观察（observation）：

【新碎片】
{new_fragment}

【已有观察】
{existing_observations}

请判断这条新碎片应该怎么处理。选项：
1. create  —— 它讲的是全新的事，和已有观察都不相关 → 提炼成一条新观察
2. update  —— 它和某条观察讲同一件事，补充了新的细节/证据 → 更新那条观察（把新信息融进去）
3. merge   —— 它和某条观察几乎完全重复，没有新信息 → 不新增，只给那条观察加证据数
4. contradict —— 它和某条观察的说法直接冲突 → 标记矛盾，两条都保留

要求：
- 只选一个动作，输出 JSON：{{"action": "create|update|merge|contradict", "target_id": 观察id或null, "observation_text": "新观察的完整文本（create/update时填，第一人称，一句话到三句话，带证据感）", "reason": "一句话理由"}}
- update 时 observation_text 是"更新后"的完整观察文本（融合新旧信息）
- merge 时 observation_text 留空
- 没有合适观察就选 create，不要硬凑
- 只输出 JSON，不要别的

直接输出 JSON："""


def format_observation_decision_prompt(new_fragment: dict, observations: list) -> str:
    """格式化观察决策提示词。

    Args:
        new_fragment: {"id": int, "text": str, "created_at": float}
        observations: [{"id": int, "text": str, "raw_count": int}] 已有观察（knowledge 层）
    """
    frag_text = f"[id={new_fragment.get('id')}] {str(new_fragment.get('text', ''))[:300]}"
    if not observations:
        obs_text = "（暂无观察）"
    else:
        lines = []
        for o in observations:
            oid = o.get("id")
            otext = str(o.get("text", ""))[:300]
            ocnt = o.get("raw_count", 1)
            lines.append(f"[id={oid}, 证据数={ocnt}] {otext}")
        obs_text = "\n".join(lines)
    return OBSERVATION_DECISION_PROMPT.format(
        new_fragment=frag_text,
        existing_observations=obs_text
    )


MERGE_OBSERVATIONS_PROMPT = """你的长期记忆里有两条观察讲的是同一件事，需要合并成一条：

【观察A】
{obs_a}

【观察B】
{obs_b}

把两条合并成一条完整的新观察。要求：
- 保留两边的信息，去掉重复部分
- 第一人称，一句话到三句话
- 直接输出合并后的文本，不要解释

直接输出："""


def format_merge_observations_prompt(obs_a: dict, obs_b: dict) -> str:
    """格式化观察合并提示词。"""
    a_text = f"[id={obs_a.get('id')}, 证据数={obs_a.get('raw_count', 1)}] {str(obs_a.get('text', ''))[:400]}"
    b_text = f"[id={obs_b.get('id')}, 证据数={obs_b.get('raw_count', 1)}] {str(obs_b.get('text', ''))[:400]}"
    return MERGE_OBSERVATIONS_PROMPT.format(obs_a=a_text, obs_b=b_text)


# ── 知识页成文（knowledge_pages 层）──────────────────────────────

PAGE_COMPOSE_PROMPT = """你正在把自己的长期观察沉淀成一份体系化的「知识页」。

【主题】{page_key}

【该主题下的观察（N条）】
{observations}

【上一版本内容】（首次成文时为空）
{previous}

请把上面的观察整合成一段连贯的体系知识。要求：
- 保留所有关键事实/结论，去掉重复和琐碎细节
- 有结构：先说主题是什么，再分点讲具体认知（每条观察可对应一点）
- 用第一人称（我），因为这是你自己的知识
- 如果提供了上一版本：在旧版基础上融合新观察，不要丢弃旧版仍有效的内容
- 输出 JSON：{{"title": "页标题（8字以内）", "content": "成文知识（150-400字，可分段）"}}
- 只输出 JSON，不要别的

直接输出 JSON："""


def format_page_compose_prompt(page_key: str, observations: list, previous: str = None) -> str:
    """格式化知识页成文提示词。

    Args:
        page_key: 页主题标识
        observations: [str] 该主题下的观察文本列表
        previous: 上一版本内容（升版时传，首次成文 None）
    """
    obs_lines = []
    for i, t in enumerate(observations, 1):
        obs_lines.append(f"[{i}] {str(t)[:300]}")
    return PAGE_COMPOSE_PROMPT.format(
        page_key=page_key,
        observations="\n".join(obs_lines) if obs_lines else "（无）",
        previous=str(previous)[:800] if previous else "（首次成文）",
    )
