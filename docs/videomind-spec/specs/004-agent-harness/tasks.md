# 004 — 可执行任务清单

状态见各项清单与 status/PROGRESS.md。T04-001～003 在 M0 执行；其余主线在 M3。每项先核实 [spec](spec.md) 与 [plan](plan.md)，再写实现。以下 tests/spec_004 路径均为待创建验收入口。缺少真实 provider 或测试服务时记录 BLOCKED，不能勾选通过。

### T04-001 核实旧 Harness 兼容边界

- [x] VERIFIED
- 需求：REQ-AGT-003、REQ-OPS-006
- 依赖：T00-001
- 产物：实际 SHA、路径、TaskKey、checkpoint/ledger/history 版本、默认开关与新旧路由映射审计。
- 验收：逐项对照源文件；保存至少一份旧 checkpoint 和历史 fixture；确认不修改 AGENTS 或旧状态语义。

### T04-002 建立 Flash 配置与能力合同

- [x] VERIFIED
- 需求：REQ-AGT-001
- 依赖：T04-001、T00-003
- 产物：单一逻辑模型配置、resolved model/adapter 指纹、能力报告持久引用与接口适配。
- 验收：test_provider_contract.py 校验禁止其他通用模型回退；缺字段和无效模型配置明确失败；报告不含密钥。

### T04-003 实测 Flash 能力预检

- [ ] BLOCKED
- 需求：REQ-AGT-001、REQ-UX-002
- 依赖：T04-002、T00-002
- 产物：文本、图片、组合输入、native/JSON 工具闭环、usage/错误的真实探测报告。
- 验收：test_live_flash.py -m live_provider 在目标 endpoint/model 执行；失败项标记 BLOCKED；mock 结果不计此 gate。

### T04-004 建立统一类型化工具注册表

- [ ] IMPLEMENTED
- 需求：REQ-AGT-002、REQ-AGT-006
- 依赖：T04-001、T00-003
- 产物：对齐 contracts/tool-registry.json 的 schema、role/effect、结果上限、恢复策略适配。
- 验收：test_tool_policy.py 覆盖未知工具、额外字段、超时限、readonly/write 分类；注册表与合同无漂移。

### T04-005 保持 JSON 工具模式并有界修复

- [x] VERIFIED
- 需求：REQ-AGT-001、REQ-AGT-005
- 依赖：T04-002、T04-004
- 产物：FINAL/TOOL_REQUEST adapter、schema 验证、最多一次格式修复与用量记录。
- 验收：test_provider_contract.py 覆盖无效 JSON 连续两次、工具后续接、错误不产生副作用；旧协议 fixture 可读。

### T04-006 增加原生工具协议适配

- [ ] IMPLEMENTED
- 需求：REQ-AGT-001、REQ-AGT-002
- 依赖：T04-004、T04-005
- 产物：native call ID 与 registry.provider_name 安全名字映射、多调用串行闭环、每调用结果关联与模式锁定。
- 验收：协议测试覆盖两个工具请求、一项拒绝、重复 provider ID；真实能力可用才启用 native 模式，不影响已通过 JSON 模式。

### T04-007 实施可信资源解析与执行层授权

- [x] VERIFIED
- 需求：REQ-AGT-002、REQ-AGT-006、REQ-OPS-001
- 依赖：T04-004、T00-004
- 产物：服务层 scope/ownership 校验，artifact resolver，模型输入与可信身份分离。
- 验收：test_tool_policy.py 使用两个 workspace 交叉引用 media/revision/artifact/run；全部在检索/解码/写入前拒绝，错误不枚举其他租户资源。

### T04-008 持久化 WorkflowRun 与 checkpoint

- [x] VERIFIED
- 需求：REQ-AGT-003
- 依赖：T04-001、T00-003、T00-004
- 产物：状态机、state_version CAS、固定输入版本、phase/pending jobs/结果引用与迁移。
- 验收：test_workflow_recovery.py 覆盖每个合法转换、非法终态恢复、双 Worker claim 和进程重启续接。

### T04-009 持久工具 ledger 与业务 receipt 对账

- [x] VERIFIED
- 需求：REQ-AGT-004
- 依赖：T04-004、T04-007、T04-008
- 产物：server call ID、参数 digest、物理尝试、结果持久化、按 effect 分类的恢复器。
- 验收：test_workflow_recovery.py 在业务提交后/ledger 保存前杀进程；使用受控写服务确认同 key 返回同一 receipt；只读存结果后不重复调用。

### T04-010 实现事务 outbox 与幂等消费

- [x] VERIFIED
- 需求：REQ-AGT-004、REQ-AGT-003
- 依赖：T04-008、T05-009、T00-002
- 产物：复用 M1 已建立的共享 outbox/dispatcher，扩展 workflow durable inbox、重试与死信诊断；005/004/006 不各建一套事件表和调度器。
- 验收：真实队列集成中 broker 断连、发后确认前崩溃、重复 delivery；状态不丢且一个事件仅驱动一次逻辑转换。

### T04-011 实现后台作业等待与唤醒

- [x] VERIFIED
- 需求：REQ-AGT-003、REQ-AGT-004
- 依赖：T04-009、T04-010
- 产物：WAITING_JOB checkpoint、pending job/resume token、JobRef/JobView kind/terminal、完成/失败/PARTIAL 续接和超期处理。
- 验收：test_workflow_recovery.py 检查队列等待不占模型循环，重复/过期/异 run 事件不能错误推进；已提交 job 不被说成已完成。

### T04-012 加入持久预算预留与结算

- [x] VERIFIED
- 需求：REQ-AGT-005、REQ-OPS-002
- 依赖：T04-008、T04-009
- 产物：workspace/run/reservation 层预算、BudgetUsage 的 nullable 费用/定价版本、模型/图像/工具计数、仅授权用户的 PATCH budget 与审计。
- 验收：test_budget_cancel.py 并发预留不超额，重试计物理成本，重启不重置预算，unknown usage 不按零自动退款；缺定价阻断货币硬预算，预算更新先幂等再 CAS，模型无调整权限。

### T04-013 实现协作取消与 deadline 传播

- [ ] IMPLEMENTED
- 需求：REQ-AGT-005
- 依赖：T04-011、T04-012
- 产物：幂等取消请求、终态仲裁、共享 job 订阅规则、子进程/HTTP deadline 与租约 fencing。
- 验收：test_budget_cancel.py 覆盖调用前、调用中、job 等待、结果发布并发取消；确认没有 orphan 子进程或取消后新写操作。

### T04-014 支持多轮补充与计划替换

- [x] VERIFIED
- 需求：REQ-AGT-003、REQ-EDT-006
- 依赖：T04-008、T04-013
- 产物：resume expected_state_version/idempotency、AWAITING_INPUT/可重试 FAILED 恢复、WorkflowCreate.supersedes 的明确目标+CAS、关联新 run 与 SUPERSEDED 事务。
- 验收：test_workflow_recovery.py 验证双客户端冲突、同 key 重放、可重试失败追加 attempt 且不清空费用、成功/取消/替换不复活、旧 job 不驱动新计划。

### T04-015 构建可压缩且保留来源的上下文

- [x] VERIFIED
- 需求：REQ-AGT-003、REQ-AGT-006
- 依赖：T04-007、T04-008
- 产物：ContextAssembler、约束保留清单、artifact/evidence 摘要、压缩失败处理。
- 验收：受控长上下文 fixture 压缩后 must_include/exclude、revision、未决问题和来源仍在；字幕注入文本不能变更权限，摘要不产生伪证据。

### T04-016 接入 API、SSE 与断线恢复

- [x] VERIFIED
- 需求：REQ-AGT-003、REQ-UX-003
- 依赖：T04-011、T04-013、T04-014
- 产物：/v1/workflow-runs 创建/查询/resume/cancel/events；快照+cursor 的一致读取。
- 验收：test_sse_resume.py 验证跨断线补读、event_id 去重、CURSOR_EXPIRED、未授权 run、快照/订阅间隙事件不丢。

### T04-017 保持旧历史与开关可回退

- [x] VERIFIED
- 需求：REQ-AGT-003、REQ-OPS-006
- 依赖：T04-005、T04-008、T04-016
- 产物：版本化 reader、旧 analysis 路由兼容、独立功能开关和回退说明。
- 验收：test_history_compatibility.py 读旧 fixture 不调用外部能力；关闭新功能仍可查询已提交状态和产物；旧默认开关保持含义。

### T04-018 完成 Harness 故障与真实闭环验收

- [ ] BLOCKED
- 需求：REQ-AGT-001、REQ-AGT-002、REQ-AGT-003、REQ-AGT-004、REQ-AGT-005、REQ-AGT-006
- 依赖：T04-003、T04-006、T04-007、T04-009、T04-010、T04-011、T04-012、T04-013、T04-014、T04-015、T04-016、T04-017
- 产物：A04-01～17 证据、真实 Flash 运行记录、错误/费用/延迟报告和未通过项。
- 验收：tests/spec_004 全部适用测试及 live_provider gate；无能力的模式明确 BLOCKED，不伪装成功、不据此阻塞独立 M1。
