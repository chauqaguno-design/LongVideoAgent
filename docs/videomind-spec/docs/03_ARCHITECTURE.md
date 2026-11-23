# VideoMind 目标架构与迁移边界

状态：设计待实现。版本：1.0。日期：2026-11-23。范围：REQ-PRJ-001、REQ-AGT-001～006、REQ-EDT-001～007、REQ-TEAM-001～003 及各感知/检索接口的集成边界。

本文件规定目标架构，不代表参考仓库已经具备这些能力。执行者必须先按实际 checkout 的 AGENTS、分支与提交复审；参考源码基线为 6bfbe9580271406fd0c2942f7800635d4a824e0e。字段与 API 以 [统一 OpenAPI](../contracts/openapi.yaml)、[领域 Schema](../contracts/schemas/domain.schema.json) 和 [工具注册表](../contracts/tool-registry.json) 为机器合同。发生冲突时先修订规范并说明迁移，不允许实现自行创造第二套字段。

## 1. 架构决策

保留现有 Python/FastAPI、application/domain/infrastructure 分层和前端。MySQL、Redis、MinIO、RabbitMQ/Celery、Qdrant 继续承担其已有职责；本轮不引入另一套工作流平台、图数据库或多个独立主 Agent。

通用 LLM/VLM 统一配置为 DeepSeek Flash。默认逻辑模型名 deepseek-flash；部署时必须通过真实 provider 能力探测并冻结 resolved model、endpoint 类型与能力指纹。不能由名称推断图片、原生工具调用或结构化输出可用。ASR、OCR、embedding、reranker、FFmpeg 是独立专用能力。禁止自动改用 Pro 或其他通用模型。

已有分析链路继续服务。新增编辑工作流单独拥有 TimelineRevision、EditOperation、RenderJob；逐步抽取共享 Harness 能力，避免先整体重写 AgentLoopService 才能剪第一条视频。

## 2. 已有实现与目标的差距

| 参考源码事实 | 目标设计与复用方式 |
|---|---|
| AgentLoopService 已有 Planner/Executor/Critic、预算和检查点 | 保留分析循环；新增可等待后台作业的 WorkflowRun，复用角色端口、监控和预算设施 |
| provider 使用 json_object，Executor 返回 FINAL/TOOL_REQUEST JSON | 保留结构化工具请求通道；另实现经能力测试的原生 tool-call 适配，二者归一到强类型 ToolExecution |
| X1 注册表固定三个读取证据工具，生产默认关闭 | 版本化新增视觉查看、时间线修改、提交渲染等工具；逐能力上线，不直接放开任意命令 |
| TaskKey 由 media_id/goal/mode 构成 | 旧分析身份不变；新增 workspace/project/run/revision 身份，不把 timeline revision 拼进 goal 字符串 |
| AnalysisResult 是标题、结论、证据、建议和章节 | 保留报告结构；编辑产物另由版本化时间线和 Artifact 表达 |
| 工具 EXECUTING 状态恢复可再次执行只读工具 | 修改/渲染通过持久幂等、事务 outbox、状态对账处理至少一次投递 |
| visual_score 匹配 OCR 文本 | 新增独立视觉观察/图像向量分支；不得把旧分数改名后称已支持图像理解 |
| 原始 ASR/OCR 聚合成 60 秒窗口，父块为 5 分钟带 1 分钟重叠 | 保留兼容投影；底层增加带真实源时间的 Observation、FrameArtifact、Segment 和 CoverageRecord |
| 本地媒体工作目录退出会清理；R4 OCR 接线未注入帧发布器 | 补 durable Artifact resolver，或按不可变原视频/PTS 重抽，禁止把临时路径当持久证据 |
| 执行历史与 checkpoint/tool ledger 分开 | 延续这一区分；渲染进度、团队消息不能塞进一份对话文本 |

上述事实的源码证据列于文末。它们需要在实施前再次核对，不是对任意用户 checkout 的保证。

## 3. 运行拓扑

~~~mermaid
flowchart TD
    UI["现有前端与任务中心"] --> API["FastAPI /v1 适配层"]
    API --> APP["应用服务与范围鉴权"]
    APP --> ANALYSIS["既有分析与检索"]
    APP --> RUN["WorkflowRun 与编辑协调"]
    RUN --> TOOLS["强类型工具执行"]
    TOOLS --> PERCEPTION["感知与证据适配器"]
    TOOLS --> EDIT["时间线服务"]
    TOOLS --> SUBMIT["后台作业提交"]
    SUBMIT --> SQL["MySQL 与事务 outbox"]
    SQL --> MQ["RabbitMQ / Celery"]
    MQ --> WORKER["媒体与模型 Worker"]
    WORKER --> OBJECT["MinIO Artifact"]
    PERCEPTION --> INDEX["Qdrant 索引代次"]
    EDIT --> SQL
    RUN --> SQL
    APP --> CACHE["Redis 缓存与通知"]
    WORKER --> SQL
    SQL --> API
~~~

图中是调用/数据流，不能解读为 API 可以绕过应用层直接访问用户不拥有的索引或对象。模型只看到经裁剪的上下文和被授权工具；身份、权限、对象路径、预算由服务端绑定。

## 4. 数据权威与持久性

| 存储 | 保存什么 | 不能承担什么 |
|---|---|---|
| MySQL | Workspace/Project、媒体版本、Observation/Evidence、CoverageRecord、IndexGeneration 指针、WorkflowRun/ToolExecution、编辑版本、作业、TaskBoard/Mailbox、outbox、预算账本 | 不存大视频字节；不把可变聊天摘要当唯一编辑状态 |
| MinIO | 不可变原始素材、帧/片段、缩略图、预览/导出、字幕和清单；对象 digest 与元数据绑定 | 对象写入成功不自动表示业务提交成功 |
| Qdrant | 每类 embedding 空间独立的版本化索引、来源和 generation 过滤字段 | 不作为媒体所有权、当前编辑版本或“事件真实发生”的权威 |
| Redis | 复用现有缓存、短期进度、通知、租约辅助；可从持久状态恢复 | Redis 单独丢失不能丢失编辑历史、已提交任务或最终产物 |
| RabbitMQ/Celery | 至少一次任务通知、重试与 Worker 执行 | 消息本身不是作业唯一状态；不能宣称传输天然 exactly-once |

所有业务记录带 workspace_id。workspace_id、actor、可读项目/素材范围从服务身份导出；模型不能传入更大 scope。数据库读取、向量候选过滤、artifact 解析、编辑输入与导出下载都必须复核范围。

服务生成 UUID。media_revision_id 绑定不可变原始 bytes digest；不同转码、观察模型、采样策略、embedding 模型、索引代次和 timeline revision 分别版本化。删除和失效从资产依赖关系传播，不能仅删除向量而保留可读取的导出引用。

## 5. 时间和证据的共同坐标

所有源区间采用 [start_us,end_us)，整数微秒且不超 JSON safe integer；秒数只用于展示。帧保存 requested_us、actual_pts、time_base_num/den；source_frame_index 只能在解码器实际提供时填入。不得用 t×fps 伪造真实帧号。

每个媒体流保存其源 PTS 与公共媒体起点的映射。音频和视频不能各自减掉不同起点后忽略原始偏移。编辑使用独立 output timeline_us。v1 顺序拼接、原速，服务端计算：

- clip 理想输出长度 = source_end_us − source_start_us，实际量化映射记录在 CompiledTimeline；
- 第一个 clip 输出起点为 0，后续起点为前面已编译 clip 的累计时长；
- 来源时间 s 的输出位置 = clip 输出起点 + s − source_start_us；
- 字幕先与源 clip 区间求交，再映射到输出位置。

真正切片时需按 decoder 实际 PTS 对齐并记录量化误差。v1 使用统一输出 profile 的解码/重编码路径；不把任意时间的 stream copy 宣称为精确切割。可变帧率、29.97fps、非零起始 PTS 和无音轨都进入媒体验收集。

Observation 是某模型/工具的观察，Evidence 是针对查询或决策的支持关系，CoverageRecord 是实际完成哪一级工作的账本。这三者不能互相替代。“抽过帧”不等于“模型看过”，更不等于“这个查询的所有事件都已核实”。

## 6. 模型与工具接口

Flash Gateway 复用并扩展现有 provider/role ports。允许同一个 resolved Flash model 有不同角色提示、上下文和输出 schema；禁止角色配置悄悄选择其他通用模型。

能力探测至少覆盖：文本结构化结果、图片输入、图片与结构化结果组合、原生工具调用及 tool result 续接、结构化 TOOL_REQUEST 闭环、usage 记录、超时和错误分类。探测结果带 endpoint/model/config 指纹，配置变化即失效。

原生 tool-call 与结构化 JSON 是两种协议适配：

- 原生模式须保留 provider call ID 与应用 call ID 的映射，完整返回每个已接收调用的工具结果。内部工具名通过 registry 的 provider_name 映射为安全 wire name，例如 video.search_evidence → video__search_evidence，不能直接把内部点号名字硬塞给 provider。
- JSON 模式由 schema 约束 FINAL/TOOL_REQUEST；解析失败最多一次有界修复，修复也计费。
- 两者均经过同一注册表、范围鉴权、参数校验、预算、幂等执行与结果大小限制。
- 如必要能力不通过，明确 BLOCKED 对应真实链路；mock 不得标记 provider 验收成功。显式时间段剪辑的 M1 可继续运行。

图片和视频 bytes 不进入普通 ToolResult JSON。工具返回受范围保护的 Artifact 引用与时间清单，provider adapter 按受控规则读取图片。用户文档、OCR、ASR、网页和工具输出中的指令均视为内容数据，不得修改工具权限或系统策略。

## 7. 编辑与后台任务边界

TimelineRevision 不可变。EditProject 只保存当前版本指针。应用服务是唯一写入入口，接收 expected_revision（整数 revision_no）与 idempotency_key。

一次提交在数据库事务内完成：检查既有幂等 receipt → 校验 payload digest → 新请求执行 CAS → 校验并编译 patch → 写 revision/operation → 更新当前指针 → 写 outbox。相同 key/相同 payload 返回原 receipt；同 key 不同 payload 或旧 expected_revision 返回 409。

RenderJob 固定 revision_id、profile 和 renderer_version；compiled_manifest_artifact_id 指向 CompiledTimeline 与 CompiledClipMapping，公开请求/实际源区间和输出微秒/帧范围。Worker 不接受自由文本 FFmpeg 命令或任意路径，只接受经校验的 typed plan；路径由 Artifact resolver 生成。输入快照固定后，后续用户修改不会改变在跑作业的输入。

输出先写 attempt 专属临时对象，完成结构检查与成片验证后，再发布 Artifact 并在数据库完成可见引用。超时或崩溃后的恢复首先查询已有 job/产物及校验和。允许重复物理计算，必须防止重复可见版本、重复有效导出和过期结果覆盖最新预览。

## 8. 工作流、取消与消息顺序

WorkflowRun 使用统一状态：QUEUED/RUNNING/WAITING_JOB/AWAITING_INPUT/SUCCEEDED/FAILED/CANCELLED/SUPERSEDED。WAITING_JOB 释放当前模型执行，不在模型工具调用里等待完整渲染。

AWAITING_INPUT 的补充通过 resume 以 state_version CAS 接受；仅 error.retryable=true 的 FAILED 允许追加尝试恢复，既有失败记录和预算保留。SUCCEEDED/CANCELLED/SUPERSEDED 不被恢复。运行中实质改目标建立关联新 run，并在安全步骤边界停止旧 run；不把任意 RUNNING 状态当可直接 resume。长时间工具需支持取消请求和进度检查。cancel_requested_at 表示已提出取消；只有 Worker 确认终止/输出不再发布后，作业才进入 CANCELLED。

WorkflowRun 返回 awaiting_input、superseded_by_run_id、budget_usage、last_event_id/last_event_sequence；预算费用未知为 null，不写 0。只有授权用户的 typed budget endpoint 可调整额度，模型不能通过消息要求提高预算。PROCESSING/INSPECTION 的 PARTIAL 是该 job 本次终态；等待它的工作流读取 JobView 后决定如何报告缺口或创建新的继续处理任务，不能无限等“PARTIAL 变 SUCCEEDED”。

旧版本的成功结果仍可作为历史 Artifact 保留；只有与当前项目 revision_id 匹配的结果可成为当前预览。过期工作流标记 SUPERSEDED，不得把它的模型总结追加为新版本的事实。

GET /v1/workflow-runs/{run_id}/events 通过持久事件序号提供 SSE cursor；断线恢复必须可以补读已提交事件。Redis 通知丢失只影响即时性，不影响正确性。

## 9. Agent Team 的位置

M5 才引入完整 Team。多个角色继续使用同一个 Flash model，区别在权限、上下文、任务和输出合同。TaskBoard/Mailbox 持久化；项目时间线始终由协调器经编辑服务单写。成员可提议 clip/patch，不能自行提交时间线或发布渲染产物。

任务依赖、排他 claim、租约 epoch、TTL、attempt、幂等结果和预算 reservation 必须由服务控制。Redis 锁只能优化协调，最终更新必须检查数据库中的 owner/epoch/CAS。租约过期的 Worker 即使仍在运行，也不能覆盖新持有者的结果。

Team 是可选执行策略。单 Agent 路径必须保留，团队成员失败可以在预算内串行接管，不要求每个请求都启动多个角色。

## 10. 分阶段迁移

| 阶段 | 交付边界 | 保持可用的退路 |
|---|---|---|
| M0 | checkout 审计、统一合同、provider 能力探测、最小身份和测试设施 | 旧分析 API 不变；真实模型不通过则显示不可用能力 |
| M1 | 显式区间 → 时间线版本 → 真 FFmpeg 预览 → 修改 → 导出 | 无需等待智能检索、视觉模型或 Team |
| M2 | 长视频分段处理、覆盖账本、多模态记忆、索引原子发布 | 处理失败显示 PARTIAL 与缺失范围 |
| M3 | 多路检索、截图检索、证据问答和按需复看 | 证据不足明确不确定，旧文本链路可保留 |
| M4 | 自然语言 EditBrief → 有证据的选段 → QA 与迭代 | 可切回显式区间编辑与人工调整 |
| M5 | TaskBoard/Mailbox、同 Flash 多上下文协作 | 单 Agent 路径持续可用 |
| M6 | 迁移、备份恢复、故障注入、隔离、成本与精度验收 | 新能力独立开关；保留读取旧数据的版本适配 |

数据库采用 expand → backfill → switch → contract 方式；先增加新表/可空关联，不把旧 TaskKey、checkpoint 或 JSON payload 就地改为不兼容结构。旧媒体整数 ID 经适配映射到新 UUID，映射具有 workspace 约束；不得直接按整数值猜测所属租户。

## 11. 源码证据与实施复审清单

以下链接均固定到参考 SHA；实施时记录实际分支差异：

- [AgentLoopService](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/agent.py)：792–846 行为只读工具的至少一次恢复；1450 行起为有界运行。
- [provider 实现](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/providers/model.py)：json_object、ExecutorTurn 结构化请求，不应误写为已有原生 tools 协议。
- [工具合同](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/tool_contracts.py)、[工具 ledger](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/tool_state.py)、[X1 配置](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/x1_config.py)。
- [TaskKey/AnalysisRequest](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/value_objects.py)、[AnalysisResult](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/domain/analysis.py)。
- [R4 媒体接线](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/r4_media.py)、[检索实现](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/retrieval.py)。
- [checkpoint 修订](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/checkpoint_service.py)、[语义历史](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/execution_record.py)。
- [API composition](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/presentation/api/r4_runtime.py)、[Celery 接线](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/celery_tasks.py)、[现有基础设施](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/docker-compose.r2.yml)。

所有外部 API、图片理解、检索精度、长视频性能及渲染效果必须运行验收后填写结果；本文不提供已通过声明。
