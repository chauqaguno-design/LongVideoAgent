# VideoMind 规范驱动开发文档包

版本 1.0  日期 2026-11-23

这套文档供Codex在现有VideoMind基础上实施完整的视频管理、长视频理解、检索问答和按需剪辑产品。包含PRD、技术设计、接口与数据合同、6个业务功能规格包、基础与界面规格、发布规格、110项开发任务、56项验收及执行提示词。

本地工作台已接入媒体处理、观影笔记、文字与截图检索、带证据的问答、版本化剪辑和导出。各项任务、真实服务验收和待配置的模型能力见[进度记录](status/PROGRESS.md)。

## 直接使用

1. 把本目录整体放到你的实际VideoMind仓库的docs/videomind-spec目录。
2. 保留现有AGENTS、.specify、配置和用户代码；本包中的同名文件用于阅读与增量合并，不能直接覆盖。
3. 在仓库根目录启动Codex，将[prompts/START_HERE.txt](prompts/START_HERE.txt)内容发给它。若放在别处，替换提示词中的规范目录。
4. Codex先核查实际checkout，再按M0至M6完成开发、真实验证和发布证据。
5. 中断后使用[prompts/CONTINUE.txt](prompts/CONTINUE.txt)，独立审查使用[prompts/REVIEW.txt](prompts/REVIEW.txt)。

## 已确定的方案

- 通用Agent、视觉理解、问答和编辑决策全部使用DeepSeek Flash；ASR、OCR、embedding和FFmpeg独立。
- 保留现有VideoMind分层、数据库、队列、对象存储、向量检索和前端，按实际源码增量改造。
- 上传后建立分块记忆和索引；问题到来后检索候选并局部复看；剪辑使用可追溯证据与版本化时间线。
- 采样覆盖不等于语义零遗漏；首个、全部和计数回答必须携带核查范围和限定。
- 任务可恢复，副作用幂等，渲染真实产物；Agent Team在基本闭环稳定后引入。

## 阅读导航

| 文件 | 用途 |
|---|---|
| [开发流程](docs/00_SPEC_WORKFLOW.md) | 从规范、计划、任务到实现和收敛的工作方法 |
| [PRD](docs/01_PRD.md) | 产品目标、42项需求、用户故事和范围 |
| [交互流程](docs/02_UX_FLOWS.md) | 页面入口、状态、错误、修改和恢复 |
| [架构与迁移](docs/03_ARCHITECTURE.md) | 已有源码边界与目标架构 |
| [媒体和检索](docs/04_MEDIA_RETRIEVAL.md) | 时间、采样、ASR/OCR、向量、记忆、检索和覆盖 |
| [Harness与编辑](docs/05_HARNESS_EDITING.md) | 模型调用、持久工作流、编辑、渲染、Team |
| [数据与API](docs/06_DATA_API.md) | 统一实体、事务、版本、读路径和契约语义 |
| [Codex执行手册](docs/07_CODEX_EXECUTION.md) | 启动、续做、任务选择、命令和交付证据 |
| [验收与发布](docs/08_VERIFICATION_RELEASE.md) | 真实测试、拟定阈值、故障、部署和恢复 |
| [决策与来源](docs/09_DECISIONS_AND_SOURCES.md) | 已确定选择、源码基线、模型依据和待冻结项 |

## 功能规格

| 目录 | 内容 | 主要阶段 |
|---|---|---|
| [000 基础与界面](specs/000-foundation-and-ui/spec.md) | 仓库环境、权限、合同与跨阶段页面 | M0以及M1至M5 |
| [001 媒体处理](specs/001-media-ingestion/spec.md) | 上传、源版本、PTS、采样、覆盖、索引发布 | M2 |
| [002 多模态记忆](specs/002-multimodal-memory/spec.md) | ASR/OCR/视觉、语义事件与向量空间 | M2 |
| [003 检索定位](specs/003-retrieval-grounding/spec.md) | 文字截图召回、原片复看和全局查询 | M3 |
| [004 Agent运行层](specs/004-agent-harness/spec.md) | Flash adapter、工具、预算、持久恢复 | M0预检和M3 |
| [005 剪辑渲染](specs/005-editing-render/spec.md) | 显式编辑、版本、真实预览和语义剪辑 | M1基础和M4智能 |
| [006 Agent Team](specs/006-agent-team/spec.md) | 同模型角色、任务板、消息与单写提交 | M5 |
| [007 发布](specs/007-release/spec.md) | 质量、隔离、故障、部署与最终交付 | M6，素材准备提前 |

每个目录均有spec.md、plan.md、tasks.md。**执行顺序以里程碑和任务依赖为准，005的M1基础任务优先于完整智能处理。**

## 结构化合同与追踪

- [OpenAPI](contracts/openapi.yaml)：41个目标操作，包含上传/播放、检索、工作流、编辑、渲染和历史列表。
- [领域Schema](contracts/schemas/domain.schema.json)：59个共享定义，含输入、输出、时间范围、证据、预算与编译映射。
- [工具注册](contracts/tool-registry.json)：14个Agent工具，明确角色、输入输出、超时和副作用。
- [示例清单](contracts/examples/manifest.json)：正例、结构反例与业务时间反例。
- [需求任务验收矩阵](traceability/TRACEABILITY.md)：可读索引；同目录JSON可供程序校验。
- [当前进度](status/PROGRESS.md)：Codex每完成任务后维护；应用验收初始NOT_RUN。

这些合同是本项目的实施目标。不能据此声称参考仓库已经存在相同API，也不能把规范示例当作真实运行结果。

## 本包验证

在本目录、Python 3.11或更高环境中安装requirements-docs.txt所列校验依赖后运行：

```bash
python scripts/validate_spec_pack.py
```

它检查文件、Markdown引用、REQ/T/AT关联、任务依赖环、Schema、API参数、工具引用和正反例，结果写入status/SPEC_VALIDATION.json。应用测试由Codex实施并执行，文档静态检查不代替真实API、模型、媒体和业务验收。

## 最终产品完成标准

用户能上传长视频，查询处理进度，用文字或截图定位原片，得到带证据与范围说明的回答，通过自然语言生成可修改预览并导出真实视频；过程支持中断恢复、版本控制、预算限制和受控Team协作。M6还要求真实模型合同、质量基准、隔离、故障与恢复验收通过。只有这些实际证据齐全，Codex才能将应用标记为最终完成。
