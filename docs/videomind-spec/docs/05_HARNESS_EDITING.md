# Harness、编辑与 Agent Team 的执行设计

状态：待实现规范。日期：2026-11-23。核心要求：REQ-AGT-001～006、REQ-EDT-001～007、REQ-TEAM-001～003。

本文件解释跨功能协作；逐项验收见 [004 Harness](../specs/004-agent-harness/spec.md)、[005 编辑渲染](../specs/005-editing-render/spec.md)、[006 Agent Team](../specs/006-agent-team/spec.md)。所有协议引用 [OpenAPI](../contracts/openapi.yaml)、[domain.schema.json](../contracts/schemas/domain.schema.json)、[tool-registry.json](../contracts/tool-registry.json)，不另定义一份 wire schema。

## 1. Harness 应管理什么

Harness 管理模型每一步看到什么、可以调用什么、已完成什么、如何恢复与停止；业务事实仍由媒体、证据和编辑服务拥有。

四种状态必须区分：

| 状态 | 权威来源 | 恢复方式 |
|---|---|---|
| 对话理解：用户想要什么、代词指谁 | 持久用户消息、EditBrief、带引用的摘要 | 加载最近消息和明确约束，重新读取当前版本 |
| 工作流：下一步做什么、等待哪个任务 | WorkflowRun checkpoint 与事件 | 从最后已提交步骤继续 |
| 工具执行：调用是否授权、是否已有结果 | ToolExecution/既有 ledger 的版本适配 | 查询幂等 receipt，复用已存结果或对账 |
| 业务产物：当前剪辑版本、成片是什么 | TimelineRevision/EditOperation/RenderJob/Artifact | 按 ID 读取，不能从聊天文字重新猜测 |

不能把一长段“记住我们刚才的剪辑”当作撤销与重试机制。对话压缩保留约束、未决问题、当前 revision_id、evidence/artifact ID；不嵌入视频 bytes、全片转录或过期文件路径。压缩失败时明确缩减/暂停，不静默丢失用户硬约束。

## 2. Flash 接入先验证再开放

全角色使用 DeepSeek Flash。逻辑模型名默认 deepseek-flash，真实部署以能力探测得到的 resolved model 为准。能力指纹至少包含 endpoint、provider adapter 版本、resolved model、功能开关与探测时间；密钥只存部署配置，不写入报告和数据库事件。

必要探测分开运行：

1. 一个固定 schema 的文本结构化响应。
2. 一个本地已知内容图像的输入与结构化响应。
3. 原生工具调用：模型请求一个无副作用测试工具，服务回传结果，模型据此完成响应。
4. 结构化 JSON 工具请求：FINAL/TOOL_REQUEST 合同、无效 JSON/参数校验、工具返回后续接。
5. usage、超时、限流和 provider 错误形状；缺失 usage 明确标记 unknown。
6. 实际采用的图片与工具组合输入，而非只测试两种能力各自可用。

原生模式和 JSON 模式可以由部署选择，不能在运行中悄悄切换已记录合同。必要能力失败则标记对应链路 BLOCKED，提供诊断与配置修正路径，禁止换模型掩盖。M1 显式时间段编辑只依赖媒体能力，仍应可独立验收。

当前参考 provider 主要通过 json_object 与自定义 ExecutorTurn 运行。新增 native tool-call 需要新的协议适配和回归；不应因 SDK 有 tools 参数就宣称已有支持。

## 3. 一轮模型/工具循环

~~~mermaid
flowchart TD
    LOAD["加载 run、版本、范围和预算"] --> MODEL["Flash 下一步"]
    MODEL --> KIND{"输出类型"}
    KIND -->|"最终结果"| CHECK["结构与证据校验"]
    KIND -->|"工具请求"| ADMIT["schema、范围、预算、幂等"]
    ADMIT --> EXEC["执行或复用 receipt"]
    EXEC --> WAIT{"后台作业"}
    WAIT -->|"已提交"| PARK["持久 WAITING_JOB"]
    WAIT -->|"即时结果"| SAVE["存 ToolResult 与 checkpoint"]
    SAVE --> MODEL
    CHECK --> DONE["完成或有界修复"]
    PARK --> RESUME["job 事件触发恢复"]
    RESUME --> LOAD
~~~

执行顺序必须保证：先持久化应用调用身份，再执行可能产生副作用的工作；先存结果，再推进消费状态。原生 provider call ID 只做协议关联，应用 call ID 与幂等键由服务生成且可跨恢复复用。

内部工具名和 provider wire name 使用 registry 的一一映射，例如 video.search_evidence → video__search_evidence；响应名称也须按同一映射反解并检查允许列表。不能让模型选择映射目标，也不能把 provider call ID 当应用幂等键。

注册表必须声明参数/结果 schema、读写类别、作用域、时间/帧数/结果大小上限、可否重试、恢复方法、预算类别和异步结果语义。原生批量 tool calls 首版可串行执行；每个调用必须得到匹配结果，不允许只回第一个然后丢弃其余。写操作按项目串行提交。

执行器不接受模型提交的 user/workspace/owner、SQL、任意 shell、文件系统路径或下载 URL 作为权限凭据。媒体 ID 是候选引用，实际解析仍由应用层根据 run 的可信 scope 完成。

## 4. 三类工具的恢复语义

| 类别 | 例子 | 恢复规则 |
|---|---|---|
| 无副作用读取 | 读已有证据、读 timeline | 可以重试；记录实际调用成本；固定查询版本 |
| 可缓存的派生计算 | 抽帧、局部查看、embedding | 固定输入版本和参数 digest；复用已验证 artifact；失败不写伪完成结果 |
| 持久修改/任务提交 | 提交 edit patch、创建 render job | 通过幂等 receipt 与状态查询恢复；不能无条件重新执行 |

原有 AgentLoop 对 EXECUTING 的恢复是只读工具 at-least-once 重执行。扩展后要保持“工具调用完成”与“后台媒体作业完成”分开：submit render 的结果可为 job_id+QUEUED，而 run 转 WAITING_JOB，不能输出“视频已导出”。

ToolExecution 的已存结果不可随渲染进度反复覆盖；RenderJob 自己维护 QUEUED/RUNNING/VALIDATING/SUCCEEDED/FAILED/CANCELLED。高频进度进入 job 投影，状态转换与关键产物进入持久事件。

异步通用接口使用 JobRef/JobView 的 job_id、kind、terminal 和 result_artifact_ids，区分 PROCESSING、INSPECTION、RENDER。处理/查看的 PARTIAL 表示本次 job 结束且留有缺口；WorkflowRun 可从 WAITING_JOB 续接报告/补问或创建新的处理任务，不能继续轮询原 job 等它自动完整。

## 5. 预算、修复与停止

预算有四层：workspace/project 总预算、WorkflowRun 累计预算、角色/工具 reservation、单次 provider/FFmpeg deadline。等待队列时间与 active 模型执行时间分别记录；作业还有绝对最大等待时间和最大运行时间。重试不重置累计预算。

初始可调策略由部署配置固定并写入 run，建议结构：

- 单次 schema 修复最多一次；同一输入同一错误再次出现即返回可诊断失败。
- 证据不足允许扩大检索或局部复看，但次数、帧数和时长都有上限。
- 每次新任务先 reservation，成功后结算实际 usage；超时导致未知费用时保留 unknown，不按零费用退回全部额度。
- Team 成员从同一项目/run 预算分配，不各自获得一整份总预算。
- 达预算上限时返回已完成结果和未完成范围；不能换 Pro 或加倍预算来“修复”。

公开费用由 BudgetUsage 的 committed/reserved/uncertain_microunits、pricing_status、price_version 与计数表达，未知值为 null。定价缺失且要求货币硬预算时阻止新的付费调用。只有授权用户可 PATCH /v1/workflow-runs/{run_id}/budget；先幂等再 state_version CAS，新额度不得低于已承诺与预留部分，模型不能代替用户调用此端点。

取消请求先持久化，再由 Worker 在可终止边界执行。FFmpeg 子进程确认回收、临时文件隔离、输出发布停止后才能标记 CANCELLED。若取消与提交已验证产物并发，以同一数据库状态机/CAS 仲裁；不得同时公开 SUCCEEDED 与 CANCELLED 两个终态。

## 6. 编辑 Brief 到时间线

M1 从用户显式区间开始，不依赖自然语言理解。M4 才加入 Flash 生成 EditBrief：目标、受限素材、输出规格、目标时长和允许误差、必须保留/排除内容、音频/字幕策略。硬约束和偏好分开；含糊且会改变结果的条件进入 AWAITING_INPUT，其他情况使用已记录默认值继续。

模型提出候选 clip，必须包含真实 media_revision_id、源区间与 evidence_ids；系统验证引用属于当前授权 scope。对于动作与片段边界，要在提交时间线前局部复看，不能把单帧相似度当作动作完成证明。

v1 TimelineRevision：

- clips 是用户/模型指定顺序的剪辑选择；服务端应用 reorder 操作、校验并计算 output timeline_us，不擅自按源时间重排叙事顺序。
- 原速顺序拼接，默认硬切，统一 output_profile。
- 每 clip 有 stable clip_id、media_revision_id、source_start_us/end_us、evidence_ids、audio_policy keep/mute、可选 crop/caption_track_id。
- caption_policy 为 none/preserve_source/burn_aligned。
- 源画面内的烧录字幕属于像素，不能通过删除独立字幕轨来移除。
- v1 不支持高级转场、变速和无限层合成；不得产生 schema 表面接受而 renderer 静默忽略的字段。

append/replace 输入使用无 clip_id 的 ClipDraft。append 的新 UUID 由服务生成，在 OperationReceipt.clip_id_assignments 按 operation_index 回传；replace 保留目标 clip_id。相同请求重试回传同一 IDs。v1 没有同 patch 临时 ID；新增片段按 append 顺序，需另行重排时先取返回 revision/IDs 再提交新 patch。

素材不足以达到目标总时长时必须说明缺口；不能通过重复同一片段、虚构事件或延长源区间满足约束。允许的重复必须来自明确用户要求。

## 7. CAS 与幂等提交的精确顺序

POST /v1/edit-projects/{edit_project_id}/operations 的服务行为：

1. 从会话读取 workspace/actor；读取并校验项目权限。
2. 规范化 patch，计算包含 expected_revision 的请求 digest。
3. 查找同项目 idempotency_key。存在且 digest 相同，返回原 receipt，即使当前版本已前进；不同则 409。
4. 对新请求锁定/条件更新当前版本；expected_revision 必须等于当前 revision_no，否则 409。
5. 编译修改，校验每个 clip 与源版本、crop/音频/字幕、输出 profile。
6. 同一事务写不可变 TimelineRevision、EditOperation、当前指针和 OutboxEvent。
7. 事务提交后响应；broker 不可用由 outbox 重试，不回滚已提交时间线或创建第二个 revision。

回退也是创建一个新的 revision，内容引用旧版本的选择，revision_no 继续递增；不删除已存在版本。不让模型直接写 timeline_start_us。

## 8. 渲染与时间映射

RenderJob 固定 revision_id、profile、renderer_version；cache key 包含规范化计划及所有素材/字幕/字体等实际依赖的不可变标识。compiled_manifest_artifact_id 指向 CompiledTimeline，包含 requested/realized 源范围与 output_start/end_us、output_start/end_frame。不同输出规格不能误命中同一个成片。Artifact.media_revision_id 与 timeline_revision_id 分别表达源媒体和编辑版本关系。

v1 编译器确定视频帧率、分辨率、像素格式、音频采样率/声道策略和时长。混合输入先归一再拼接。v1 对无音轨和 mute clip 显式合成等长静音轨，统一到 48 kHz；不能依赖 concat 的偶然默认行为。

源字幕与 clip 求交后平移到输出时间。keep/mute 控制源音频；混合素材中音频必须随视频裁切、补静音并拼接，不保留前一素材越界尾音。背景音乐、自动配乐与复杂混音不是 v1 默认功能，新增前需扩充合同和验收。

Worker 用 argv 执行已编译 plan，禁止 shell=True。crop 使用受检整数区域，须在源有效图像范围内；纵横比变化按输出 profile 明确 scale/pad 或裁切，不能默默拉伸。

结构检查包括：输入存在、PTS 映射、区间不越界、时长守恒、合法 crop、字幕区间、输出媒体 streams/profile。成片检查必须读取真实输出，验证可解码、首尾与拼接点、音视频同步及字幕对齐。语义 QA 再检查所选内容是否符合 Brief；两类检查分开报告。

## 9. 崩溃和重试窗口

| 故障点 | 必须恢复的行为 |
|---|---|
| edit 事务提交前失败 | 没有可见新版本；同 key 可重试 |
| edit 已提交但响应丢失 | 同 key 返回原 revision/receipt |
| job/outbox 已提交，broker 发送失败 | outbox 重试；API 可查询 QUEUED |
| broker 重复投递 | 同一 job 被排他 claim；不能重复可见导出 |
| FFmpeg 完成但 job 未落库 | 先查询 staging/已存在产物及 digest，再验证并补提交 |
| Worker lease 过期后仍返回 | owner/epoch 校验拒绝旧 Worker 的状态提交 |
| 验证失败 | job FAILED，产物不可作为成功预览；保留有限诊断并回收 staging |
| 用户已提交新 revision，旧 render 才成功 | Artifact 关联旧 revision；不改当前预览指针 |
| Redis/通知丢失 | 从 MySQL 及持久事件重建；不能丢已提交任务 |
| API/SSE 断线 | cursor 补读，客户端按事件 ID 去重 |

这能控制重复副作用，但不保证所有崩溃场景下只发生一次物理计算；对外统一声明至少一次执行、持久幂等发布。

## 10. Team 的最小职责

M5 角色固定为 COORDINATOR、MATERIAL、EDITOR、REVIEWER。素材检索和局部观察都是 MATERIAL 的有界任务，不新增角色枚举。所有通用模型调用仍是同一 resolved Flash；角色只改变任务、上下文、工具权限与输出 schema，不改变模型。

TaskBoard 保存依赖 DAG、输入 revision/evidence、状态、owner/lease epoch/expiry、attempt 和预算。Mailbox 保存有 message_id、task_id、发送/接收身份、序号、payload 引用的事件；ack 在结果持久化后完成，重复消息用 durable inbox 去重。TTL 指投递/处理期限，超期有明确失败或重分配，不静默丢消息。

成员产出候选证据、clip proposals 或验证报告。只有协调器可请求时间线提交，且仍由编辑服务执行 CAS；“单写者”不能替代服务端 CAS。父任务取消向后代传播，未运行子任务直接取消，运行中的任务协作取消；失联成员由租约过期重分配，旧 epoch 结果不能提交。

先完成 M1/M4 的单 Agent 剪辑链，再用 Team 验证是否改善质量或延迟。没有收益证据的任务保持串行。

## 11. 参考源码

- [agent.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/agent.py)：工具恢复和预算边界。
- [providers/model.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/providers/model.py)：当前结构化工具循环。
- [checkpoint_service.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/checkpoint_service.py)：既有 staged revision 是分析计划重做，不是编辑历史。
- [execution_record.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/execution_record.py)：最多 128 个语义事件，不应用来写每秒渲染进度。
- [historical_replay.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/historical_replay.py)：历史重放只读，不重新执行外部工具。
- [runner.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/media/runner.py)：现有 argv subprocess、超时和取消回收可复用。
