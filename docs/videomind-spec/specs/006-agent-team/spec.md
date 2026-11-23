# 006 — 同模型的持久 Agent Team

状态：TODO。仅在 M5 实施，前提是单 Agent 与 M1/M4 剪辑链已可用。Team 是可关闭的增强，不是第一条剪辑链的基础依赖。

## 1. 问题与范围

长视频任务可把不同素材段检索、局部复看、编辑提案和质量检查分配到隔离上下文，提高并行度并减少无关上下文。是否改善质量、费用或延迟必须实测，不能因成员数增加就宣称更强。

所有成员与协调器使用同一已验证 resolved Flash 模型。角色不同体现在任务、可见上下文、允许工具与输出 schema；不通过换模型获得能力。专用 ASR/embedding/FFmpeg 仍是外部确定性或专门模型服务，不称作 Flash 成员。

| 需求 | 行为 |
|---|---|
| REQ-TEAM-001 | 四种合同角色 COORDINATOR、MATERIAL、EDITOR、REVIEWER；有界依赖图与明确产物；同一 Flash |
| REQ-TEAM-002 | MySQL TaskBoard/Mailbox、租约、owner、fencing_token、期限、幂等结果/消息与重启恢复 |
| REQ-TEAM-003 | 成员共享受限证据、分配同一预算；仅协调器提交 timeline/render，编辑服务仍强制 CAS |

关联 REQ-AGT-002～006、REQ-EDT-003、REQ-EDT-006、REQ-OPS-001、REQ-OPS-002、REQ-OPS-005。工具权限与 wire 字段以 contracts 为唯一来源；不另定义第五种角色，也不把可见讨论流当真实数据库。

## 2. 角色与完成条件

| 角色 | 输入 | 允许产物/工具 | 不允许 |
|---|---|---|---|
| COORDINATOR | 用户意图、Brief、当前版本、团队结果与总预算 | 分解有界任务、整合证据、合法 patch、render 提交/取消、最终状态 | 绕过 CAS、擅自提升素材范围或预算 |
| MATERIAL | 指定源版本/区间、查询、覆盖要求 | 检索、概览/局部查看、语音检查，输出 evidence/artifact refs 与缺口 | 提交 timeline/render、把候选当核实 |
| EDITOR | Brief、固定 timeline、已授权候选证据 | clip/patch proposal artifact、时长/顺序解释 | 直接 edit.apply_patch、伪造 clip/evidence ID |
| REVIEWER | proposal 或真实成片、固定 Brief 与证据 | 结构/语义审查报告、问题位置与置信/未核实范围 | 改写已提交版本、把审美偏好冒充硬约束失败 |

每张任务卡有明确输入版本、scope、完成条件、预算、deadline、依赖与交付 schema。大对象用 artifact 引用，消息摘要限长；不把所有成员的完整聊天复制给其他成员。初期最大并发成员数由配置限制，建议从 2～3 个工作成员起步；该值写入 run，不能让模型无限扩容或递归派生。

## 3. TaskBoard 状态与租约

TeamTask 仅有 READY、CLAIMED、RUNNING、BLOCKED、SUCCEEDED、FAILED、CANCELLED。

- 无未满足依赖的卡为 READY；等待依赖的卡为 BLOCKED。
- Worker 以 SQL 原子条件 claim READY，写 claimed_by、lease_until、fencing_token，attempt 递增。
- 开始实际执行后为 RUNNING；心跳以服务器时间延租，必须带 owner 和 token。
- 结果提交在同一事务验证 owner/token、lease 未过期、父 run 未取消/被替换、输入版本仍适用，再写成功/失败结果和 outbox。
- 运行中租约过期，reaper 根据剩余 attempt/预算/期限重置为 READY 或终结 FAILED；旧 Worker 即使稍后完成也不能写进度、状态或业务结果。
- 已完成/失败/取消的历史不改写。重试物理 attempt 保留记录；达到最终失败的卡若需重新规划，创建新 task，关联原失败卡。
- 依赖全部成功且输出可解析时 BLOCKED → READY。依赖终结失败时按该卡已冻结的必要/可选策略失败或取消/改计划，不能无期限停在 BLOCKED。
- 不允许跨 run/workspace 依赖或环。任务图更新需服务端验证并有版本；模型只提交拆解提案。

claim、续租与完成必须使用同一 authoritative DB 时钟/条件，Redis TTL 只可用于调度提示。一个 Redis 锁消失不是租约合法转移的充分证据。

## 4. Mailbox 与重复投递

MailboxEvent 的 message_id、sender_task_id、recipient_task_id、run_id、sequence、类型、artifact_ids、摘要与 created_at 持久化。类型限 RESULT、QUESTION、DEPENDENCY_READY、CANCEL、REVIEW。发送/接收身份由服务从任务上下文解析，模型不能冒充其他成员。

内部消息投递记录另保存 expires_at、消费状态、attempt 与 recipient inbox key；这些是持久实现字段，不擅自添加到现有公开响应。sequence 在选定 mailbox 流内由服务生成并单调递增。事件传输至少一次，接收者按 message_id durable 去重；处理结果/副作用 receipt 落库后才 ack。

TTL 超期意味着“此消息不再驱动任务”，不是删除历史。对于必要依赖，超期使协调器重查 authoritative TaskBoard，重新发出有效通知或按 deadline 失败；通知丢失不应让一个已成功依赖永久卡住。已取消/已替换 run 的消息只作为历史，不触发工作。

## 5. 单写者、版本与预算

只有 COORDINATOR 可调用 edit.apply_patch、render.submit 和 jobs.cancel。EDITOR 的输出是 proposal artifact；其创建不等于时间线已生效。所有提案携带固定输入 revision/证据版本；协调器读取当前 head 后，检查 proposal 是否仍适用，再提交 expected_revision 和稳定 idempotency key。过期提案不能自动重基或偷偷覆盖用户修改。

协调器写权可在崩溃后由新 owner 接管，但仍受 project write lease/fencing 和 EditingService CAS 限制。单写者是调度规则，数据库 CAS 是最终一致性规则。没有“多个成员共写 JSON 然后最后一个赢”。

同一 run 的预算由中心 BudgetLedger 原子预留；成员只能使用分配的上限。并行任务不能各自拿一份完整总预算。超时或旧租约调用已产生成本，仍记到账本；unknown usage 不能假设免费。父 run 取消/被替换后停止新预留，并向已有子任务/独占 job 传播取消。

## 6. 任务图应按阶段构造

避免预览依赖 QA、QA 又依赖尚不存在的预览造成循环。先构造检索/候选 → 编辑提案 → 提案检查的有限图；协调器经 CAS 提交并生成真实预览后，再创建输出 QA 阶段的任务。输出 QA 若要求修改，创建新的有界修正阶段和新 revision，不修改旧 task/结果或创建回边。

每个阶段有明确最大修正轮数和剩余预算。不能以不断生成新 task ID 绕过同一 run 的循环/成本上限。非必要材料任务失败时可形成有说明的部分结果，但不得把“全收录/全部验证”改成不真实的成功。

## 7. Given / When / Then 验收

| ID | Given / When / Then |
|---|---|
| A06-01 | Given 同一 READY task 有两个 Worker 竞争；When claim；Then 只有一个 owner/token 成功，另一个没有执行权限 |
| A06-02 | Given Worker A 失联后租约过期且 B 已接管；When A 提交旧结果/续租；Then 旧 token 全部被拒绝，B 的有效状态不被覆盖 |
| A06-03 | Given 任务结果已持久化但消息 ack 丢失；When RESULT 重投；Then durable inbox 返回已有消费结果，不重复推进或提交 patch |
| A06-04 | Given 依赖成功但通知丢失或 TTL 到期；When调度器对账；Then从 TaskBoard 发现已满足依赖，合法卡进入 READY，或明确超 deadline 失败，不永久挂起 |
| A06-05 | Given 图提案含环或异 workspace/run 依赖；When创建阶段；Then 整体拒绝，无部分卡调度 |
| A06-06 | Given EDITOR 调用 edit.apply_patch 或伪装 COORDINATOR；When执行服务鉴权；Then拒绝，无 timeline 副作用 |
| A06-07 | Given 用户更新了 timeline，旧 proposal 才完成；When协调器提交；Then CAS 拒绝或重新规划，旧提案不覆盖新版本 |
| A06-08 | Given 三个成员同时请求最后一份预算；When预留；Then总预留受同一上限控制，失败请求不发模型调用，实际 usage 全部可追溯 |
| A06-09 | Given 父 run 取消/被替换；When成员仍计算并消息到达；Then停止新任务/预留，传播取消，晚到结果不触发新编辑或恢复父 run |
| A06-10 | Given 调度器与 Worker 全部重启且 Redis 清空；When恢复；Then从 MySQL TaskBoard/Mailbox/ledger 恢复，不丢成功结果、不重复可见产物 |
| A06-11 | Given 同一真实剪辑任务分别走单 Agent 与 Team；When实测；Then两者使用相同 Flash/素材/验收规则，报告质量、费用、延迟和失败，不预设 Team 胜出 |
| A06-12 | Given Team 开关关闭；When用户执行原本可用的单 Agent 或 M1 剪辑；Then完成路径仍可用；查看已有 Team 历史保持只读可用 |
| A06-13 | Given 成员输入中含恶意媒体文字或过期证据 ID；When读取/提案；Then scope 和版本检查生效，媒体内容不能修改角色、工具注册表或模型配置 |
| A06-14 | Given QA 要求修正预览；When继续；Then创建新阶段和新 revision，修正次数计入同一 run 上限，DAG 无回边，旧任务结果保持不变 |

## 8. 发布边界

M5 gate 包含真实多 Worker、故障注入与同一 Flash 的实际协作。仅通过 in-memory agent 对话、日志中出现多个角色名或 README 宣传不算实现 Team。若没有可测收益，保持单 Agent 为默认；功能完成与默认开启是两个独立决策。
