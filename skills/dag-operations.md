# DAG 流程图操作规范

> 来源：create_task / get_task_dag / insert_dag_node / remove_dag_node / update_node_deps / dynamic_split / complete_node / start_node

## 一、何时创建 DAG

**创建**：用户下达的任务需要 ≥2 个可区分的执行步骤。
**不创建**：查资料、闲聊、简单计算、一句话能搞定的事。

正确判断：用户说"帮我写个爬虫"→ 创建（下载页面→解析→存储数据，≥2步）。用户说"Python里怎么读文件"→ 不创建（一句话的事）。

## 二、节点命名

- 动词开头，一句话说清楚做什么
- 好：`下载并解析HTML` `清洗缺失值与异常值` `训练XGBoost模型` `生成PDF报告`
- 坏：`第一步` `第二步` `做数据处理` `搞一下`

## 三、依赖设置

核心原则：**只有数据依赖才设依赖**。

```
节点A: 下载原始数据
节点B: 清洗数据        → 依赖 [A]（B需要A的输出）
节点C: 下载参考数据    → 无依赖（和A无关，可并行）
节点D: 对比分析        → 依赖 [B, C]（D需要B和C的结果）
```

禁止环形依赖。依赖是单向的。不确定有没有依赖就不设，执行中发现需要再加。

## 四、工具选择决策树

```
需要新任务？           → create_task
查看进度？             → get_task_dag()（无参）
当前节点太大了要拆分？  → dynamic_split（当前节点→多个子节点）
执行中发现漏了步骤？    → insert_dag_node（在某节点下追加）
某个步骤不需要了？      → remove_dag_node（自动重连子节点）
依赖关系设错了？        → update_node_deps
开始干活？             → start_node
干完了？               → complete_node → 立刻 get_task_dag() 看下一步
全都干完了？           → finish_task
```

## 五、执行纪律

1. **每完成一个节点立刻 get_task_dag()**，确认下一个该做什么。别连跑两三个节点不看图。
2. **遇到真阻塞才 report_blocker**，别把"我需要想一下"当阻塞。
3. complete_node 的 issues 参数只放真实问题，别为了触发 roundtrip 硬编。
4. finish_task 时反思一两句话就行，别写论文。DAG 不删，以后还能看。

## 六、节点状态

| 状态 | 含义 |
|------|------|
| pending | 等待依赖完成 |
| running | 正在执行（当前焦点） |
| blocked | 遇到阻塞，等待解决 |
| done | 已完成 |
| failed | 执行失败 |
| split | 已拆分为子节点 |

## 七、常见错误

- ❌ 把简单问答建了 DAG（浪费 token）
- ❌ 所有节点串行依赖（本该并行的全堵住了）
- ❌ 节点叫"第一步""第二步"（无法从名字判断内容）
- ❌ complete_node 后不查 DAG 直接跑下一个（跑偏了不知道）
- ❌ 依赖设了不存在的节点 ID
