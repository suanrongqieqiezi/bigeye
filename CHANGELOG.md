# 更新日志（CHANGELOG）

大眼（bigeye）的项目版本与主要更新记录。当前为开发交流版，主要面向源码使用者。

---

## 2026-08-31（当前公开版本）

### 新增

- 任务执行层重构：新增 task/ 模块（planner、executor、thought_chain、work_memory、attention_focus、reflection、dag），加强长航时任务的拆解、执行与连续思维链性能。
- 工具路由层：新增 tools/tool_router.py 统一接入并调度各类 Agent 工具，配套 tools/registry.py 管理工具集。
- 循环检测（loop_detector）：新增避免无限循环的能力。
- 网络检索（web_search）：加强 web 检索工具与检索式工作流。
- 技能扫描（skills_scanner）：新增技能/文档扫描，便于按需加载。
- LLM 本地缓存-测试（llm_cache）：新增本地 LLM 结果缓存，即便api厂家已有一层缓存命中但依然需要收费，此功能可再次降低重复调用开销、提升响应速度，功能仍在验证。
- 文件检索（file_search/）：新增文件检索模块（含 adapters/native、adapters/everything）。
- 记忆体系扩展：新增 message_vectors、knowledge_pages、file_slicer、vec_index 等记忆检索组件，以及"上下文与性能"说明文档。
- 规则引擎文档：新增 rules_engine/README.md 与状态探针（state_probe.py）。
- 示例配置：新增 model_config.example.json（脱敏模板），便于新使用者按模板填入自己的 API Key。
- 技能合集（skills/）：随源码同步一批技能文档（嘉立创EDA、多模态接入、记忆加载链路诊断、策略清单整理、OTA 等）。
- 对话窗口支持图片显示
- 支持多模态模型

### 修复

- 修复克隆仓库后 python server.py 启动即崩溃的问题（补齐 loop_detector、tools.web_search、skills_scanner 缺失模块）。

### 使用提示

- 源码持续更新，适合开发交流，不适合直接用于生产环境；如需稳定版本请到 Release 下载已打包的可运行版本。
- 首次运行请启动后在前端设置里的模型配置填入自己的模型 API 配置后再启动。
- 运行依赖请以 requirements.txt 为准，并确保已安装 cryptography、Pillow、scrapling 等相关依赖。

---

## 更早版本

历史更新记录见 GitHub 提交历史；此前的稳定发布包位于 Releases。
