# 003 — 多模态检索、截图搜索与证据定位规格

状态：TODO；目标里程碑：M3。输入为 002 发布的多模态 IndexGeneration 与 001 的原片/覆盖/抽帧能力；输出为可复看的候选、可引用 Evidence 和查询级覆盖状态，供问答与 M4 剪辑使用。

## 1. 用户目标

用户可以用中文、英文或截图查找素材中的画面、台词和动作；点开结果跳到真实源时间，继续要求“看前后几秒确认一下”。用户问“第一次”“全部”“一共几次”时，系统必须检查查询范围是否按相应协议核查完成，不能把 topK 搜索结果数量当总数。

## 2. 需求与范围

| 需求 | 首版行为 |
|---|---|
| REQ-RET-001 | 文字查询召回、原片时间跳转、候选与已验证结果区分 |
| REQ-RET-002 | 上传截图/ROI、同空间图像检索、重复来源与局部定位 |
| REQ-RET-003 | 授权过滤先于 topK、多路召回、RRF、重排与来源解释 |
| REQ-RET-004 | 原片局部回看、连续帧核验与边界细化 |
| REQ-RET-005 | FIRST/ALL/COUNT 的范围、验证覆盖与实例定义 |
| REQ-RET-006 | 证据不足、模态冲突、未核查范围与不确定结论 |
| REQ-OPS-001 / REQ-OPS-002 / REQ-OPS-005 | 隔离、可观测成本与版本化评测 |

不实现全片逐帧发送给一个聊天上下文，不将换一个更大通用模型作为失败兜底。普通推理、视觉复核、问题规划均调用同一 DeepSeek Flash。专用 reranker 可独立选型；若未部署，允许 RRF 加 Flash 复核，不得假称已有专用重排模型。

## 3. 请求、范围与结果语义

公开 SearchRequest 使用统一合同的 scope、text、image_artifact_id、query_mode、top_k、可选 range。至少有一个非空文本或有效截图。截图 artifact 必须属于请求者有权限的 workspace/project，并经图片类型/像素限制检查；服务端不直接抓取用户随意提供的 URL。

scope.project_id 和 media_revision_ids 是用户选择，不是身份声明。服务端将其与授权集合求合法范围；未授权 ID 按统一错误策略拒绝，不能让模型扩展范围。未指定媒体列表时使用请求开始时该项目可见媒体的固定快照。range 是每个目标媒体的源时间区间，不能被理解成多素材拼接后的输出时间线。

query_mode 为 LOCAL、FIRST、ALL、COUNT。跨多个媒体的 FIRST 必须有用户认可的顺序；首版若未提供顺序，要求缩小到单媒体或进入澄清 workflow，不能按数据库插入时间猜“最早”。ALL/COUNT 在多个媒体上明确计数单位和重复素材策略。

SearchHit.score 是排序分数，不是事实置信概率；route_ranks 解释来自哪些召回路。verified=false 表示候选，不可直接当作确认事件；verified=true 需有相应 Evidence 与验证协议。

SearchResult.coverage_summary.status 为 UNASSESSED、PARTIAL、PROTOCOL_COMPLETE；同一 summary 必须带 protocol_id、query_run_id、requested_ranges、verified_ranges、unverified_ranges，semantic_exhaustiveness_guaranteed 固定为 false。PROTOCOL_COMPLETE 表示已按这次查询协议完成所需范围核查，不等于数学证明无漏检；普通概览 MODEL_OBSERVED 不能直接转成该状态。top_k 只控制展示候选数，不定义搜索范围、计数或完整清单。

/search 始终同步返回候选，不执行全局验证。FIRST/ALL/COUNT 返回 verification_required=true，由持久 WorkflowRun 完成验证；query_id 是本次检索 ID，query_run_id 是验证 WorkflowRun ID，不能混用。truncated 表示本次候选展示被截断，candidate_count 只是候选数；verification_workflow_id 和 result_artifact_id 没有对应对象时为 null，不制造占位 ID。

完整实例清单、查询定义、固定范围、generation、证据与未核查区间存入受控结果 Artifact。最终 AnswerResult 通过 found_event_count、count_qualifier（OBSERVED_ONLY/WITHIN_PROTOCOL）和 complete_event_list_artifact_id 表达已发现数量与协议限定；不能只把完整清单放在聊天文本中，不能以 candidate_count 代替次数。

## 4. 功能验收场景

### RET-S01 文字检索与可跳转证据

**Given** 已发布的 generation 中有 ASR、OCR、caption 与图像记录。

**When** 用户搜索“红衣人把杯子递给另一个人”。

**Then** 返回授权范围内的相关候选、来源与源时间；动作先后需要连续帧回看才能 verified。点击结果跳到与 Evidence 绑定的原片位置，不以摘要中的估计时间替代。

### RET-S02 授权先于 topK

**Given** 其他租户存在更相似的素材，当前租户也有较低相似度正确结果。

**When** 查询任何词法、文本向量、图像向量或可选片段路。

**Then** 在各路 topK 前施加服务端 workspace/project/media/generation 过滤，仍可取到本租户正确结果；不能全库 topK 后才删越权记录，也不能在 debug 信息中泄露越权 ID/分数。

### RET-S03 同一 generation

**Given** 查询开始时绑定 generation A，后台同时发布 B。

**When** 并行召回、重排和复看完成。

**Then** 本次候选与已索引证据均按 A 的 manifest 解释；局部新增证据有明确 query-run 来源，不伪装成 A 已有内容。不在同一查询中混用 A 的文本与 B 的图像向量。

### RET-S04 截图搜索

**Given** 用户上传一张经过裁切、缩放或压缩的截图。

**When** 截图以帧索引同版本 encoder 编码并检索。

**Then** 图像路真实参与召回，返回实际帧/出现区间及原片可跳转位置。关闭 OCR 辅助路后仍能验证图图检索；图像能力未配置时明确不可用，不以文字检索冒充。

### RET-S05 重复画面与无匹配截图

**Given** 同一画面在片头、回放、片尾出现多次，或另一素材只有相似但不同的画面。

**When** 用户截图搜索。

**Then** 保留不同源出现实例，显示不唯一；近重复或不同来源的相似画面需要复核。不因 top1 存在就强制认定截图来源，无充分支持时返回未确认或无已确认匹配。

### RET-S06 中文与模态能力路由

**Given** 某图文 encoder 的中文文本对齐能力未验证，但 BGE-M3 文字路径可用。

**When** 用户发出中文查询。

**Then** 按已验证能力路由到文本和可用视觉路径；若需要查询翻译，保留原文、规范化结果和翻译版本。不得静默声明所有 encoder 都支持中文图文检索。

### RET-S07 融合去重与解释

**Given** 同一声音片段被 ASR 文本、caption 摘要和重叠 chunk 多次命中。

**When** RRF 融合和候选归并。

**Then** 保留多路来源，但同一原始证据不会因复制次数无限加分，也不会变成三次事件。结果可查看每路 rank/raw score、generation 和所用模型版本。

### RET-S08 动作顺序与局部复看

**Given** 单帧候选同时显示人、杯子和桌子，但并不能区分拿起还是放下。

**When** 查询要求“先拿起杯子再转身”。

**Then** 回看前后有序帧并附 actual_us；若动作顺序仍不可判断，返回 UNCERTAIN，不从单帧相似或多帧均值向量推导确定顺序。

### RET-S09 边界细化

**Given** 召回到一个 10 秒候选，目标动作只发生在其中一部分。

**When** 启动局部复看。

**Then** 在候选前后上下文和边界附近有界加密采样，返回有证据支持的源区间及精度限制；应用验证区间有效且属于正确原片。模型猜出的任意小数秒不能冒充帧精确边界。

### RET-S10 首次发生

**Given** topK 最早命中在 45 分钟，而前 30 分钟只做过稀疏概览。

**When** 用户问“第一次发生在什么时候”。

**Then** 不能直接称 45 分钟为首次；对必要前缀按该查询协议核查。核查预算不足时回答“目前发现最早的一次”，列未核查前缀；若前缀存在未解决候选，也不能宣称已确认首次。

### RET-S11 全部结果与分页

**Given** 全片有超过 top_k 的相关实例。

**When** 用户选择 ALL。

**Then** top_k 仅决定当前展示页；完整扫描、实例列表和覆盖清单独立存储并可继续查看。PROTOCOL_COMPLETE 不等于这一页展示了全部实例；不能截断后输出“所有片段如下”。

### RET-S12 计数与跨块重复

**Given** 一次事件横跨两个重叠 chunk，之后又真实发生两次。

**When** 用户问 COUNT，且协议范围已完成。

**Then** 根据明确实例定义得出三个发生实例，每个实例有证据与去重依据；不以 hit 数或 chunk 数求和。若有未核查区间或身份/实例归并不确定，则输出已确认下界与限制，不能给无条件精确总数。

### RET-S13 不存在与缺证据

**Given** 检索没有找到目标，但相关画面路失败或存在未观察区间。

**When** 生成回答。

**Then** 说明“在已核查范围内未发现”，并列缺失范围/能力；不能断言从未发生。对完整协议也要说明观测粒度与适用条件。

### RET-S14 查询级覆盖不能互借

**Given** 已对“红衣人出现”完成范围验证。

**When** 新问题是“红衣人先拿杯子再转身”。

**Then** 不直接复用 QUERY_VERIFIED；只有范围、规范意图、实例定义、验证协议和版本均兼容时才可复用必要证据。相同词语不是相同验证任务。

### RET-S15 取消、恢复与费用上限

**Given** 一个全片查询正在执行大量局部核查。

**When** 用户取消或达到帧/token/时间预算。

**Then** 持久保存已核查实例与未完成区间，停止领取任务并禁止晚到结果改写最终状态；非终态 worker 崩溃可恢复，已取消或终止的查询若要补查则创建关联的新运行并复用兼容证据。预算停止后不能把部分扫描升级为 PROTOCOL_COMPLETE。

### RET-S16 权限变化和证据冲突

**Given** 候选生成后某素材被删除或权限撤销，另一个候选存在 OCR/ASR 冲突。

**When** 返回结果或打开 Evidence。

**Then** 再次鉴权并移除/拒绝已不可见资源；冲突证据展示来源和不确定性，不由排序分数决定谁“说了算”。旧 artifact 链接不能绕过权限检查。

## 5. 全局查询的定义约束

ALL/COUNT 需固定目标范围快照、动作/对象条件、计数单位、短事件可观察性假设和去重规则。“进入画面次数”“持续出现片段数”“独立人物数”不是同一任务。定义影响结果且无法从上下文决定时，进入 AWAITING_INPUT；已明确的定义不重复要求用户确认。

同一个 media_revision 被多个素材入口引用时不重复计数；不同 revision 含相同 bytes 或重复拍摄内容时，必须使用已记录的“按素材计/按源发生实例去重”政策。不能未经说明自行把副歌、回放或重复上传合成一次。

查询覆盖的完整性检查由服务端执行，不由 Flash 输出的 complete=true 决定。协议完成后可表述“按本次核查协议共确认 N 次”，仍应提供协议粒度与来源，不使用绝对无漏保证。

## 6. 质量门槛与完成定义

使用 24 条、总长≥12小时、至少6条≥1小时（其中2条≥2小时）的固定盲测集，共320条查询：200条文字、60条截图、60条全局。另建独立开发集，不能反复用盲测调参。目标：文本候选 Recall@20≥0.90，截图 Recall@20≥0.85，最终 Grounding R@5@tIoU0.5≥0.80，主张支持 precision≥0.95，符合条件事件边界误差 P95≤3秒；覆盖越权结论与跨租户泄漏均为0。定义与计算见 plan。

所有 RET 场景有可复现记录；真实截图→候选→原片复看→Evidence→播放器跳转闭环通过；FIRST/ALL/COUNT 的持久任务与部分覆盖已测试；实际 usage、query trace、错误与删除/发布竞态可追溯。未通过真实 encoder 或 Flash 的验收项不允许用 mock 标记通过。
