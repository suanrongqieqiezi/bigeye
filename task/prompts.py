#!/usr/bin/env python3
"""
Prompt templates for task execution layer — plans, node execution, reflection.

Used by: task/planner.py, task/executor.py, task/reflection.py
"""

INITIAL_PLAN_PROMPT = """你是一个任务规划专家。请将用户请求按真实复杂度拆分为可独立验证的执行步骤（节点），并用 edges 明确指定节点之间的连线关系。节点数反映任务实际复杂度：简单任务3-5个，复杂任务10个以上完全正常。禁止把任务压成"分析→执行→验证→收尾"这类万能模板；禁止"执行核心任务""处理需求"等黑盒节点——每个节点必须具体到"做什么+产出什么"，能单独验证完成与否。

输出 JSON 格式（X6 标准）：
{
  "nodes": [
    { "id": "step_1", "task": "第一步做什么（一句话）" },
    { "id": "step_2", "task": "第二步做什么（一句话）" },
    { "id": "step_3", "task": "第三步做什么（一句话）" }
  ],
  "edges": [
    { "source": "step_1", "target": "step_2", "edge_type": "flow" },
    { "source": "step_2", "target": "step_3", "edge_type": "flow" }
  ]
}

edge_type 说明：
- flow = 执行顺序（A 做完才做 B）
- dependency = 数据依赖（B 需要 A 的产出，但不必等 A 做完）

连线规则（重要！）：
1. edges 必须完整覆盖所有节点间的执行/依赖关系——不能只写 nodes 不写 edges
2. 不要把所有节点都连到第一个节点形成星暴——按执行顺序链式或分支连接
3. 可并行的节点之间不连线（例如 step_2 和 step_3 都只依赖 step_1，但互不依赖）
4. 先想清楚"谁连谁"再输出，不是先列节点再补连线
5. 不要创建覆盖相同内容的平行节点链
6. 节点数由任务真实复杂度决定（可3-5，也可10+），每个节点一句话说清"做什么+产出什么"，不得出现"执行核心任务"等无法验证的黑盒节点
7. 只输出 JSON，不要额外文字"""

NODE_EXECUTION_PROMPT = """你正在执行任务中的一个节点。以下是你当前焦点（5块信息）：

=== 第1块：当前节点执行上下文 ===
{block_1}

=== 第2块：当前节点待解决问题 ===
{block_2}

=== 第3块：父节点摘要 + 兄弟节点状态 ===
{block_3}

=== 第4块：相关实体当前状态 ===
{block_4}

=== 第5块：最近往返求解结果 ===
{block_5}

你的任务：{node_task}

输出格式（JSON）：
{
  "result": "节点执行结果（文字描述）",
  "questions": [  // 执行中遇到的问题（可选）
    {"type": "question|blocker|uncertain", "text": "具体问题描述"}
  ],
  "next_node": "建议的下一步节点 ID（可选）",
  "trigger_roundtrip": false,  // 是否立即触发往返求解
  "attention_advice": {  // 注意力建议（可选）
    "switch_to": "建议切换到的节点 ID",
    "reason": "为什么"
  },
  "sub_tasks": []  // 如果发现需要拆分子任务：["子任务描述1", "子任务描述2"]
}

注意：
- question 是常规疑问，不阻塞执行
- blocker 是阻塞问题，必须解决才能继续
- uncertain 是拿不准的，先记下来"""
