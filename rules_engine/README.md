# 规则引擎 (rules_engine)

逻辑触发规则引擎：聚合大眼系统全部可读状态 → 按 rules.json 匹配条件 → 输出带实况的提示清单。

## 文件结构

- `state_probe.py` — 状态聚合层：调 9890 端口各 API，聚合成 state_snapshot.json
- `rules.json` — 规则定义（条件表达式 + 提示 + 建议动作）
- `run_rules.py` — 匹配引擎：读快照或实时采集，按规则输出命中清单
- `rule_worker.py` — 事件触发：server 构建上下文瞬间调 check_rules_for_topic()，命中只把提醒拼进 system prompt，不写库不推APP，处理完自然消失
- `fired.json` — 已提醒记录 (topic_id, rule_id)，防重复刷屏；规则不再命中自动移除
- `state_snapshot.json` / `rules_result.json` — 运行产物

## 自动触发链路（事件触发）

```
用户发消息 → server.py _handle_chat 构建上下文瞬间
  → 调 rule_worker.check_rules_for_topic(话题) → state_probe 聚合状态 → rules.json 匹配
  → 命中 → 提醒文本插到 system prompt 最前（━━━ 规则提醒 ━━━）
  → 处理完 → 规则不再命中 → 提醒自然消失，不留痕
```

- 挂载点：server.py _handle_chat 构建 system_msg 时调用（3568-3570行附近）
- 提醒置顶：插入位置在上下文最前，防注意力分散
- 不写库不推APP：不发 reminders.json、不推消息给用户端
- 手动验证：`run_rules.py --probe --topic <topic_id>`
- 后台常驻轮询已停用（曾导致APP刷屏，用户明确要求事件触发）
## 用法

```bash
# 实时采集 + 匹配（指定话题）
D:/HughPlay/anaconda/python.exe run_rules.py --probe --topic <topic_id>

# 读已有快照匹配
D:/HughPlay/anaconda/python.exe run_rules.py --topic <topic_id>

# 只看状态摘要
D:/HughPlay/anaconda/python.exe state_probe.py --topic <topic_id>
```

## 数据源（http://127.0.0.1:9890）

| 接口 | 用途 |
|---|---|
| /api/system_status | 系统信息（pid/端口/路径） |
| /api/topics | 话题列表（任务名/时间） |
| /api/tasks | 任务 + dag_snapshot（节点/边） |
| /api/messages?topic_id= | 消息数（返回 {messages:[...]}） |
| /api/domain-book | 工具书开关状态 |
| /api/important-matters | 重要事项 |

注意：
- /api/tasks 的 topic_id 过滤参数不生效，需本地按 id 筛
- /api/usage 的 total_tokens 是历史累计（389M），不是当前水位，无当前上下文水位字段
- 无独立 DAG/思维导图 API，DAG 只能从 tasks 的 dag_snapshot 拿

## 规则变量（build_env 提供）

topic_title / topic_id / msg_count / dag_nodes / dag_edges / total_tokens /
active_pages / active_pages_count / extra_pages_open / extra_pages / task_status / ...

## 规则写法

```json
{
  "id": "topic_unnamed",
  "name": "任务未命名",
  "severity": "high",
  "condition": "topic_title == '新任务' or topic_title == '新话题' or topic_title == ''",
  "hint": "任务无身份：请先抓取意图...",
  "action": "改任务名 + 写意图brief"
}
```

- condition 是 Python 表达式，白名单求值（禁调用/属性/导入）
- hint 支持 {变量} 占位符
- severity: high/medium/low，输出按此排序

## 当前规则

1. topic_unnamed — 任务未命名（high）
2. dag_missing — 多步任务缺 DAG（high）：msg_count>=10 且 dag_nodes==0
3. book_leak — 非常驻工具书开着（medium）


## 环境坑

- cmd 下 curl 异常（exit 255），用 anaconda python: D:/HughPlay/anaconda/python.exe
- bash 工具直接调 python 输出偶发为空，用 run_python 工具或写文件再读
