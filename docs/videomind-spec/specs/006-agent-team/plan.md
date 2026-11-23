# 006 — 实现计划

状态：TODO。目标是把可恢复任务组织在现有应用与队列内，不引入新的自治平台或让外部 Agent 框架成为第二套权威状态。

## 1. 接入现有 VideoMind 的方式

现有 application/agent.py 是单分析任务的 Planner/Executor/Critic 流程，domain/agent.py 的自然语言计划不是依赖 DAG。现有 task lease、Celery 至少一次执行、checkpoint、execution record、conversation memory 可提供基础，但不等于 TaskBoard/Mailbox 或共享 timeline 写入协议。

复用 004 的 Flash Gateway、typed tool policy、WorkflowRun、BudgetLedger、outbox/inbox；复用 005 的 EditOperation/CAS/RenderService。新增 TeamCoordinator、TaskBoardService、MailboxService ports 及 SQL/worker adapters。路径需按真实 checkout 分层确定，不把领域调度塞进 provider adapter。

四个角色使用同一 provider 配置指纹。工具注册表是角色能力来源；MATERIAL/EDITOR/REVIEWER 无 timeline/render 写权。局部查看等 ARTIFACT_WRITE 可由成员使用，但仍经过业务幂等与预算。

## 2. 持久模型

公开 TeamTask/MailboxEvent 字段遵守 domain.schema.json。内部存储补充可恢复运行所需字段：

| 内部字段/表 | 目的 |
|---|---|
| task_spec 与 input digest | 固定 scope、Brief/revision/evidence 版本、完成条件、输出 schema |
| task deadline、最大 attempt、依赖策略、task graph version | 确定过期/失败行为，防止无限重分配 |
| attempt ledger | 保留每次 owner/token、模型用量、错误、取消与输出引用 |
| budget reservation refs | 跨成员共享 004 预算，不能各自重置 |
| lease owner/fencing/expiry | 与公开 TeamTask 对齐，所有提交受 fencing |
| mailbox delivery/inbox | expires_at、消费者/消息唯一键、ack 时间、重试与错误 |
| stage/controller checkpoint | 恢复阶段图、用户 steering、待聚合结果和写入责任 |

以上内部字段需迁移、约束和版本读写；不直接添加到公开 JSON 的 additionalProperties=false 对象。长内容存 Artifact；TaskBoard/Mailbox 只存有限摘要和引用。删除派生内容遵循父项目保留策略，不能先删 task 仍引用的结果。

## 3. 有界任务图与调度

Coordinator 在 scope、budget 与阶段模板内提出任务分解，服务验证 DAG、角色、输入归属、最大节点数、并发上限与 deadline。Task ID 由服务生成。将角色的自然语言描述映射到受限输入输出 schema；不允许每张任务卡临时定义任意工具。

初期模板可以是：

- 检索阶段：最多若干并行 MATERIAL 卡负责不同已分配区间或独立查询，输出证据/覆盖缺口。
- 提案阶段：EDITOR 依赖材料结果，生成 patch proposal；REVIEWER 读取 proposal 与源证据作结构/语义意见。
- 提交阶段：COORDINATOR 聚合，经 EditingService 提交与 RenderService 产生预览。
- 输出检查阶段：新的 REVIEWER 卡读取实际输出；必要修改创建下一阶段，受 run 修正次数上限约束。

依赖图在阶段内无环。协调器可以有多张不同阶段任务卡，但同项目同一时刻的 timeline 写责任只有一个有效 lease/token；所有实际修改还要 CAS。不可用“协调器等待子任务、子任务又等待协调器成功”的互相依赖图表示调度控制。

READY 卡只在父 run 可运行、依赖已满足、预算可分配、deadline 未过时可 claim。BLOCKED 的原因在内部明确记录，定期对账可推进或终结；禁止依赖 Redis 消息恰好抵达才唤醒。

## 4. 排他 claim、租约与完成

使用 MySQL 条件更新或 SELECT FOR UPDATE SKIP LOCKED，根据实际版本选择并测试；不能仅用 Python 锁。claim 在事务中检查 READY 和依赖状态，分配 owner UUID、fencing_token+1、attempt+1 与 lease_until。

heartbeat WHERE task_id/claimed_by/fencing_token/status 匹配，并检查父 run 未终结。时钟以数据库为准；租约长度与心跳间隔在部署配置中成比例约束并记录，例如心跳必须显著短于租约，不能依赖每个 Worker 自己的时区。

Worker 完成时，在同事务验证 token/lease/run/version，把结果 schema、引用和终态写入，再插入 RESULT/REVIEW mailbox/outbox。若输出 artifact 尚未完成上传，不得先提交成功。外部上传可能重复，按内容 digest/attempt manifest 对账；不宣称物理计算 exactly-once。

Reaper 对过期 CLAIMED/RUNNING 卡检查 attempt、绝对 deadline、预算和取消条件。可继续则重置 READY 并使旧 token 无效；不能继续则写 FAILED/CANCELLED 与原因。FAILED 是最终结果，新的规划创建新卡。新 Worker 恢复已有 ToolExecution/业务 receipt，避免再次创建 timeline/render。

## 5. Mailbox 协议

sender/recipient 均引用真实 TeamTask；协调器也拥有可寻址的任务卡。写消息前验证同 run/scope、允许消息类型和关联任务状态。sequence 在同 run 的 mailbox 事件流单调生成；message_id 全局 UUID。摘要限 2000 字段长度内，payload 用受控 artifact refs。

消息创建和其引起的任务结果/状态变更使用事务 outbox。消费者以 (recipient_task_id,message_id) 唯一 inbox 去重，处理完成和 ack 在可恢复事务中落库。对外消息只能展示经过脱敏的摘要、状态和产物引用，不暴露内部完整 prompts、秘密或隐藏推理。

expires_at 是内部投递期限。超过期限不执行消息内容；协调器读取 authoritative task 状态决定重新通知或失败。不能让过期 RESULT 抹掉已成功的源任务；同样不能让迟到 CANCEL 撤回已确定成功的产物。所有消息驱动行为都重新检查父 run 和当前 token/state_version。

## 6. 单写者与版本协调

只有 COORDINATOR 的受信执行上下文允许 timeline/render 写工具；role 不从模型参数读取。EDITOR proposal 固定 base_revision_id/revision_no、输入 evidence IDs、候选 patch 和理由。协调器解析原始证据，核对当前 head，再调用 005 服务。

同一个 proposal 的重放使用稳定 operation idempotency key；新内容或新的基线必须新 operation。冲突由 409 返回，需要重新读取并生成新 proposal；禁止自动把旧 expected_revision 替换成 head 后直接重试。

来自 Team 的写入在可信执行上下文携带 task owner/fencing_token；EditingService/RenderService 在业务提交边界再次验证有效写租约和父 run 状态，不能只在模型调用开始时校验。否则旧 Worker 虽然不能写 TaskBoard 结果，仍可能偷偷提交外部副作用。普通授权用户的显式编辑走用户权限，不伪造协调器任务身份。

协调器失联时接管者先从 WorkflowRun/ToolExecution/operation receipt 重建已提交动作，再续接；不能只依据聊天中的“还没完成”重做。多个协调器实例就算同时运行，fencing 和 CAS 仍保护写入；角色单写规则不承担最终并发正确性。

## 7. 预算与取消

每个 task 分配 run 的 reservation 子额度；并行模型调用在中心账本预留。TaskBoard 的卡数、最大深度、修正阶段数与每成员调用上限均绑定同一 run policy，创建新卡不能重置预算。专用媒体 job 的成本另按 decoder/帧数/时间等计量，仍归属该 run。

父 run CANCELLED/SUPERSEDED 或收到取消请求后：禁止新 claim/预留；未运行卡直接取消；运行卡发 durable CANCEL 并设置取消条件；独占下游 job 协作停止，共享 job 解除订阅；晚到输出在提交检查被挡住。取消应有最大收敛期限和可诊断未终止任务列表，不能只给 UI 一个“取消已发送”。

成员失败策略先冻结：必要材料失败导致阶段失败/请求补充；可选材料失败可继续，但最终记录缺口并限制声明。任何“全片全部事件”任务不能因某成员失败仍假装覆盖完整。

## 8. API、监控与发布

读取 API 为 GET /v1/workflow-runs/{run_id}/team-tasks 与 /mailbox-events，支持 cursor/limit；任务创建、claim、heartbeat、完成通过内部服务/queue，不开放模型可调用的任意成员管理端点。UI 展示角色、任务状态、依赖、费用摘要与产物；业务工作流仍通过现有 /events 推送。

记录 run/task/attempt/token/tool/job/revision 的 trace 关联，观测 claim 竞争、过期 lease、重复 inbox、blocked 卡时长、重试/取消、模型费用与 active/wall latency。比较 Team 与单 Agent 使用同一素材、Flash 版本、预算政策和质量阈值，不以总并发越高越好为指标。

feature flag 默认关闭；通过 M5 gate 后按有收益任务类别开启。关闭后停止新 Team 创建，已运行任务按选定 drain/cancel 策略收敛，旧历史仍可读，单 Agent 和 M1 手动路径保持可用。

## 9. 待创建验证

~~~bash
python -m pytest tests/spec_006/test_taskboard.py tests/spec_006/test_mailbox.py
python -m pytest tests/spec_006/test_team_recovery.py tests/spec_006/test_single_writer.py
python -m pytest tests/spec_006/test_team_budget_cancel.py
python -m pytest tests/spec_006/test_live_team.py -m live_provider
~~~

以上为实施时创建的入口，未运行。集成测试用多个真实 Worker 和 MySQL/queue，不用一个进程假装测试租约。故障覆盖 claim 竞争、心跳停顿、时间偏差、旧 token 返回、消费后 ack 丢失、DB 提交后进程退出、Redis 清空、父取消和 concurrent user patch。真实模型 benchmark 至少保存单 Agent 与 Team 两种路径的产物/费用/延迟/失败清单；无提升也应如实报告。

## 10. 源码依据

- [agent.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/agent.py)：单任务分析闭环与工具恢复，不能直接等同多 Agent。
- [tool_policy.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/tool_policy.py)：已有服务端工具策略，扩展角色仍应由可信上下文控制。
- [task_lease.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/task_lease.py)：租约维护基础。
- [celery_tasks.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/celery_tasks.py)：至少一次重投语义。
- [conversation_memory.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/conversation_memory.py)：有限对话记忆，不是共享 TaskBoard。
