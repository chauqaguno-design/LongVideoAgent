# 004 — 实现计划

状态：TODO。本计划为目标实现，列出的新增模块和测试命令均待 Codex 创建；本轮没有执行模型调用或应用测试。

## 1. 真实接入点与约束

参考源码基线为 6bfbe9580271406fd0c2942f7800635d4a824e0e；实施先在真实 checkout 复核目录、AGENTS、实际分支和迁移工具。

| 已有路径 | 已核实用途 | 迁移决策 |
|---|---|---|
| application/agent.py | AgentLoopService；EXECUTING 只读工具可至少一次重执行；调用预算和超时 | 保持现有分析；共享执行基础设施前先分清只读恢复和写操作对账 |
| application/ports/ai.py、infrastructure/providers/model.py | Planner/Executor/Critic ports；json_object 和自定义 ExecutorTurn | 新增 Flash gateway 适配能力，保留当前 JSON 模式；native 工具模式单独实现 |
| application/tool_contracts.py、tool_policy.py、tool_state.py | 三个只读 video 工具、参数限制、服务端 policy、ledger | 版本化扩展 registry；每个 effect 类型有恢复器，不能仅扩大 ToolName 枚举 |
| infrastructure/x1_config.py | Agent tool calling 默认关闭 | 保持旧默认；新功能开关经能力、隔离和恢复门槛后开启 |
| application/checkpoint_service.py、value_objects.py | 分析状态；TaskKey=(media_id,goal,mode)，media_id 为整数 | 新 run 使用 UUID 与真实 workspace/project，不复用 TaskKey 做编辑工作流唯一键 |
| application/execution_record.py、historical_replay.py | 有界语义事件与只读历史重放 | 保留旧 schema reader；新 durable workflow event 独立于高频进度 |
| application/conversation_memory.py | recent turns + summary，有 TTL 与 CAS | 作为上下文输入，不能成为 timeline/evidence 的唯一存储 |
| infrastructure/celery_tasks.py、application/task_lease.py | acks_late、worker lost 重投、租约 | 延续至少一次队列，给新 job/run 增加持久 fencing 与 idempotency |
| presentation/api/r4_runtime.py、presentation/composition.py | 依赖装配与生产生命周期 | 添加 ports/adapters，保留既有 API；新增 /v1 路由 |

旧代码每个 task 范围内的 tool-call-N 不能作为全局 UUID。旧历史最多 128 个语义事件且有大小限制，新渲染进度不能逐秒塞入同一个 legacy record。旧调用的 RESULT_STORED/CONSUMED 要继续可读；恢复时不因业务状态 FAILED/TRUNCATED 就重新执行已完成调用。

## 2. 最小分层

建议在现有 application 层增加 WorkflowService、ToolExecutionService、ContextAssembler、BudgetLedger ports；在 infrastructure 增加 FlashGatewayAdapter、SQL workflow/ledger/outbox adapters；类名和路径在 T00-001 审计后确定。保持 media/retrieval/edit/render service 的业务所有权。

一期由 WorkflowService 支持 QUESTION/INSPECT/EDIT 调度，但 M1 编辑 API 可直接调用 005 服务。避免把现有 AgentLoopService 一次性改成同时负责会话、数据库、媒体编辑和团队调度的大类。

Gateway 输入是已裁剪上下文、允许工具 schema、能力指纹、预算 token；输出是类型化 final/tool requests 与 usage。原始 SDK response 只能存脱敏诊断。图像使用受控 Artifact resolver 获取的有限资源，不接受模型构造的任意 URL。

## 3. 数据与事务

WorkflowRun 是状态读模型，公开 awaiting_input、superseded_by_run_id、cancel_requested_at、last_event_id/last_event_sequence 与 budget_usage；内部 checkpoint 保存 phase、pending tool executions、pending job IDs、输入版本、能力指纹、预算快照引用、resume token 与结果引用。大 prompt、媒体 bytes 和完整转录不进 checkpoint。

ToolExecution 内部表至少保存 run_id、server call ID、provider correlation ID、contract version、role、参数 digest、effect、授权结果、reservation、执行/结果时间、结果 artifact 或有限 JSON、业务 operation/job receipt 引用。允许读取与写入使用不同恢复策略，但所有已执行物理尝试可审计。

调用授权和业务事务无法总在同一事务内完成；因此规范为：

1. 持久化调用意图和稳定 idempotency key。
2. 调用拥有业务事务的应用服务。
3. 持久化返回 receipt，再推进消费 checkpoint。
4. 任何中间崩溃按同 key 查 receipt/业务状态，不能凭“ledger 未完成”推断副作用不存在。

新事件采用 MySQL outbox。业务状态和 outbox 同事务；dispatcher 可重复发送，消费者有 durable inbox 去重。高频 PROGRESS 可合并；状态变更、用户输入、证据与最终产物事件必须可补读。

## 4. Provider 与上下文实现

先验证文本结构化输出、图像内容、图像+结构化组合、原生工具闭环、JSON 工具闭环、usage/错误；探测模型与生产配置必须一致。native_tools=false、structured_json=true 时可配置 JSON 模式，前提是它自己的闭环真实通过；image=false 时视觉能力保持不可用。禁止 runtime 静默换模式或模型。

JSON 模式执行严格 schema 校验，最多一次格式修复。native 模式把 provider call ID 映射到应用 ID，并按 registry.provider_name 转换安全工具名，例如 video.search_evidence → video__search_evidence；返回名称只能反解为本次允许工具。同响应多调用逐个持久化和匹配回传，首版串行，无权限的调用返回类型化 denied。两种模式共用同一个工具服务。

上下文由授权 scope、用户要求、EditBrief/当前固定 revision、当前任务、必要 evidence/artifact 摘要、未决工具结果组成。候选检索命中、观察、核实与用户陈述保留来源标签。压缩做结构化保留，不能用纯文本摘要替换源 ID 或删除 must_include/must_exclude。上下文超限且无法安全压缩时进入 AWAITING_INPUT 或明确失败。

## 5. 异步续接、steering 与取消

POST /v1/workflow-runs 创建 durable run 并返回 202。GET 读取权威状态。POST /{run_id}/resume 接受 expected_state_version、message、idempotency_key；同 key 同请求优先重放 receipt，再校验新请求的状态版本。仅 AWAITING_INPUT 和 error.retryable=true 的 FAILED 可恢复 RUNNING；后者追加 attempt，保留旧失败记录与累计预算。checkpoint、恢复 receipt、outbox 同事务提交。SUCCEEDED/CANCELLED/SUPERSEDED 及不可重试 FAILED 不被恢复，RUNNING 不接受直接 resume。

实质变更通过 WorkflowCreate.supersedes={run_id,expected_state_version} 创建关联新 run。先幂等，再验证同 workspace/project、目标非终态及 state_version，事务创建新 run、将明确目标标记 SUPERSEDED/设置 superseded_by_run_id、写 outbox；终态或过期版本为 409。未传 supersedes 不影响项目中其他并行 run。工作者在下一安全点停止旧任务，旧结果不能推动新 run。

job 完成事件必须匹配 run 的 pending_job_ids、input revision、resume token；过期 job 的产物可以保留但不能驱动新的计划。JobRef/JobView 用 kind 区分 PROCESSING/INSPECTION/RENDER，terminal 与 result_artifact_ids 表示实际完成；处理/查看的 PARTIAL 也是本次终态。工作流可报告已完成部分或新建继续处理任务复用缓存，不能让原 job 原地变 SUCCEEDED。等待作业时只保存状态，不占用一次持续的 LLM 请求。jobs.get 提供查询，不允许模型无限轮询。

POST /{run_id}/cancel 接受幂等取消请求；协调取消下游，仅当该 run 对共享 job 有独占责任时停止物理 job，否则解除本 run 订阅，保留其他合法订阅者。已成功终态返回当前实际状态。取消事件、发布与结果提交使用同一 fencing/CAS 规则。

SSE /events 使用 Last-Event-ID 定位 sequence；连接建立时先定快照水位，再补事件并订阅，避免快照/订阅间隙。cursor 不属于 run 或已超保留期限返回 409 CURSOR_EXPIRED；客户端读取快照后重新订阅。不可直接把 Redis pub/sub 当历史存储。

## 6. 预算、超时与恢复

预算以服务器策略限制请求上限。reservation 在 MySQL 原子扣减可用额，不能只在各进程内累加；成功后按实际 usage 结算，未知成本保留 pending/unknown 内部记录。公开 BudgetUsage 的 committed/reserved/uncertain_microunits 未知为 null，对应固定 currency、price_version 与 pricing_status，禁止不同币种裸相加。定价缺失且运行要求货币硬预算时，阻断新的付费调用，不以 0 费用穿过预算检查。

PATCH /v1/workflow-runs/{run_id}/budget 只对授权用户开放，不注册模型工具。它按 BudgetAdjust 执行 same-key-first、state_version CAS、服务器预算上限和已 commit+reserved 下界检查，同事务写审计/outbox。预算更新与 resume 分开，不能顺便复活终态。

既有 AgentExecutionBudget 可复用单次 deadline 传播，但仍需调用前预留、跨重启累计和后台作业 deadline。HTTP/provider 超时可能已经产生成本；重试必须计新物理尝试。取消到达时停止后续预留，Worker 检查 run 状态后方可提交结果。

故障分类固定：schema/权限/非法资源/CAS/idempotency conflict 不自动重试；网络/限流依据 deadline 有界重试；未知写结果先对账；能力不可用阻断相关链路。运行配置和重试上限写入 run，恢复不能默认放宽。

## 7. 迁移与发布

新增表和索引先 expand，再兼容旧读路径；旧 analysis TaskKey 与 UUID 资源建立显式服务端映射。checkpoint/ledger/history 采用合同版本 reader，不修改旧行假装新协议。功能开关旧分析、新 Harness、native adapter 分开；回滚停止新写入但仍可读新版本结果与状态。

运行历史只重放记录，不调用模型、不再执行编辑或渲染。旧版无法理解的新 event 明确显示版本不支持，不能略过后把结果标成完整。恢复没有必要材料时 fail closed 并保留诊断。

## 8. 验证策略与待创建命令

实现时创建 tests/spec_004，建议命令如下；此处是未来验收入口，不表示文件现在已存在或已经通过：

~~~bash
python -m pytest tests/spec_004/test_provider_contract.py tests/spec_004/test_tool_policy.py
python -m pytest tests/spec_004/test_workflow_recovery.py tests/spec_004/test_budget_cancel.py
python -m pytest tests/spec_004/test_history_compatibility.py tests/spec_004/test_sse_resume.py
python -m pytest tests/spec_004/test_live_flash.py -m live_provider
~~~

单元测试使用受控 provider fixtures 验证协议与失败；真实 Flash gate 独立保存能力报告，不允许 mock 替代。集成测试使用真实 MySQL/Redis/RabbitMQ 的隔离测试资源，注入结果保存前后断进程、重复消息、租约过期、预算竞争、取消/成功竞态和异租户引用。已有历史 fixture 必须原样读通。

## 9. 源码依据

- [agent.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/agent.py)
- [providers/model.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/providers/model.py)
- [tool_contracts.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/tool_contracts.py)
- [tool_policy.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/tool_policy.py)
- [value_objects.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/value_objects.py)
- [execution_record.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/execution_record.py)
- [r4_runtime.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/presentation/api/r4_runtime.py)
