# CMN 实施方案

> 晶体记忆网络工程实施方案
> 配套文档：[晶体记忆网络设计书.md](晶体记忆网络设计书.md)
> 版本：v1.0

---

## 第一章：总体策略

### 1.1 核心原则

基于设计书 §8.3"不为统一而统一，现有能用的逻辑保留"，实施方案遵循三条原则：

1. **不推翻现有系统**：FragmentStore / EntityStore / SummaryTree / RelationStore 全部保留，做字段扩展和行为改造，不重写
2. **文件晶体独立新建**：新建 FileCrystalStore 模块 + file_crystals 表，不污染自传晶体存储
3. **共享基础设施**：vec_index / embedder / extractor 全部复用，两类晶体共享同一套 hash 寻址 + 向量检索

### 1.2 摸底确认的可复用资产

| 设计书需求 | 现成基础设施 | 复用方式 |
|---|---|---|
| 内容寻址 hash | `summary_tree._hash()` + `summary_hash` 字段 | 扩展到 fragment / file_crystal 层 |
| 向量检索 | `vec_index.VecIndex`（sqlite-vec 包装） | 直接复用 |
| 三层金字塔 | `summary_tree.SummaryTree`（月/季/年 Merkle） | 改造支持文件维度 |
| 三问压缩雏形 | `memory_tools._generate_compress_summary`（结论/产出/遗留三段式） | 改提示词为"结论/为什么/下一步" |
| 认识论标记 | `epistemic` 字段（experience/world/opinion） | 直接复用，加 authority 维度 |
| 因果传播 | `fragment_store._fetch_causal_neighbors` | 直接复用，作为强关系同步建的雏形 |
| 反思触发器 | `task/reflection.reflect_and_sediment` + `memory/reflection.trigger_deep_integration_now` | 改造为四项职责 |
| 嵌套压缩树模式 | `compression_tree` 表 + `_build_tree_nav_text` | 模式参考，不直接复用 |
| 实体抽取 | `extractor.extract_entities/relations/epistemic/importance` | 直接复用，扩 edge_type |

### 1.3 阶段划分原则

- **每阶段可独立验收**：不依赖下一阶段也能跑通并验证价值
- **从最小可用开始**：先跑通流程再优化质量
- **风险前置**：摘要质量、切片粒度等高风险点尽早验证
- **不破坏现有功能**：每阶段结束保证现有 remember/recall/任务执行不受影响

---

## 第二章：阶段划分总览

| 阶段 | 名称 | 核心产出 | 验收标志 |
|------|------|---------|---------|
| **P0** | 数据模型扩展 | 表字段+迁移脚本 | 老数据兼容，新表可用 |
| **P1** | 文件晶体 MVP | 切片+单层摘要+hash检测 | 大文件能建晶体+召回 |
| **P2** | 金字塔多层 + 双链路 | 递归摘要+下钻工具 | 顶层下钻到任意层 |
| **P3** | 自传晶体对齐 CMN | remember 记晶体+三问反思 | AI 经验晶体双路可检 |
| **P4** | 反思回路四职 + 提拔权威 | 反熵+涌现+自检+权威 | 网络结构有改善 |
| **P5** | 检索统一 + 按需验证 | 双路接口+衰减信号 | 过期晶体有信号可下钻 |

---

## 第三章：P0 — 数据模型扩展

### 3.1 目标

为后续阶段铺地基。所有新字段一次性加好，避免反复迁移。

### 3.2 改动清单

#### 3.2.1 扩展 `memory_fragments` 表（[fragment_store.py](memory/fragment_store.py)）

新增字段（NULL 兼容老数据）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `crystal_parent_id` | TEXT | 派生它的上层晶体 id（思维血统） |
| `raw_source_id` | TEXT | 原始素材 id（文件切片 id / 工具输出原文 id） |
| `authority_level` | INTEGER | 0=普通，1=权威（默认 0） |
| `confidence_decay` | REAL | 置信度衰减信号（0.0~1.0，1.0=刚验证） |
| `last_hash_verified_at` | TIMESTAMP | 最后一次 hash 验证时间戳 |
| `node_type` | TEXT | 'self'（自传晶体）/ 'file'（文件晶体，预留统一） |

#### 3.2.2 扩展 `memory_relations.edge_type`（[relation_store.py](memory/relation_store.py)）

现有：`fact` / `causal` / `temporal`

新增四种：
- `derive`（派生：C 由 A+B 融合而成）
- `support`（支持：证据 A 支撑结论 B）
- `negate`（否定：新晶体推翻旧晶体，带 timestamp + reason）
- `weak_assoc`（弱关联：带 confidence + 可解释理由）

`memory_relations` 表已有 `confidence` 字段可复用。新增字段：
- `reason`（TEXT）：建边理由，弱关联和否定边必填
- `negate_timestamp`（TIMESTAMP）：否定边时间戳

#### 3.2.3 新建 `file_crystals` 表

```sql
CREATE TABLE file_crystals (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,          -- 物理路径或 URL
    source_type TEXT NOT NULL,          -- 'knowledge_base' / 'ai_downloads' / 'url'
    slice_index INTEGER NOT NULL,       -- 切片序号
    slice_range TEXT,                   -- "start:end" 字符偏移
    slice_hash TEXT NOT NULL,           -- 切片内容 SHA256
    content TEXT NOT NULL,              -- 切片原文（小文件留底，大文件仅存路径+偏移）
    summary TEXT,                       -- 三问压缩后的晶体
    embedding BLOB,                     -- 晶体摘要的向量
    layer INTEGER DEFAULT 0,            -- 金字塔层级（0=原始切片层，1=第一层摘要...）
    crystal_parent_id TEXT,             -- 上层晶体 id（金字塔血统）
    raw_source_id TEXT,                 -- 下层原始素材 id（raw_source 链）
    entity_ids TEXT,                    -- JSON 数组
    authority_level INTEGER DEFAULT 0,  -- 0=普通，1=权威
    epistemic TEXT DEFAULT 'world',     -- 文件晶体默认 world
    last_hash_verified_at TIMESTAMP,
    status TEXT DEFAULT 'active',       -- 'active' / 'stale'（hash 变化标 stale）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_fc_path ON file_crystals(source_path);
CREATE INDEX idx_fc_hash ON file_crystals(slice_hash);
CREATE INDEX idx_fc_layer ON file_crystals(layer);
CREATE INDEX idx_fc_parent ON file_crystals(crystal_parent_id);
```

#### 3.2.4 数据库迁移脚本

新建 `memory/migrations/p0_cmn_fields.sql`：
- 所有 ALTER TABLE 加字段（NULL 默认，兼容老数据）
- CREATE TABLE file_crystals
- 现有 `epistemic` 字段值不变（experience/world/opinion 保持）

在 `FragmentStore.__init__` / `RelationStore.__init__` 启动时检测字段是否存在，缺失则自动迁移。

### 3.3 验收标准

- [ ] 所有新字段添加成功，老数据查询不报错
- [ ] `file_crystals` 表创建成功
- [ ] `edge_type` 新增四种值不影响现有 fact/causal/temporal 查询
- [ ] 启动时自动检测并迁移（幂等）
- [ ] 现有 remember/recall/任务执行功能完全不受影响

### 3.4 风险与回退

- **风险**：SQLite ALTER TABLE 加字段在大表上可能慢
- **回退**：字段加失败不影响原表使用，删除迁移脚本即可

---

## 第四章：P1 — 文件晶体 MVP

### 4.1 目标

跑通"文件 → 切片 → hash → 晶体 → 召回"最小闭环。先不做金字塔多层，验证核心价值。

### 4.2 新建模块

#### 4.2.1 `memory/file_slicer.py` — 文件切片器

```python
class FileSlicer:
    def slice_file(self, path: str) -> List[Slice]:
        """按文件类型选择切分策略"""
    def _slice_code(self, content, lang) -> List[Slice]:    # 按 AST 节点边界
    def _slice_document(self, content) -> List[Slice]:       # 按段落/章节
    def _slice_generic(self, content, size=2000, overlap=200) -> List[Slice]:  # 兜底固定字符数
```

**策略**：
- 代码文件（.py/.js/.ts/.java/.go/.rs）：按函数/类边界切，单块超 4000 字二次切
- 文档文件（.md/.txt/.rst）：按 `##` 标题或空行段落切
- 其他：固定 2000 字 + 200 字重叠

**先做最简版**：代码 + 文档两种切分器，其他走兜底。

#### 4.2.2 `memory/file_crystal_store.py` — 文件晶体存储

```python
class FileCrystalStore:
    def __init__(self, db, embedder, vec_index): ...
    
    def build_file_crystals(self, path: str, source_type: str) -> int:
        """对文件建晶体：切片→hash→查重→摘要→入库"""
    
    def _summarize_slice(self, slice: Slice) -> str:
        """三问压缩：调 LLM 产出"结论/为什么/下一步"格式晶体"""
    
    def detect_hash_changes(self, path: str) -> List[change]:
        """检测文件 hash 变化，返回变化的切片列表"""
    
    def mark_stale(self, crystal_id: str): ...
    
    def recall(self, query: str, top_k=5) -> List[FileCrystal]: ...
    
    def get_by_path(self, path: str) -> List[FileCrystal]: ...
    def get_by_hash(self, hash: str) -> FileCrystal: ...
```

**关键逻辑**：
- `build_file_crystals`：切片→算 hash→查 file_crystals 表→hash 已存在则跳过→不存在则调 LLM 三问压缩→入库
- `detect_hash_changes`：重新切片算 hash，和库里比对，变化的标 `status='stale'`
- 召回走 vec_index KNN 预筛 + 精排

#### 4.2.3 物理文件夹初始化

在项目根目录新建：
- `knowledge_base/`（权威库，AI 只读）+ `.gitkeep`
- `ai_downloads/`（下载库，AI 读写）+ `.gitkeep`

在 `FileCrystalStore` 初始化时检测并创建。

### 4.3 工具与 API

#### 4.3.1 新增工具（[tools/file_crystal_tools.py](tools/file_crystal_tools.py)）

- `build_file_crystals(path)` — 对指定文件建晶体
- `recall_file_crystal(query, top_k=5)` — 召回文件晶体
- `check_file_updates(path)` — 检查文件 hash 变化
- `read_file_slice(path, slice_index)` — 读取某切片原文（下钻用）

#### 4.3.2 HTTP 接口（[server.py](server.py)）

- `POST /api/file-crystals/build` — 触发建库
- `GET /api/file-crystals?path=xxx` — 查某文件的晶体
- `POST /api/file-crystals/recall` — 召回
- `GET /api/file-crystals/changes?path=xxx` — 查变化

### 4.4 三问压缩提示词

改造 `prompts/memory_prompts.py`，新增 `FILE_CRYSTAL_PROMPT`：

```
你正在为 AI 建立文件晶体记忆。对以下文件切片做三问压缩：

【切片内容】
{slice_content}

【文件来源】{source_path}

请严格按格式输出，答不出就继续提炼直到答出：
<<<结论>>>
这个切片的核心结论是什么？（一句话，不超过50字）

<<<为什么>>>
为什么是这个结论？关键因果/证据/逻辑链（不超过150字）

<<<下一步>>>
基于这个结论，下一步该做什么？（不超过80字）

<<<关键实体>>>
逗号分隔的实体列表
```

### 4.5 验收标准

- [ ] 把一个 >10000 字的 .md 文档放进 `knowledge_base/`，调 `build_file_crystals` 能切片+建晶体
- [ ] 同文件第二次建，hash 不变 → 跳过，不重复调 LLM
- [ ] 修改文件某一段，`detect_hash_changes` 能定位变化的切片
- [ ] 变化切片标 `status='stale'`，新切片建新晶体
- [ ] `recall_file_crystal("某关键词")` 能召回相关晶体
- [ ] 代码文件（.py）能按函数边界切分，不拦腰切断

### 4.6 风险与回退

- **风险1：摘要质量差** → 三问压缩提示词必须严格，答不出不收。P1 就要重视
- **风险2：切片粒度不合适** → 先做代码+文档两种，其他兜底固定字符数。P2 优化
- **风险3：LLM 调用成本** → 大文件建库耗 token 多。加断点续建（记录已处理到哪个切片）
- **回退**：FileCrystalStore 独立模块，失败不影响现有记忆系统

---

## 第五章：P2 — 金字塔多层 + 双链路

### 5.1 目标

让大文件（超上下文）能递归压缩到可塞入上下文，且支持从顶层下钻到任意层。

### 5.2 改动清单

#### 5.2.1 FileCrystalStore 加金字塔逻辑

参考 `summary_tree.SummaryTree._recompute_upward` + `_maybe_promote_to_parent_layer` 的模式：

```python
def _build_pyramid(self, path: str):
    """对某文件的所有 layer=0 切片建金字塔"""
    # 1. 取该文件所有 layer=0 晶体
    # 2. 每 N 个（建议 3~5）打包，调 LLM 摘要成 layer=1 晶体
    # 3. layer=1 晶体的 crystal_parent 指向其 layer=0 来源
    # 4. layer=1 晶体的 raw_source_id 指向其 layer=0 来源
    # 5. 若 layer=1 仍超阈值，继续建 layer=2
    # 6. 直到顶层晶体总长度可塞入上下文
```

**关键约束**：
- 每层晶体必须同时填 `crystal_parent_id`（指向上层）和 `raw_source_id`（指向下层）
- 顶层晶体的 `crystal_parent_id` 为 NULL
- 底层切片的 `raw_source_id` 为 NULL

#### 5.2.2 下钻查询工具

`tools/file_crystal_tools.py` 新增：

- `drill_down(crystal_id)` — 沿 `raw_source_id` 下钻到下层晶体
- `drill_up(crystal_id)` — 沿 `crystal_parent_id` 上钻到上层晶体
- `get_pyramid(path)` — 返回某文件的完整金字塔结构（树形）
- `read_top_layer(path)` — 读取顶层晶体塞入上下文

#### 5.2.3 复用 SummaryTree 模式但不混表

**重要**：文件晶体的金字塔存在 `file_crystals` 表（按 `source_path + layer` 组织），**不写入 `memory_summary_nodes` 表**。后者只服务于自传晶体的月/季/年摘要。

但 `_recompute_upward` 的 Merkle 增量逻辑和 `_maybe_promote_to_parent_layer` 的提升规则，可作为参考实现。

### 5.3 金字塔摘要提示词

新增 `FILE_PYRAMID_PROMPT`（区别于 P1 的单层摘要）：

```
你正在为 AI 建立文件晶体的上层摘要。以下是 N 个下层晶体的浓缩：

【下层晶体们】
{crystals_json}

请输出一个更浓缩的上层晶体，保留主干因果，允许丢细节：
<<<结论>>>
这群晶体的共同结论是什么？

<<<为什么>>>
关键因果链（不超过200字）

<<<下一步>>>
若想了解细节，应下钻到哪一层？（指明下层晶体 id）

<<<覆盖范围>>>
这个摘要覆盖了哪些下层晶体（id 列表）
```

### 5.4 验收标准

- [ ] 一个 50000 字的大文件，建金字塔后顶层晶体总长 < 5000 字
- [ ] `get_pyramid` 返回完整树结构，层级清晰
- [ ] `drill_down` 从顶层能逐层下钻到 layer=0 原始切片
- [ ] `read_top_layer` 返回的内容可塞入标准上下文
- [ ] 修改文件中间一段，只有对应切片及其上层链路标 stale，其他复用

### 5.5 风险与回退

- **风险1：金字塔层数过多导致压缩损耗大** → 限制最多 5 层，超出则强制扩大每层打包数
- **风险2：上层摘要脱离下层语义** → 提示词强制要求"覆盖范围"字段，便于追溯
- **回退**：金字塔逻辑独立于 P1 单层晶体，失败可降级为只用 layer=0

---

## 第六章：P3 — 自传晶体对齐 CMN

### 6.1 目标

让现有的 `remember` 工具和反思机制产出符合 CMN 规范的晶体，和文件晶体共享 hash 寻址。

### 6.2 改动清单

#### 6.2.1 FragmentStore 支持 CMN 字段

[fragment_store.py](memory/fragment_store.py) 的 `add()` 方法扩展：

- `node_type` 默认 'self'
- `authority_level` 默认 0
- `crystal_parent_id` / `raw_source_id` 可选传入
- `confidence_decay` 新建时 = 1.0
- `last_hash_verified_at` 新建时 = now

#### 6.2.2 remember 工具改造

[tools/memory_tools.py](tools/memory_tools.py) 的 `remember` 工具支持新参数：

- `epistemic`（必填，experience/opinion/world）
- `authority_level`（AI 不能自己设 1，只能 0；1 由反思回路提拔）
- `raw_source`（可选，指向触发这次记忆的任务上下文片段）

#### 6.2.3 三问反思接入

改造 [task/reflection.py](task/reflection.py) 的 `reflect_and_sediment`：

- 现有的"叙事碎片"产出改为三问格式
- 复用 `memory_tools._generate_compress_summary` 的三段式骨架，改提示词为"结论/为什么/下一步"
- 产出的晶体填 `crystal_parent_id`（指向触发它的任务节点）和 `raw_source_id`（指向任务上下文片段）

#### 6.2.4 hash 寻址统一

自传晶体和文件晶体共享同一套 hash 寻址空间：
- 自传晶体 hash = SHA256(text + epistemic + ts)
- 文件晶体 hash = SHA256(slice_content)
- 查询接口 `get_by_hash(hash)` 统一从两张表查

### 6.3 验收标准

- [ ] `remember(text="...", epistemic="experience")` 能存入带 CMN 字段的晶体
- [ ] AI 不能自己存 `authority_level=1` 的晶体
- [ ] 反思回路产出的晶体是三问格式，不是流水账
- [ ] 自传晶体和文件晶体都能通过 hash 查到
- [ ] 现有 `recall_memory` 工具仍能召回新老数据

### 6.4 风险与回退

- **风险1：老数据没有 CMN 字段** → 所有新字段 NULL 兼容，老数据按普通晶体处理
- **风险2：反思提示词改造影响现有沉淀质量** → 保留老提示词作 fallback，A/B 对比
- **回退**：FragmentStore 新字段独立，不影响老 add/recall 逻辑

---

## 第七章：P4 — 反思回路四职 + 提拔权威

### 7.1 目标

把现有"任务末反思"升级为设计书 §5.3 的四项职责 + 提拔权威。这是网络不熵增成屎山的保障。

### 7.2 改动清单

#### 7.2.1 改造 [memory/reflection.py](memory/reflection.py)

新增 `ReflectionLoop` 类，包含五项职责：

```python
class ReflectionLoop:
    def run_full_cycle(self):
        """完整反思周期，依次执行五项职责"""
        self.build_weak_associations()   # 建弱关联
        self.emerge_meta_crystals()      # 涌现元晶体
        self.anti_entropy_prune()        # 反熵修剪
        self.self_check_gaps()           # 自检缺口
        self.promote_authority()         # 提拔权威
    
    def build_weak_associations(self):
        """扫描孤立/新晶体，批量判定要不要建 weak_assoc 边"""
        # 取最近 N 天无边的晶体
        # 对每对计算 embedding 相似度
        # 高于阈值 → 调 LLM 判定"为什么关联"，带 confidence + reason 入库
    
    def emerge_meta_crystals(self):
        """发现晶体群共通模式，提炼元晶体"""
        # 聚类相似晶体
        # 对每个聚类调 LLM 提炼元晶体
        # 元晶体通过 derive 边指向来源
    
    def anti_entropy_prune(self):
        """反熵修剪"""
        # 删指向已失效节点的边
        # 合并语义重复的晶体（embedding 相似度 > 0.95）
        # 降权长期低召回的 weak_assoc 边
    
    def self_check_gaps(self):
        """自检盲区"""
        # 扫描最近任务的 question/uncertain
        # 看相关主题是否有对应晶体
        # 缺口标记为待补全
    
    def promote_authority(self):
        """提拔权威"""
        # 扫描 authority_level=0 的晶体
        # 判定三条信号：来源类型/多源印证/多次验证
        # 满足任一 → 提升为 authority_level=1
```

#### 7.2.2 触发机制

- **任务末触发**（现有）：`task/reflection.reflect_and_sediment` 调 `ReflectionLoop.run_full_cycle`
- **空闲触发**（新增）：`memory/reflection.trigger_deep_integration_now` 也调 `ReflectionLoop`
- **手动触发**：现有 `reflect` 工具不变

#### 7.2.3 提拔权威的判定逻辑

```python
def _check_authority_signals(self, crystal) -> bool:
    # 信号1：来源类型
    if crystal.source_path and crystal.source_path.startswith('knowledge_base/'):
        return True  # 权威库文件自动权威
    
    # 信号2：多源印证（≥3 个独立来源说同样的事）
    similar = self._find_similar_crystals(crystal, threshold=0.85)
    sources = set(c.source_path for c in similar if c.source_path)
    if len(sources) >= 3:
        return True
    
    # 信号3：多次验证（被反思回路反复确认没被推翻）
    if crystal.recall_count >= 5 and not self._has_negate_edge(crystal.id):
        return True
    
    return False
```

### 7.3 验收标准

- [ ] 跑一次反思回路后，孤立晶体数减少（建了弱关联）
- [ ] 语义重复的晶体被合并
- [ ] 权威库文件的晶体自动 `authority_level=1`
- [ ] 被 ≥3 次召回且无否定边的经验晶体被提拔为权威
- [ ] 普通晶体不能否定权威晶体（API 拒绝）
- [ ] 反思回路不破坏现有任务执行

### 7.4 风险与回退

- **风险1：LLM 调用成本爆炸** → 限制每次反思扫描的晶体数（如最近 100 条），批量处理
- **风险2：弱关联建得过猛** → 设 confidence 阈值，低于 0.6 不建
- **风险3：提拔权威误判** → 提拔是单向的（只升不降），但可手动降级
- **回退**：ReflectionLoop 独立类，可单独禁用某项职责

---

## 第八章：P5 — 检索统一 + 按需验证

### 8.1 目标

实现设计书 §6 的双路检索 + 按需验证信号。让 AI 用晶体时知道"这可能过期了"。

### 8.2 改动清单

#### 8.2.1 统一检索接口

新建 `memory/cmn_retriever.py`：

```python
class CMNRetriever:
    def __init__(self, fragment_store, file_crystal_store, relation_store): ...
    
    def retrieve(self, query: str, top_k=5) -> List[Crystal]:
        """双路检索：hash 寻址 + 走边"""
        # 1. hash 寻址（精确）
        # 2. 向量召回（粗筛）
        # 3. 沿 causal/derive/support 边扩展（走边）
        # 4. 合并去重，按 confidence + relevance 排序
        # 5. 每个晶体附 confidence_decay 信号
    
    def verify_crystal(self, crystal_id) -> Crystal:
        """按需验证：检查 hash 是否变化"""
        # 文件晶体：重算切片 hash，比对
        # 自传晶体：检查是否有新否定边
        # 更新 last_hash_verified_at + confidence_decay
```

#### 8.2.2 confidence_decay 计算

```python
def _compute_decay(self, crystal) -> float:
    if crystal.node_type == 'file':
        # 文件晶体：按时间衰减
        days = (now - crystal.last_hash_verified_at).days
        return max(0.1, 1.0 - days * 0.05)  # 每天衰减 5%，最低 0.1
    else:
        # 自传晶体：按被引用/否定情况
        negations = count_negate_edges(crystal.id)
        references = count_references(crystal.id)
        if negations > 0:
            return 0.2  # 被否定过，低置信
        return max(0.3, 1.0 - 0.1 * (1 - min(references/5, 1.0)))
```

#### 8.2.3 召回结果附带信号

召回的每个晶体都带：
- `confidence_decay`：0.0~1.0
- `last_verified`：时间戳
- `is_authority`：bool
- `epistemic`：world/experience/opinion

AI 召回时看这些信号，自己决定是否调 `verify_crystal` 下钻验证。

### 8.3 验收标准

- [ ] `retrieve("某查询")` 同时返回自传晶体和文件晶体
- [ ] 每个结果都带 `confidence_decay` 信号
- [ ] `verify_crystal` 能检测文件 hash 变化并更新信号
- [ ] 被否定过的自传晶体 `confidence_decay` 低
- [ ] 权威晶体 `confidence_decay` 衰减慢
- [ ] AI 召回结果按 authority + confidence + relevance 综合排序

### 8.4 风险与回退

- **风险1：走边扩展导致召回爆炸** → 限制扩展深度（如 2 跳）和总数量（如 top_k×3）
- **风险2：衰减参数不合理** → 参数可配置，先跑观察再调
- **回退**：CMNRetriever 独立模块，失败可降级为现有 FragmentStore.recall

---

## 第九章：跨阶段约束

### 9.1 不破坏现有功能的红线

每阶段结束必须保证：
- `remember` / `recall_memory` / `trace_memory` 正常工作
- 任务执行（DAG + executor）正常工作
- 现有 HTTP 接口正常响应
- 现有数据库表数据不丢失

### 9.2 测试要求

每个阶段新增：
- 单元测试：核心类的方法测试
- 集成测试：端到端流程测试
- 回归测试：现有功能不破坏

### 9.3 文档同步

每阶段完成后更新：
- [晶体记忆网络设计书.md](晶体记忆网络设计书.md) 的术语表（如有新概念）
- 本实施方案的进度标记
- 新增模块的 docstring

---

## 第十章：优先级建议

如果资源有限，建议优先级：

1. **P0 + P1（必做）**：数据模型 + 文件晶体 MVP。这是 CMN 最核心的价值验证，跑通后才知道设计是否成立
2. **P3（重要）**：自传晶体对齐。让现有记忆系统符合 CMN 规范，投入小收益大
3. **P2（按需）**：金字塔多层。只有当确实要处理超上下文大文件时才做
4. **P4 + P5（长期）**：反思回路 + 检索统一。网络质量保障，但可延后

**不建议跳过 P0 直接做 P1**——字段不加好，P1 的 FileCrystalStore 无处存数据。

---

## 附录：文件清单

### 新建文件

| 文件 | 阶段 | 用途 |
|------|------|------|
| `memory/migrations/p0_cmn_fields.sql` | P0 | 数据库迁移 |
| `memory/file_slicer.py` | P1 | 文件切片器 |
| `memory/file_crystal_store.py` | P1 | 文件晶体存储 |
| `tools/file_crystal_tools.py` | P1 | 文件晶体工具 |
| `prompts/file_crystal_prompts.py` | P1 | 三问压缩提示词 |
| `memory/cmn_retriever.py` | P5 | 统一检索 |
| `knowledge_base/` | P1 | 权威库物理文件夹 |
| `ai_downloads/` | P1 | 下载库物理文件夹 |

### 改动文件

| 文件 | 阶段 | 改动 |
|------|------|------|
| [memory/fragment_store.py](memory/fragment_store.py) | P0/P3 | 加字段+迁移+CMN 支持 |
| [memory/relation_store.py](memory/relation_store.py) | P0 | 加四种边类型 |
| [memory/reflection.py](memory/reflection.py) | P4 | 改造为 ReflectionLoop |
| [tools/memory_tools.py](tools/memory_tools.py) | P3 | remember 支持 CMN 参数 |
| [task/reflection.py](task/reflection.py) | P3/P4 | 三问反思+触发 ReflectionLoop |
| [prompts/memory_prompts.py](prompts/memory_prompts.py) | P1/P2 | 加文件晶体提示词 |
| [server.py](server.py) | P1 | 加文件晶体 HTTP 接口 |

---

> 本方案基于设计书 v1.0 和现有系统摸底编写。
> 每阶段完成后根据实际效果调整后续阶段。
> 版本：v1.0

---

## 第十一章：实施进度（截至 2026-08-01）

> 本章记录实际落地的内容，与原规划章节对照。原章节保留作为设计参考，本章反映真实状态。

### 11.1 阶段完成情况

| 阶段 | 状态 | 说明 |
|------|------|------|
| **P0** 数据模型扩展 | ✅ 完成 | 字段+迁移+file_crystals 表全就位 |
| **P1** 文件晶体 MVP | ✅ 完成（哲学修订） | 切片+hash 检测+召回全链路。**废弃三问压缩**，改为透明切片基础设施 |
| **P2** 金字塔多层 + 双链路 | ✅ 完成 | drill_down/up + get_pyramid + read_top_layer |
| **P3** 自传晶体对齐 CMN | ✅ 完成 | remember 支持 CMN 字段，hash 寻址统一 |
| **P4** 反思回路 + 提拔权威 | ✅ 完成 | 五项职责全实现 + 空闲触发 + AI 工具 |
| **P5** 检索统一 + 按需验证 | ✅ 完成 | CMNRetriever 双路检索 + 衰减信号 + crystal_recall 工具 |

### 11.2 超出原规划的实现

原方案聚焦"存储+检索"基础设施，实际落地多做了三块"AI 可用性"工作：

#### 11.2.1 AI 工具化（原方案未规划）

反思回路和检索能力暴露为 AI 可调用工具：

| 工具 | 文件 | 用途 |
|------|------|------|
| `reflection_loop` | [tools/memory_tools.py](tools/memory_tools.py) | 跑一次反思回路（五项职责），支持 `only` 参数单独跑某项 |
| `check_memory_gaps` | [tools/memory_tools.py](tools/memory_tools.py) | 查记忆盲区（高频实体但无深度晶体） |
| `crystal_recall` | [tools/memory_tools.py](tools/memory_tools.py) | CMN 双路检索（hash + 关系网扩展），替代 recall_memory 的复杂场景 |
| `trace_memory`（升级） | [tools/memory_tools.py](tools/memory_tools.py) | 双入参：`fragment_id`（精准追）或 `keyword`（模糊追） |

#### 11.2.2 空闲触发机制（原方案 §7.2.2 提到但未细化）

**触发链**（[server.py](server.py) chat 完成后）：
```
chat 完成
  → 启动 daemon 线程 _idle_reflect
    → trigger_deep_integration_now()
      → MemoryAI._run_deep_integration()
        → ReflectionLoop.run()  ← 只跑 1 次
```

**关键特性**：
- 每次对话结束后**只跑 1 次**反思回路（五项职责串行一遍）
- 有 `_busy_lock` 防重入：上一次没跑完，新的触发直接跳过
- 异步执行，不阻塞用户响应
- AI 也可通过 `reflection_loop` 工具主动调用

#### 11.2.3 提示词结构重构（原方案未规划）

按"入口透明、AI 自然选择"理念重构系统提示词，让能力作为可见入口暴露在提示词里，而非藏在工具列表等 AI 去找。

**实际提示词结构**（[server.py](server.py) `_build_system_message`）：

```
《系统提示词》
├─ Block 1   【规则】         — 重要信息，修改入口（important_matters_add/update/remove）
├─ Block 1.5 【领域书】       — 开关控制入口（14页，✅激活/⏹关闭，book_turn_to 开关）
├─ Block 2   【任务】         — 当前任务上下文
├─ Block 2.1 【任务DAG】      — 条件触发：有 DAG 时显示
├─ Block 2.5 【心智锚点】     — 任务自带挂件（update_task_brief 写入，AI 无感文件）
├─ Block 2.6 【思维链】       — 操作入口，条件触发：有思维链时显示
├─ Block 3   【现在时间】     — 每次刷新
├─ Block 4   【技能】         — 操作入口（list_skill_templates / create_skill）
├─ Block 5   【记忆】         — 5条+回想入口（recall_memory / crystal_recall / trace_memory）
├─ Block 6   【网络】         — 端口信息
├─ Block 7   【草稿纸】       — 任务自带便签（write_draft，AI 无感文件）
《聊天历史》
《上下文墙》                  — 条件触发：超预算时显示翻阅入口
《当前用户消息》
```

**关键设计决策**：

1. **记忆 Block 升级**（3条→5条）：
   - 筛选规则：`weight × recency × importance × authority` 综合排序
   - 同 topic 去重（保证多样性）
   - 标记：★权威晶体 / ⚠️衰减晶体
   - 入口文案："💭 回想更多: recall_memory(关键词) / crystal_recall(跨文件全景) / trace_memory(追因果)"

2. **领域书 Block 注入**（修复主代码丢失的 bug）：
   - 列出全部 14 页（✅激活/⏹关闭）
   - 已激活页内容前 400 字摘要
   - 入口：`book_list_pages` / `book_turn_to` / `book_read_page`

3. **心智锚点 + 草稿纸任务挂件化**：
   - 心智锚点从读 `README.md` 改为读 `data/anchors/{tid}.md`
   - 新增 `update_task_brief` 工具：AI 写任务概览便签，系统底层存文件但 AI 无感
   - `write_draft` 改追加模式（默认 append），描述去掉文件路径概念
   - 空状态时提示入口（`📌 update_task_brief` / `📝 write_draft`），非常驻硬塞
   - AI 视角：这是"任务自带挂件"，不是"文件"

#### 11.2.4 透明文件晶体集成（哲学修订：废弃三问压缩）

**核心哲学转变**：所有晶体必须经过 AI 脑子。

原方案用 LLM 三问压缩机械生成文件晶体 summary，违反"晶体是 AI 思考产物"的哲学。修订后：
- 文件切片+hash 降级为**纯基础设施**（定位+变化检测），不调 LLM，不生成 summary
- 真正的晶体（理解/结论）由 AI 思考产生，存在 `memory_fragments` 表（自传晶体）
- `raw_source_id` 桥将自传晶体指向文件切片

**改造清单**：

| 文件 | 改动 | 说明 |
|------|------|------|
| [memory/file_crystal_store.py](memory/file_crystal_store.py) | 新增 `build_slices_only` | 纯切片建库（无 LLM），embedding 基于原文前 500 字 |
| | 新增 `get_file_status` | 已阅状态+hash 变化+关联思考（read_file 透明接入用） |
| | 新增 `get_thoughts_for_path` | 反查：文件切片 → 自传晶体（`WHERE raw_source_id IN (...)`） |
| | 新增 `get_thoughts_for_slice` | 反查特定切片的思考 |
| [tools/crystal_session.py](tools/crystal_session.py) | **新建** | 线程级会话上下文（path/slice_ids），桥接 read_file → remember |
| [tools/file_tools.py](tools/file_tools.py) | 改造 `read_file` | 透明接入 FileCrystalStore，已阅返回思考+hash 状态+session tag |
| | 新增 `read_full`/`slice` 参数 | 已阅时强制读原文 / 读特定切片 |
| [tools/memory_tools.py](tools/memory_tools.py) | 改造 `remember` | 自动检测 session tag，建 `raw_source_id` 桥（AI 无感） |
| [memory/reflection_loop.py](memory/reflection_loop.py) | 新增 `extract_file_thoughts` | 从对话历史提取 AI 对文件的真实思考（替代三问压缩） |

**read_file 三种返回形态**：

```
未阅 → 返回原文分页 + 后台异步建切片（AI 无感）
已阅未变 → 返回思考+hash 状态（不返回原文）
  thoughts: [切片0的思考, 切片1的思考, ...]
  hint: "文件未变。要看原文 read_file(path, read_full=true)"
已阅有变 → 返回思考+变化切片列表
  changed_slices: [切片2 hash 变了]
  stale thoughts 标记
  hint: "切片2变了。要看变化 read_file(path, slice=2)"
```

**透明桥接闭环**：
```
AI 调 read_file("auth.py")
  → 系统查 file_crystals 表，设 crystal_session（线程上下文）
  → 返回思考+状态（AI 拿到旧结论）

AI 干活，调 remember("JWT 验证有 race condition")
  → remember 自动检测 crystal_session
  → raw_source_id = session.current_slice_id
  → 桥建好（AI 完全无感）

下次 AI 再读 auth.py
  → get_file_status 反查 memory_fragments WHERE raw_source_id = 切片id
  → 返回之前的思考："JWT 验证有 race condition"
  → AI 拿到旧结论，不用重读
```

**反思回路新职责**：`extract_file_thoughts`
- 扫描最近 24 小时对话，找 `read_file` 工具调用
- 取 AI 调用后的文本回复（真实思考/结论）
- 用 LLM 提取核心结论（不超过 100 字）
- 存为自传晶体，`raw_source_id` 指向文件第一个切片
- 替代废弃的三问压缩，符合"晶体必须经过 AI 脑子"哲学

**验证结果**（2026-08-01）：
- server.py 92 个切片正确建立（无 LLM 调用）
- session tag 机制：set → get → clear 全对
- 桥接闭环：read_file 设 session → remember 自动检测 → 写入记忆 → 反查 thoughts 从 0 变 1
- hash 变化检测：未变时 hash_changed=False

### 11.3 反思回路实际表现

**五项职责 + 1 项新增**（[memory/reflection_loop.py](memory/reflection_loop.py)）：

| 职责 | 阈值 | 实际效果 |
|------|------|---------|
| ① 建弱关联 | embedding 相似度 ≥ 0.65 | 每次最多建 20 条 weak_assoc 边 |
| ② 涌现元晶 | 群内平均相似度 ≥ 0.65，≥3 个晶体成群 | 每次最多涌现 5 个元晶（LLM 三问压缩生成） |
| ③ 反熵修剪 | confidence < 0.2 的弱关联边删除 | 合并重复边（同 subject+object+edge_type 只保留最新） |
| ④ 自检缺口 | 实体 mention_count > 3 但无深度晶体 | 发现盲区返回建议 |
| ⑤ 提拔权威 | raw_count ≥ 3 且 authority=0 的自传晶体 | 提升为 authority_level=1 |
| **⑥ 提取文件思考**（新增） | 扫描最近 24h 对话找 read_file 调用 | LLM 提取 AI 真实结论，存为自传晶体，raw_source 指向切片 |
| **⑦ 知识晶体关联**（P5.5 新增） | knowledge 层晶体存入后，按 embedding 相似度 ≥ 0.72 关联同主题 core 碎片 | 建立血统（crystal_parent_id），反思回路定期补漏 |

**额外职责**：
- `backfill_entities`：从记忆碎片文本提取技术词回填到 entities 表（反思回路前置步骤）

**P5.5 knowledge 层（已实现，2026-08-02）**：

`memory_fragments.layer` 新增 `"knowledge"` 值，AI 可通过 `remember_knowledge` 工具存成体系理解（如"蓝牙HID开发的整套要点"）。区别于 `remember`（记一句话碎片），knowledge 层默认 importance=7.0，算 summary_hash 供查重，存入时即时关联同主题 core 碎片，召回时自动带 📚 标记。AI 视角无文件/路径/hash 概念，只有"我记得 / 我想起来"。详见第十二章 P6 设计。

**已知阈值调整**（原方案未指定具体数值）：
- WEAK_ASSOC_THRESHOLD：从初版 0.75 降到 0.65（256维 embedding 最高相似度约 0.74）
- META_CRYSTAL_THRESHOLD：从初版 0.80 降到 0.65（群内平均相似度实测约 0.72）

### 11.4 待优化项

| 项 | 现状 | 优化方向 |
|----|------|---------|
| ~~recall_memory vs crystal_recall 共存~~ | ✅ 已合并：recall_memory 并入 crystal_recall | — |
| ~~元晶生成质量~~ | ✅ 已接 LLM 三问压缩 | — |
| ~~三问压缩文件晶体~~ | ✅ 已废弃，改为透明切片+AI 思考提取 | — |
| 提示词 Block 顺序 | 当前：规则→领域书→任务→...→记忆→技能 | 用户设想：规则→领域书→思维链→记忆→技能（待重排） |
| 空闲触发反思深度 | 只跑 1 次，busy 时跳过 | 可考虑：空闲累积触发（连续 N 次跳过后强制跑） |
| extract_file_thoughts 精度 | 目前取 read_file 后第一条 AI 文本 | 可优化：取多条 AI 消息综合提取，或按切片粒度提取 |
| read_file 已阅返回 | 大文件可能 thoughts 很多 | 可优化：按 importance 截断，或只返回 top 5 思考 |
| crystal_recall 文件晶体召回 | 写死 `WHERE layer=0` | 可优化：召回时也包含 raw_source 关联的思考 |

### 11.5 验证记录

| 验证项 | 方法 | 结果 |
|--------|------|------|
| 空闲触发不阻塞 | 测 chat 响应时间 | ✅ 普通 chat 4.1s，反思后台异步 |
| AI 自然调用 reflection_loop | 对话让 AI 跑反思 | ✅ AI 调用工具并理解五项职责 |
| 五项职责全生效 | POST /api/reflection/run | ✅ weak_assoc/meta/prune/gaps/promoted 全有数值 |
| 领域书注入 | 问 AI "有多少领域书" | ✅ AI 回答 14 页，列出激活/关闭 |
| 记忆 5 条 + 回想入口 | 问 AI 过去的事 | ✅ AI 看到多条记忆并列举 |
| trace_memory 双入参 | keyword 追因果 | ✅ AI 用 keyword 追，发现追错主动换工具 |
| 心智锚点无感 | update_task_brief 后新对话 | ✅ AI 以为"系统展示"，不知是文件 |
| 草稿纸无感 | write_draft 后新对话 | ✅ AI 以为"任务自带便签" |
| 透明文件晶体：建切片 | read_file 未阅文件 → 后台 build_slices_only | ✅ server.py 92 切片，无 LLM 调用 |
| 透明文件晶体：已阅返回思考 | read_file 已阅文件 → 返回 thoughts + hash 状态 | ✅ seen_before=True, hash_changed=False |
| 透明桥接：read_file → remember | read_file 设 session → remember 自动检测 raw_source | ✅ 桥接成功，反查 thoughts 从 0 变 1 |
| 反思回路提取文件思考 | extract_file_thoughts 扫对话历史 | ✅ 方法就绪，待真实对话数据验证 |

---

> 实施进度章节到此结束。
> 后续更新继续在本章追加，不改动原规划章节。

---

## 第十二章：P6 — 叙事记忆层（story layer）

> 本章为 2026-08-02 新增设计，基于用户与 AI 共同讨论的"故事化记忆"构想。
> 目标：让 AI 的记忆从"碎片池"升级为"连续自传体叙事"，思维不再混乱。

### 12.1 目标

解决当前记忆系统的核心痛点：**碎片散落、缺乏连续性、AI 自我感薄弱**。

现状（P0-P5）的记忆是 1148 条零散 core 碎片 + 2 条 knowledge 晶体。AI 想起一件事时，召回的是孤立的一行字，没有情境、没有因果、没有"我经历了什么"的叙事感。这导致：
- AI 的记忆像便签纸堆，不像回忆
- 跨任务的连贯认知无法形成（每次召回都是离散点）
- 思维链封档后丢失，任务经验无法沉淀为记忆

P6 引入 `story` 层：**AI 在空闲时把相关碎片整理成连贯的叙事故事**，故事作为记忆网络的超级节点，连接原始素材、抽象结论、时间锚点、因果关系。召回一个故事就等于召回它周围的整个上下文。

### 12.2 设计哲学

#### 12.2.1 故事是网络节点，不是文件夹条目

故事**不分任务主题**，也**不是完整人生流水账**。每个故事是一个多维锚定的节点：

| 维度 | 说明 | 字段 |
|------|------|------|
| 主题 | 故事讲什么（可多个） | `tags` |
| 事件时间 | 故事里的事发生在何时 | `event_time` |
| 写作时间 | 故事何时被整理成文字 | `written_at`（系统自动，毫秒级） |
| 事件跨度 | 单次事件 or 持续几天 | `event_span` |
| 涉及实体 | 人/物/概念 | `entity_ids` |
| 素材来源 | 由哪些碎片整理而成 | `source_ids`（JSON 数组） |
| 知识沉淀 | 抽象出的结论 | `knowledge_id`（指向 knowledge 晶体） |
| 原始文档 | 引用的文件切片 | `raw_source_id` |
| 任务来源 | 来自哪个思维链 | `task_id` |

召回时可按任意维度切：问"蓝牙相关的故事"、问"上周的故事"、问"跟当前这件事相关的故事"。

#### 12.2.2 三层时间设计

人脑的情景记忆最显著特征是时间地点锚定。故事必须带三种时间：

| 时间字段 | 含义 | 谁填 |
|----------|------|------|
| `event_time` | 故事讲的事发生在什么时候 | AI 写内容时声明 |
| `written_at` | 故事何时被整理成文字 | 系统自动带（毫秒时间戳） |
| `event_span` | 故事跨越的时间范围 | AI 声明（single / hours / days / weeks） |

`written_at` 形成记忆形成时间线——AI 可回溯"我是从什么时候开始懂这个的"。

#### 12.2.3 故事是超级节点

故事通过 `memory_relations` 表的多类型边连接一切：

```
story ─素材─→ core 碎片（这个故事由哪些碎片整理而成）
story ─沉淀─→ knowledge 晶体（这个故事抽象出的结论）
story ─引用─→ file_crystals（故事提到的原始文档切片）
story ─来源─→ thought_chain（故事来自哪个任务的执行脚印）
story ─邻接─→ 另一个 story（时间相邻或主题相关）
story ─涉及─→ entity（人/物/概念）
story ─因果─→ 另一个 story（"因为上次那样，所以这次这样"）
```

召回一个故事 → 自动带出它的邻居 → AI 看到的是完整的情境。

### 12.3 数据模型

#### 12.3.1 memory_fragments 扩展 story 层

`layer` 字段新增 `"story"` 值。复用现有字段，新增 3 个可选字段：

```sql
ALTER TABLE memory_fragments ADD COLUMN event_time TEXT DEFAULT NULL;      -- 故事事件时间，如 "2026-08-02 下午"
ALTER TABLE memory_fragments ADD COLUMN event_span TEXT DEFAULT NULL;      -- single/hours/days/weeks
ALTER TABLE memory_fragments ADD COLUMN source_ids TEXT DEFAULT NULL;      -- JSON 数组，素材碎片 id 列表
```

story 层默认 `importance=6.5`（排碎片前，knowledge 后）。

#### 12.3.2 memory_relations 新增边类型

```sql
-- edge_type 扩展（现有：fact/causal/temporal/weak_assoc/negate）
ALTER TABLE memory_relations ADD COLUMN edge_type CHECK(edge_type IN (
    'fact','causal','temporal','weak_assoc','negate',
    'material',    -- story → core 碎片（素材关系）
    'distill',     -- story → knowledge（沉淀关系）
    'reference',   -- story → file_crystal（引用关系）
    'origin',      -- story → thought_chain（来源关系）
    'adjacent',    -- story ↔ story（邻接关系）
    'involve'      -- story → entity（涉及关系）
));
```

### 12.4 召回排序：相似度为主，时间倒序为辅

故事召回采用**两阶段排序**：

```
1. 相似度筛选：embedding cosine_sim(query, story) ≥ 0.65 → 候选集
2. 时间倒序：候选集内按 written_at DESC（最新靠前）
3. 同分时按 importance DESC
```

**为什么这样排**：
- 相似度保证召回的是主题相关的（不相关的不进候选）
- 时间倒序让最近的经历优先浮出（人脑也是：最近的事记得最清楚）
- 最新的靠前 = AI 优先想起最近整理的故事

召回时 story 层与 core/knowledge 一起进入候选（同 `_fetch_candidates` 逻辑），但 story 带独特的 📖 标记，文本截到 250 字。

### 12.5 反思回路新增"叙事沉淀"职责

反思回路（[memory/reflection_loop.py](memory/reflection_loop.py)）新增第⑧项职责：

#### 12.5.1 触发条件

- 空闲触发（同现有机制，5 分钟间隔）
- AI 主动调 `reflection_loop(only="narrative_consolidation")`

#### 12.5.2 流程

```
1. 找聚类：未关联 story 的 core 碎片，按 embedding 聚类（相似度 ≥ 0.72，≥3 条成组）
2. 时间排序：组内按 created_at ASC（故事按发生顺序讲）
3. LLM 叙事：把碎片组喂给 LLM，生成连贯故事
   - 提示词要求：第一人称、带时间、带因果、"我经历了什么"
   - 不是抽象结论（那是 knowledge 的活），是叙事
4. 存 story：layer="story", importance=6.5, event_time 取组内时间范围
5. 建边：
   - story → 每个素材碎片：edge_type="material"
   - story → 相关 knowledge（如有）：edge_type="distill"
   - story → 相关 file_crystal（如 raw_source 有）：edge_type="reference"
6. 标记素材：碎片的 crystal_parent_id 指向 story（已被整理过，不再重复整理）
```

#### 12.5.3 叙事提示词

```
你正在整理自己的记忆。下面是关于「{topic}」的几条碎片记忆，按时间排序：

{碎片列表，带时间戳}

把它们串成一段连贯的故事。要求：
- 第一人称（"我"）
- 带时间感（"那天"、"后来"、"直到"）
- 带因果（"因为...所以..."、"这次让我明白..."）
- 不是抽象总结，是你经历的叙事
- 200-400 字

直接输出故事，不要标题、不要解释。
```

### 12.6 思维链自动沉淀（任务封档时）

思维链（[task/thought_chain.py](task/thought_chain.py)）目前任务结束就成死档案。P6 让它自动沉淀为故事：

#### 12.6.1 触发时机

任务状态变为 `done` / `archived` 时，触发一次叙事整理。

#### 12.6.2 流程

```
1. 读取任务的 thought_chain（motivation + result 序列）
2. LLM 整理成故事："这个任务我经历了什么、为什么这么做、最后怎样"
3. 存 story：layer="story", task_id=当前任务, event_time=任务时间范围
4. 建边：story → thought_chain（edge_type="origin"）
5. 故事自动进入记忆池，下次类似任务自然召回
```

这样思维链不再是孤立的执行脚印，而是**记忆的素材源**——做完事自动沉淀成故事，下次遇到类似任务，故事自然浮出。

### 12.7 三层记忆 + 网络连接全景

```
        原始文档 (file_crystals, hash 寻址)
            ↑ reference
            │
        story (情景记忆，带时间+因果+叙事)  ←── 空闲时整理 + 思维链沉淀
            ↑ material           ↓ distill      ↑ origin
            │                    │              │
        core 碎片 (零散经验)  knowledge (抽象结论)  thought_chain (执行脚印)
            └─ weak_assoc ──→ 其他碎片
            └─ causal ─────→ 其他碎片/故事
            └─ adjacent ───→ 邻接故事
```

| 层 | role | importance | 召回标记 | 典型长度 |
|----|------|-----------|---------|---------|
| file_crystals | 原始素材 | — | [文件] | 切片原文 |
| core | 碎片经验 | 5.0 | [记忆] | 一句话 |
| story | 情景叙事 | 6.5 | 📖 | 200-400 字 |
| knowledge | 抽象结论 | 7.0 | 📚 | 成体系 |

召回时按 importance 排序：knowledge → story → core，但 story 因 similarity 高时也能超过 knowledge。AI 看到的是分层记忆：先想起成体系理解，再想起经历的故事，再想起零散细节。

### 12.8 改动清单

#### 12.8.1 数据模型（[memory/fragment_store.py](memory/fragment_store.py)）

- `memory_fragments` 表加 3 字段：`event_time`, `event_span`, `source_ids`
- `add()` 支持 `layer="story"` + 新字段
- `link_materials()` 新方法：建 story → core 的 material 边

#### 12.8.2 反思回路（[memory/reflection_loop.py](memory/reflection_loop.py)）

- 新增 `narrative_consolidation()` 职责
- `run()` report 加 `stories_consolidated` 字段
- 聚类逻辑：复用 `emerge_meta_crystals` 的聚类思路，但阈值 0.72，输出是 story 不是元晶

#### 12.8.3 思维链沉淀（[task/thought_chain.py](task/thought_chain.py) + [server.py](server.py)）

- `ThoughtChain` 加 `consolidate_to_story()` 方法
- server.py 任务封档处（task → done）触发沉淀

#### 12.8.4 召回（[memory/fragment_store.py](memory/fragment_store.py)）

- `_fetch_candidates`：core 层查询扩展为 `core + knowledge + story`
- 召回结果 story 带 📖 标记，文本截到 250 字
- 排序：相似度筛选 → written_at DESC → importance DESC

#### 12.8.5 AI 工具（[tools/memory_tools.py](tools/memory_tools.py)）

- `remember_story` 工具：AI 主动整理故事（可选，主要靠反思回路自动跑）
- `crystal_recall` 升级：召回结果包含 story 层

#### 12.8.6 叙事提示词（[prompts/](prompts/) 目录）

- 新建 `narrative_prompts.py`：故事整理提示词
- 思维链沉淀提示词

### 12.9 验收标准

| 验证项 | 方法 | 预期 |
|--------|------|------|
| story 层存储 | 存一条 story，查 DB | layer="story", importance=6.5, 三时间字段齐全 |
| 反思回路叙事沉淀 | POST /api/reflection/run | stories_consolidated > 0，story 有 material 边指向碎片 |
| 召回命中 story | 对话问相关主题 | AI 看到 📖 标记的故事，排碎片前 |
| 时间倒序 | 存多条同主题 story | 最新 written_at 的排最前 |
| 思维链沉淀 | 任务标记 done | 自动生成 story，task_id 关联 |
| 故事网络连接 | 查 memory_relations | story 有 material/distill/reference/origin 边 |
| AI 自然体验 | 问 AI "你经历了什么" | AI 讲出连贯故事，不是碎片罗列 |

### 12.10 风险与回退

| 风险 | 缓解 |
|------|------|
| LLM 叙事质量差 | 提示词约束 200-400 字 + 第一人称；质量差的 story 不提拔 authority |
| 聚类误判 | 阈值 0.72（高于 weak_assoc 的 0.65），≥3 条成组才整理 |
| story 数量爆炸 | 每次反思最多整理 3 个故事（`STORY_MAX_PER_RUN=3`） |
| 召回 token 膨胀 | story 截到 250 字，单次召回最多 2 条 story |
| 思维链沉淀干扰 | 任务封档异步触发，不阻塞用户；失败不报错只 log |

回退方案：`layer="story"` 是新值，不影响现有 core/knowledge 逻辑。出问题只需在 `_fetch_candidates` 里移除 story 层即可降级回 P5.5。

### 12.11 实施顺序

1. **数据模型**（12.3）：加字段 + story 层支持 — 0.5 天
2. **叙事沉淀职责**（12.5）：反思回路加 narrative_consolidation — 1 天
3. **召回集成**（12.4 + 12.8.4）：story 进入召回 — 0.5 天
4. **思维链沉淀**（12.6）：任务封档触发 — 0.5 天
5. **AI 工具 + 提示词**（12.8.5 + 12.8.6）：remember_story 工具 + 叙事提示词 — 0.5 天
6. **验证**（12.9）：全链路测试 — 0.5 天

总计约 3.5 天，可分两阶段：先做 1-3（story 层 + 反思沉淀 + 召回），验证可用后再做 4-6（思维链 + 工具）。

---

> 第十二章 P6 设计到此结束。
> 下一步：按 12.11 实施顺序开干，从数据模型开始。
