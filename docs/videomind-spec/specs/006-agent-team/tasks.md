# 006 — 可执行任务清单

以下状态对应本地实现与验收记录。

### T06-001 固定同模型角色和阶段模板

- [x] VERIFIED
- 需求：REQ-TEAM-001
- 依赖：T00-001、T00-003、T04-018、T05-021
- 产物：四角色输入/输出 schema、registry 权限、同一 Flash 指纹、最大并发/节点/修正轮数配置、有限阶段模板。
- 验收：静态合同测试角色不增生，所有通用模型配置一致；MATERIAL/EDITOR/REVIEWER 无 timeline/render 写权限；不依赖新的 Agent 平台。

### T06-002 建立 TaskBoard、attempt 与 Mailbox 存储

- [x] VERIFIED
- 需求：REQ-TEAM-002、REQ-OPS-001
- 依赖：T06-001、T00-004
- 产物：任务/依赖/attempt/mailbox/delivery/inbox 迁移、唯一键、内部 deadline/TTL/预算引用，公开 schema 适配。
- 验收：真实数据库 migration/rollback 测试；所有对象有可验证 scope；公开 additionalProperties=false 合同不被内部字段污染。

### T06-003 验证 DAG 与依赖推进

- [x] VERIFIED
- 需求：REQ-TEAM-001、REQ-TEAM-002
- 依赖：T06-002
- 产物：环检测、同 run/workspace 依赖、图版本、READY/BLOCKED 转换和依赖终结策略。
- 验收：test_taskboard.py 拒绝环/异 scope/非法节点；依赖完成可推进，必要失败不能永久 BLOCKED；阶段重规划不加回边。

### T06-004 实现排他 claim 与 fencing

- [x] VERIFIED
- 需求：REQ-TEAM-002
- 依赖：T06-003
- 产物：SQL 原子 claim、owner/lease/token/attempt 更新，完成/进度条件写入。
- 验收：多个真实 Worker 竞争同卡只有一个获权；旧 token 的完成、进度和续租均拒绝；不以进程内锁证明正确。

### T06-005 实现心跳、过期回收与有界重分配

- [x] VERIFIED
- 需求：REQ-TEAM-002、REQ-AGT-005
- 依赖：T06-004
- 产物：DB 时间 heartbeat、reaper、attempt/deadline/预算判定、失联后续接工具 receipt。
- 验收：test_team_recovery.py 停心跳后新 owner 接管、旧 owner 返回不覆盖；超过期限/重试上限终结且可诊断。

### T06-006 实现持久 Mailbox 与 inbox 去重

- [x] VERIFIED
- 需求：REQ-TEAM-002
- 依赖：T06-002、T04-010
- 产物：服务端 sender/recipient、单调 sequence、message_id、TTL、outbox 投递、ack 后置及 durable inbox。
- 验收：test_mailbox.py 重复消息、乱序、ack 丢失、过期 RESULT/CANCEL、跨 run 冒充均符合规格；摘要不泄露完整内部 prompts。

### T06-007 原子提交任务结果与通知

- [x] VERIFIED
- 需求：REQ-TEAM-002、REQ-AGT-004
- 依赖：T06-004、T06-006
- 产物：结果 schema/引用验证、artifact 已完成检查、task 终态+mailbox+outbox 原子事务。
- 验收：在 artifact 完成后/SQL提交前、SQL提交后/ack前断进程；恢复只有一份逻辑结果，未完成 artifact 不可成功引用。

### T06-008 接入共享预算与上下文隔离

- [x] VERIFIED
- 需求：REQ-TEAM-003、REQ-AGT-005、REQ-AGT-006
- 依赖：T06-001、T06-004、T04-012、T04-015
- 产物：task reservation、同 run 累计成本、按角色/任务组装上下文与受限引用。
- 验收：test_team_budget_cancel.py 并发预留不超额、重试/unknown usage 不归零；成员看不到未授权素材或其他成员完整上下文。

### T06-009 强制单写时间线与提案版本绑定

- [x] VERIFIED
- 需求：REQ-TEAM-003、REQ-EDT-003、REQ-EDT-006
- 依赖：T06-007、T04-007、T05-004
- 产物：EDITOR proposal artifact、COORDINATOR 有效写 lease、base revision 校验、稳定幂等 key 和 receipt 续接。
- 验收：test_single_writer.py 拒绝伪造 role；双协调器/用户并发 patch 只有合法 CAS 成功；过期 proposal 不自动重基。

### T06-010 编排有界阶段与真实结果聚合

- [x] VERIFIED
- 需求：REQ-TEAM-001、REQ-TEAM-003
- 依赖：T06-003、T06-007、T06-008、T06-009
- 产物：材料→提案→提交/预览→输出 QA 的分阶段协调器，结果来源/缺口聚合与最大修正轮数。
- 验收：受控 fixture 检查无循环 DAG、所有派生阶段计同一预算、必要成员失败不伪造全覆盖；最终输出关联实际 revision/artifact。

### T06-011 传播父取消与用户 steering

- [x] VERIFIED
- 需求：REQ-TEAM-002、REQ-TEAM-003、REQ-AGT-005
- 依赖：T06-010、T04-013、T04-014
- 产物：停止 claim/预留、子任务取消、共享 job 退订、旧消息/结果失效、SUPERSEDED 处理。
- 验收：test_team_budget_cancel.py 在各阶段取消/改目标；晚到成员不能提交新编辑或恢复父 run，最终状态与剩余任务可查。

### T06-012 实现调度对账与整组恢复

- [x] VERIFIED
- 需求：REQ-TEAM-002、REQ-AGT-003
- 依赖：T06-005、T06-006、T06-010、T06-011
- 产物：队列/通知丢失后的依赖扫描、coordinator checkpoint 恢复、TTL 到期处置和历史只读读取。
- 验收：test_team_recovery.py 清空 Redis、重启全部 Worker/dispatcher 后从 SQL 恢复；已成功结果不丢，失效通知不造成永久挂起。

### T06-013 暴露任务板、事件与追踪

- [x] VERIFIED
- 需求：REQ-TEAM-002、REQ-OPS-002、REQ-UX-003
- 依赖：T06-007、T06-010
- 产物：/v1/workflow-runs/{run_id}/team-tasks 与 mailbox-events 分页、最小 UI、task/attempt/token/tool/revision trace。
- 验收：合同/权限/cursor 测试；浏览器刷新/断线能恢复状态；只展示允许的摘要和产物，不暴露内部完整 prompts 或秘密。

### T06-014 验证默认关闭与单 Agent 回退

- [x] VERIFIED
- 需求：REQ-TEAM-001、REQ-TEAM-003
- 依赖：T06-010、T06-012
- 产物：feature flag、drain/cancel 规则、旧 Team 历史兼容与单 Agent 路径保留。
- 验收：关闭 Team 后 T05-013 的 M1 和既有单 Agent 路径仍可运行；已提交历史仍能查看，不用删除团队数据回退。

### T06-015 完成真实 Team 与故障验收

- [ ] BLOCKED
- 需求：REQ-TEAM-001、REQ-TEAM-002、REQ-TEAM-003、REQ-OPS-005
- 依赖：T06-001、T06-002、T06-003、T06-004、T06-005、T06-006、T06-007、T06-008、T06-009、T06-010、T06-011、T06-012、T06-013、T06-014
- 产物：A06-01～14 结果、同 Flash 单 Agent/Team 对比、真实多 Worker 故障记录与适用范围。
- 验收：tests/spec_006 与 test_live_team.py -m live_provider；固定素材/预算/模型/质量门槛，报告实际质量/费用/延迟；缺能力 BLOCKED，无收益不默认启用。
