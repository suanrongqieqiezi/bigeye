"""
叙事记忆提示词（CMN P6）

包含：
- NARRATIVE_CONSOLIDATION_PROMPT: 把碎片记忆整理成连贯故事
- THOUGHT_CHAIN_CONSOLIDATION_PROMPT: 把思维链整理成故事（后续阶段用）
"""

NARRATIVE_CONSOLIDATION_PROMPT = """你正在整理自己的记忆。下面是关于「{topic}」的几条碎片记忆，按发生时间排序：

{fragments_text}

把它们串成一段连贯的故事。要求：
- 第一人称（"我"）
- 带时间感（"那天"、"后来"、"直到"）
- 带因果（"因为...所以..."、"这次让我明白..."）
- 不是抽象总结，是你经历的叙事
- 200-400 字
- 直接输出故事正文，不要标题、不要解释、不要前缀

直接输出："""


MERGE_STORY_PROMPT = """你之前已经整理过关于「{topic}」的一段故事：

【旧故事】
{old_story}

现在你又有了关于这件事的新记忆（按时间排序）：

【新碎片】
{new_fragments}

把旧故事和新碎片合并，重新整理成一段更完整的故事。要求：
- 第一人称（"我"）
- 时间线连贯：旧故事里的事在前，新碎片接在后面（如果新碎片时间更早就插到对应位置）
- 带因果和转折（"后来我才明白"、"这次又让我确认了"）
- 不是把两段拼接，是重新讲一遍这个故事
- 250-500 字（比初次整理稍长，因为信息更丰富了）
- 直接输出故事正文，不要标题、不要解释、不要前缀

直接输出："""


THOUGHT_CHAIN_CONSOLIDATION_PROMPT = """你刚完成了一个任务，下面是执行过程中的思维链记录（每步的动机和结果）：

任务：{task_title}

{chain_text}

把这个任务的经历整理成一段故事。要求：
- 第一人称（"我"）
- 讲清楚：我遇到什么问题、为什么这么决策、最后怎样
- 带因果和反思
- 200-400 字
- 直接输出故事正文，不要标题、不要解释

直接输出："""


def format_narrative_prompt(topic: str, fragments: list) -> str:
    """格式化叙事沉淀提示词。

    Args:
        topic: 主题（从碎片聚类提取）
        fragments: [{id, text, created_at, tags}] 碎片列表，已按时间排序

    Returns:
        格式化后的提示词
    """
    import time
    lines = []
    for f in fragments:
        ts = f.get("created_at") or f.get("ts") or ""
        # created_at 是时间戳，转成可读日期
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                ts_str = dt
            except Exception:
                ts_str = str(ts)[:10]
        else:
            ts_str = str(ts)[:10]
        text = (f.get("text") or "")[:200]
        lines.append(f"[{ts_str}] {text}")

    fragments_text = "\n".join(lines)
    return NARRATIVE_CONSOLIDATION_PROMPT.format(
        topic=topic,
        fragments_text=fragments_text
    )


def format_merge_story_prompt(topic: str, old_story: str, new_fragments: list) -> str:
    """格式化合并叙事提示词（同主题已有 story 时用）。

    Args:
        topic: 主题
        old_story: 已有 story 的文本
        new_fragments: 新碎片列表 [{id, text, created_at, tags}]，已按时间排序

    Returns:
        格式化后的提示词
    """
    import time
    lines = []
    for f in new_fragments:
        ts = f.get("created_at") or f.get("ts") or ""
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                ts_str = dt
            except Exception:
                ts_str = str(ts)[:10]
        else:
            ts_str = str(ts)[:10]
        text = (f.get("text") or "")[:200]
        lines.append(f"[{ts_str}] {text}")

    new_fragments_text = "\n".join(lines)
    return MERGE_STORY_PROMPT.format(
        topic=topic,
        old_story=old_story,
        new_fragments=new_fragments_text
    )


def format_chain_consolidation_prompt(task_title: str, chain_steps: list) -> str:
    """格式化思维链沉淀提示词。

    Args:
        task_title: 任务标题
        chain_steps: [{summary, motivation, result}] 思维链步骤

    Returns:
        格式化后的提示词
    """
    lines = []
    for i, step in enumerate(chain_steps, 1):
        summary = step.get("summary", "")
        motivation = step.get("motivation", "")
        result = step.get("result", "")
        lines.append(f"步骤{i}: {summary}")
        if motivation:
            lines.append(f"  动机: {motivation}")
        if result:
            lines.append(f"  结果: {result}")

    chain_text = "\n".join(lines)
    return THOUGHT_CHAIN_CONSOLIDATION_PROMPT.format(
        task_title=task_title,
        chain_text=chain_text
    )
