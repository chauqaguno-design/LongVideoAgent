# 003 — 实施计划

## 1. 复用边界与接口

在现有检索应用服务上增加统一 QueryPlan 与 Retriever/Reranker/Verifier 适配，不新建平行的主 Agent。公开操作遵循 searchMedia、getEvidence、getArtifact、createWorkflow/getWorkflow/取消及事件接口；图像上传使用统一合同专用入口。所有具体 HTTP 路径、字段和错误码以 contracts 为准。

内部 QueryPlan 至少记录：服务端授权范围快照、规范化 text/image/ROI、query_mode、实例定义、目标时间范围、selected generation、召回路、模型/encoder/prompt/验证协议版本、预算。它是可恢复的业务记录或 Workflow 产物，不只是临时聊天消息。

/search 同步返回候选，FIRST/ALL/COUNT 的 verification_required=true，不在请求内执行全局扫描。SearchResult 使用 coverage_summary、truncated、candidate_count、verification_workflow_id、result_artifact_id；query_id 只标识一次检索，coverage_summary.query_run_id 标识验证 WorkflowRun。覆盖状态和完整清单分开，warnings 不承载隐藏控制字段。

全局扫描产生不可变结果清单 artifact，记录完整实例、去重关系、覆盖缺口和版本。AnswerResult.found_event_count 来自确认实例集合，count_qualifier=OBSERVED_ONLY 表示目前发现的数量，WITHIN_PROTOCOL 仅在该协议要求范围完成后使用；complete_event_list_artifact_id 指向完整清单。coverage_summary.semantic_exhaustiveness_guaranteed 始终为 false，verified/unverified/requested_ranges 按 media revision 区分。

## 2. 查询规范化与过滤

1. 从身份上下文解析 workspace，校验 project 与每个 media revision 的可读权限。
2. 固定范围快照；未指定媒体时记录此时可见集合，后续新上传素材不偷偷进入同一查询。
3. 验证源 range 在目标媒体内；多媒体使用同一 range 时按各片原时间解释。用户需不同范围时拆成子请求/明确 QueryPlan。
4. 由 Flash 或确定性解析辅助识别视觉对象、动作、台词、时间关系和 LOCAL/FIRST/ALL/COUNT。结构化 query_mode 与用户文本冲突时不能静默改变语义。
5. 选择真实可用的检索路。截图 artifact 验证归属、mime、像素与解码，模型不得传任意 URL 给内部网络抓取。
6. 固定 generation，按 manifest 检查能力与 encoder space descriptor。
7. 在每路搜索请求内先施加 workspace/project/允许媒体/generation/源范围过滤，再 topK。返回前重新鉴权，处理并发撤销。

任何 raw filter、SQL、Qdrant JSON 或身份字段都不由模型直接执行；模型只能填预定义业务条件。ACL 更新晚于向量 payload 时，以权威数据库允许集合限制召回，并在返回时二次确认。

## 3. 多路召回与 RRF

### 3.1 召回路

- 词法：ASR/OCR/标题等适合精确短语，中文分词与原文保持一致。
- 文本向量：BGE-M3 编码用户文字与记忆文本，按 modality 保留来源。
- 图像向量：截图与候选帧/ROI 用同一图像 encoder；文本→图像仅在该空间已验证支持该语言时启用。
- 可选片段向量：动作召回升级；不能替代后续顺序验证。

每路初值 top50，可在独立开发集固定配置；timeout 的路记录失败和影响，不能静默把多路检索显示为全路完成。纯截图请求的图像路不可用时应能力失败，不降级成空文本查询。

### 3.2 融合

使用 RRF：score(c)=Σ_route weight(route)/(60+rank_route(c))；k=60 和初始等权为提案，调整必须版本化并只用开发集。不同模型原始相似度不直接相加。

在每路内部按同一 source evidence/segment 去重，再融合；来自重叠 chunk 的同一事件候选聚合来源但不多算发生次数。候选可按 media revision 与相邻范围成组以便复看，但保留原子候选及原区间，不能把整条视频合成一个候选来提高召回指标。

SearchHit.route_ranks 保存路名、rank、raw_score、model_version。显示说明应为“来自画面相似与字幕命中”等事实，不将排序分数转成“99%确定发生”。

## 4. 截图搜视频

截图进入受控 Artifact，支持用户选 ROI；全图和 ROI 可并行查询但明确各自来源。默认做与索引一致的缩放/归一化，不人为强行去水印或生成未存在的细节。查询截图的 EXIF/旋转处理与帧方向合同一致。

同一帧可能属于连续静止区间；从命中帧向前后检索或复看，形成“该画面出现区间”，而非只给相似度最高的一帧。多个视频或多个时间位置均真实匹配时保留多个出现实例。不同来源的相似构图、翻拍屏幕和封面图需要区分“画面相似”与“同一来源”。

无匹配判定阈值按 encoder 和开发集校准，不跨空间共享固定阈值。低于支持证据要求的 top1 可作为未确认候选显示，但不能确定认领来源。读取截图 OCR 可增加短语召回，但独立消融必须证明图像通道确实存在。

## 5. 重排与局部 Grounding

对 RRF 的有限候选做专用 rerank（若配置）和 Flash 视觉复核。输入包含原片 source range、实际帧时间、相关音频/文本来源与明确问题；只给必要上下文，不把无关整片历史塞进 prompt。所有工具输入、媒体文字与用户目标分开标注。

动作候选默认取前后上下文，使用有序帧和必要的中间采样。模型应回答：哪些条件可见、动作顺序是否成立、边界仍有哪些不确定性、哪些 frame/observation 支持结论。服务端检查 Evidence 源归属、时间与 schema；模型自报完整覆盖无效。

边界细化采用有界策略：

1. 从候选与相邻上下文形成粗包络，不直接裁掉动作准备和结束部分。
2. 根据目标动作所需粒度取连续帧，得到可能发生区间和不确定边界。
3. 对两端的不确定区间加密采样，最多配置内的轮数/帧数/token。
4. 以实际观测分辨率返回 source range；无法进一步区分时保留较宽包络与 quality flag。
5. 将核查结果提交为 query-run Evidence，关联原候选 generation；必要时异步纳入后续索引，不原地修改 ACTIVE generation。

对文字目标改用原图 ROI；对口述目标读取音频与对齐结果，不要求仅凭画面猜台词。模型返回秒数只能是候选，经真实媒体范围与时间合同校验后才能成为可引用微秒区间。

## 6. 全局查询执行器

### 6.1 计划与协议

FIRST/ALL/COUNT 先明确目标范围和实例定义，再生成持久扫描计划。每个 QueryPlan 有固定 query fingerprint，包含意图、约束、媒体快照、范围、实例定义、验证协议、模型和采样版本；只有兼容 fingerprint 才可复用 QUERY_VERIFIED。

协议声明目标观测粒度、所用模态、必要上下文、失败处理和适用事件。初版可在开发集选择连续动作核查的帧间隔与分块；必须承认短于可观察粒度的事件可能漏掉。若某查询需要的密度或模态超出预算/能力，coverage_summary.status=PARTIAL 并列限制，不能只修改协议名称就声称完整。不要给 WorkflowRun 发明 PARTIAL 状态。

### 6.2 调度与覆盖

ALL/COUNT 对整个目标范围公平调度核查窗口，而不是只复查向量 topK。召回提供高优先级候选，但不能删掉低分/未命中的未核查窗口。每个窗口完成相应协议后写 QUERY_VERIFIED；失败或预算停止留下明确 gap。

FIRST 可以按时间从前向后扫描必要前缀；一个候选成为“首次”前，候选之前的目标范围与可能更早的未解决候选必须核查完毕。优化不能仅靠“向量分数太低”跳过前缀。多媒体未定义顺序时不执行猜测排序。

覆盖聚合以区间并集去重，计算协议要求范围减去成功核查范围；实际采样粒度不足的窗口不能因为有一个结果就覆盖整窗。PROTOCOL_COMPLETE 的必要条件：范围无未核查 gap、无阻塞失败、所有实例去重/冲突按协议处理、版本一致。它仍只声明协议执行完整。

### 6.3 实例清单与计数

完整清单包含 occurrence ID、media revision、source range、evidence IDs、归并来源和状态。ID 是服务端生成的实例标识，不由模型文本直接决定。时间连续且证据支持同一发生实例的重叠块归并；真实重复、回放与重复媒体按 QueryPlan 明示政策处理。

精确计数仅从已确认且互不重复的 occurrence 集合计算，不能直接取 len(search hits)。未完成全范围时返回已确认下界，并保留冲突实例；不能把不确定候选既删掉又宣称总数完整。若实例身份是问题本身，例如“有几个人”，没有跨镜头身份依据时不能把 appearance count 当 unique person count。

全局结果 artifact 与扫描任务状态一起持久化。分页只影响展示，完整扫描状态与完整列表是否截断必须分别表达。非终态恢复沿用相同 QueryPlan；已取消/终止后的补查新建关联 WorkflowRun，复用兼容证据但保留旧运行历史。generation 或媒体版本变化则显式新建查询，不将旧覆盖无条件搬过去。

## 7. 评测集与精确指标口径

### 7.1 数据划分

固定盲测 manifest：24条素材、总长≥12小时、至少6条≥1小时且其中2条≥2小时；320条查询分为200 text LOCAL、60 image LOCAL、60 global（FIRST/ALL/COUNT各20）。另有独立小型开发集供 encoder、RRF、阈值和协议参数选择。盲测按冻结版本执行，不反复调到通过；修复后重跑需保留前后报告与变更原因。

每个 GT 发生实例有稳定 ID、media revision/digest、起止时间或孤立帧时间、模态、标注依据和困难类别。重叠片段里的同一实例只标一次，真实重复标不同 ID。截图 GT 可以是一组真实来源出现实例；同场景不同来源作为负例，不算命中。

独立标注者复核含糊边界与计数定义。不能让被测 Flash 的答案成为唯一真值。除总体结果，还必须按前/中/后时间位置、中文/英文、动作/文字、低运动、短事件、相似负例和不同视频长度分组报告。

### 7.2 候选 Recall@20

对每个有正 GT 的 LOCAL 查询 q，令 Gq 为去重后的相关发生实例集合，Cq 为融合后、语义复核前的前20个不同候选。Recall(q)=被 Cq 命中的 GT 实例数 / |Gq|；最终报告按查询宏平均。多个候选重复命中同一 GT 仅记一次，不把“至少命中一个”当多实例查询的召回率。无正 GT 查询不纳入此分母，另报误命中与错误确认。

文本粗候选匹配规则冻结为：同一 media revision，交集长度/min(候选长度,GT长度)≥0.5，且用于该指标的原子候选区间≤20秒。该规则衡量是否找到了可复看的局部材料；上下文拼接后的长范围不能参与刷分。超过20个独立GT的穷尽问题进入 global 集，不拿理论上受K限制的查询掩盖指标定义。

截图匹配使用最佳匹配 FrameArtifact.actual_us：同一真实来源 media revision，时间落在标注的 GT 出现区间内；若仅标注孤立帧，允许±2秒。多个出现实例按 GT ID 去重。不同来源相似画面即使图像很像也不是来源命中。正例召回与负例错误确认分别报告。

### 7.3 最终定位与支持度

Grounding R@5@tIoU0.5：对每个有正 GT 的局部查询，最终前5区间中至少一个与任一正确 GT 在同一媒体上 tIoU≥0.5 视为该查询成功；tIoU=交集时长/并集时长。该指标是查询成功率，与上节 GT集合召回率不同，报告名称和分母必须写清。

主张支持 precision：人工逐条评审最终事实性 claim 及其直接证据，得到被证据支持的主张数/所有被评审事实性主张数；拒答和未确认候选不计为正确事实以刷高分，另报回答覆盖率。边界误差使用已配对的正确发生实例，分别报告起点/终点绝对误差；P95≤3秒仅适用于持续≥2秒且边界清晰的事件，短事件、缺失事件和误匹配单列，不能丢掉后只报漂亮误差。

### 7.4 全局指标与硬门槛

global 不复用 topK recall：单列 FIRST 正确率、ALL 的实例 precision/recall、COUNT 精确计数率/绝对误差、协议完成率、已确认下界有效性和失败区间比例。带完整标注的确定性夹具应满足正确的去重与覆盖逻辑。自然素材的这些指标先完整报告，不能只报告“任务跑完”。

硬门槛：文本候选 Recall@20≥0.90；截图≥0.85；Grounding R@5@tIoU0.5≥0.80；主张支持 precision≥0.95；符合上述条件的边界误差P95≤3秒；覆盖不足却宣称首个/全部/精确总数为0；租户泄漏、generation混读和半成品可见均为0。全部为拟定 release 目标，未达到需报告并修复，不改分母/目标适配结果。

## 8. 故障、安全与成本验证

测试矩阵覆盖某一路超时、encoder版本不匹配、generation切换、删除与ACL撤销、证据缺失、恶意截图/字幕提示注入、取消、worker重启、外部模型返回越界时间。所有检索路和证据跳转单独验权限，不能只验主搜索接口。

记录每路延迟/候选数、RRF耗时、复看帧与ROI数、模型tokens、总费用、覆盖增量与停止原因。普通已索引搜索、局部回看、全片扫描分开设 SLO；全片扫描允许异步，不能用阻塞HTTP隐藏成本。预算在执行端强制，不依赖prompt请模型“少花钱”。

拟建立 tests/retrieval_grounding、版本化 evaluation manifest 与 benchmark runner。mock用于协议、故障和授权测试；真实Flash/encoder/媒体产物用于质量与端到端验收。报告保存固定配置、代码commit、模型resolved版本、generation、数据hash和原始结果引用。
