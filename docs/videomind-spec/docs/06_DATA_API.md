# 数据模型与接口实施说明

本章解释contracts目录的目标合同。OpenAPI与JSON Schema描述待实现接口，不代表当前VideoMind已有这些端点。与现有API共存时通过adapter转换；认证、既有前端和旧调用方保持兼容。若实际仓库模型不同，先记录字段映射和迁移，再修改合同或实现。

## 1 合同文件与校验层

contracts/openapi.yaml定义HTTP路径、请求响应、状态与错误。contracts/schemas/domain.schema.json定义共享实体。contracts/tool-registry.json定义Agent可用工具、角色、超时与副作用。contracts/examples包括合法示例、结构非法示例和需要领域校验发现的非法时间区间。

校验分四层：JSON Schema验证类型/必填/枚举；服务层验证认证/资源归属/配额；领域层验证区间/版本/幂等/状态转换；真实媒体验证解码、时长、音轨和画面。不能只通过Schema就认定业务有效。

## 2 通用约定

所有持久业务对象具有workspace_id；用户从已验证身份进入workspace context。查询、对象存储、向量库过滤、缓存键、队列消息和日志关联都包含workspace或可唯一解析到workspace的资源ID。服务器不能因为UUID难猜就省略权限校验。

MediaRevision与FrameArtifact保存同一个source_origin有理数秒值。规范时间按actual_pts乘time_base，减去source_origin，再一次性转换为整数微秒；不先分别舍入两个时间。负PTS和非零起点按同一规则处理，FrameArtifact中的原点必须与其媒体版本一致。

ID由服务生成；数据库遵循既有UUID存储约定。时间戳使用UTC，前端按用户时区显示。时间范围统一为整数微秒[start_us,end_us)，实际帧另存PTS和time_base。请求中的时间不得超出JSON安全整数范围。局部裁图坐标为旋转归一化后原画面像素坐标，crop映射记录源尺寸和旋转信息。

列表采用cursor和limit，不使用会随新数据漂移的无约束offset。异步创建返回202和job/run ID；创建资源通常201；不存在和无权访问均以不泄露存在性的404处理。用户未认证为401；参数或媒体不满足域约束为422；版本和幂等冲突为409；预算与限速为429；必需能力未配置为503。

## 3 逻辑数据表

每张表至少含主键、workspace_id、created_at；可变表增加updated_at和row_version。下表为逻辑模型，Codex必须结合现有迁移拆成增量DDL，避免另起一套重复媒体表。

| 实体 | 核心业务字段 | 唯一约束或主要索引 |
|---|---|---|
| Project | title、owner、membership | workspace与project |
| MediaAsset | project、name、current_media_revision、status | workspace/project/status |
| MediaRevision | asset、sha256、object_key、duration、streams、probe_version | workspace/asset/content digest |
| ProcessingRun | media_revision、pipeline_version、stage、status、attempt | workspace/media_revision/status |
| Segment | media_revision、start_us、end_us、level、parent、generation | workspace/generation/media/time |
| SemanticEvent | action、participants、range、segment/evidence引用、归并版本 | workspace/generation/media/time |
| Summary | level、text、子摘要与证据、未观察范围、模型版本 | workspace/generation/media/level |
| FrameArtifact | actual_pts、time_base、requested_us、actual_us、crop、sampling_version | source revision/actual PTS/transform hash |
| Observation | modality、range、text、model与prompt版本、quality_flags | workspace/generation/media/time/modality |
| Evidence | claim、source范围、support、artifact与observation引用 | workspace/project/media/time |
| CoverageRecord | window、stage、protocol、query_run、frame IDs、gap | workspace/media/protocol/query/window |
| IndexGeneration | scope、状态、配置manifest、各embedding空间 | active指针由单独发布记录唯一约束 |
| WorkflowRun | intent、state_version、phase、pending jobs、budget、checkpoint | workspace/project/status |
| ToolExecution | run、tool_call_id、request_hash、effect、receipt、status | workspace/run/tool_call_id唯一 |
| EditProject | project、head_revision_no、head_revision_id | workspace/project |
| EditBrief | goal、源集合、时长和排序约束、输出profile | edit_project/brief_version |
| TimelineRevision | revision_no、parent、clips、profile、duration、brief | edit_project/revision_no唯一 |
| EditOperation | idempotency_key、request_hash、expected_revision、receipt | workspace/edit_project/idempotency_key唯一 |
| RenderJob | revision_id、profile、renderer_version、cache_key、attempt | workspace/cache_key的活跃或成功记录受控去重 |
| Artifact | kind、digest、object_key、来源、生命周期 | workspace/digest/用途与变换版本 |
| TeamTask | run、role、dependencies、lease、fencing_token、attempt | run/status/lease_until |
| MailboxEvent | sender、recipient、sequence、type、artifact refs | recipient/sequence唯一、message_id去重 |
| OutboxEvent | aggregate、event_type、payload、published_at、attempt | pending状态与next_attempt索引 |

关系表用于Evidence到Observation/Artifact、TimelineClip到Evidence、Task依赖等多对多引用。保存引用前验证同workspace和合法源版本；必要时使用复合外键或服务端事务保证，不能只靠前端限制。

## 4 时间与字幕映射

TimelineClip保存源start/end；输出位置由服务端按片段顺序累计。v1原速硬切，片段理论输出长度等于源end-start。编译到固定帧率时使用有理数时间和一次性边界量化，不在每个步骤反复float round。source event与output clip必须保存双向映射。

字幕与源片段求交后平移到输出起点；越界部分截断，跨剪点的同一句话根据断句规则拆分或调整边界。保留源画面烧录字幕只是保留画面像素，不能把它误当成可删除的独立字幕轨。burn_aligned模式只使用已对齐字幕，不把未验证ASR覆盖原字幕当作事实修正。

源无音轨时，编译器明确为该段生成静音或在整条输出统一采用无音轨策略；有音轨段统一采样率、声道与时间基。混合竖屏横屏素材采用指定fit/crop策略，默认letterbox保留全画面；crop必须显式记录并可回到原画面。

## 5 索引发布协议

新版本构建BUILDING generation，在独立命名空间写入文本、视觉与分段记录。检查manifest、记录数、引用完整性、模型版本和各能力状态后置READY。短事务切换scope的active_generation_id，再把旧版本标记RETIRED。查询开始时读取并固定generation快照，整次检索和引用保持一致。

不能先删旧索引再重建，不能混用新描述与旧embedding空间。部分能力失败时可保留PARTIAL处理状态；是否发布该组合由明确的能力策略决定，界面必须显示缺失路线。完成异步清理前保留旧generation以支持已开始查询和回退，清理依据活跃引用与保留期而非固定立即删除。

## 6 时间线提交事务

请求包含expected_revision、idempotency_key和operations。服务端将请求规范化后计算hash，顺序如下：

1. 验证身份与编辑项目归属。
2. 查找该workspace/edit_project/key的receipt；hash相同返回原结果，hash不同返回IDEMPOTENCY_CONFLICT。幂等回放不得先因head已变化而失败。
3. 开启事务，锁定或CAS更新head；再次处理并发插入同key的唯一约束竞态。
4. 检查expected_revision，读取不可变父版本，应用全部operations并执行领域校验。
5. 创建新revision、操作receipt、head更新与outbox事件，一并提交。
6. 返回新revision身份；worker消费outbox执行后续动作。

restore_revision读取历史内容并生成更高revision_no，不倒退head编号。reorder_clips必须是当前clip ID的完整排列，不允许丢失、重复或跨项目ID。replace_clip的目标ID与新clip身份须符合领域约定；不能通过替换偷偷引用无权媒体。

## 7 渲染与产物发布

cache_key包含workspace、revision内容hash、render profile、renderer版本及所有依赖媒体/字幕版本。相同可复用请求优先返回已有成功artifact；重复消息只允许一个有效lease持有者推进可见状态。worker的fencing token每次接管递增，提交状态时核验token。

对象存储临时对象按job/attempt命名。渲染完成先做ffprobe和decode验证，再写验证报告，最后事务发布Artifact与RenderJob成功状态。崩溃后若对象已存在而数据库未提交，恢复任务应验证digest并补交；若对象不完整则清理或重做。过期worker可以完成临时文件，但不能将其发布为新成功结果。

取消是协作式状态变化：终止子进程组、释放预算、清理临时产物。取消请求与成功提交并发时，在事务中决定谁先成为终态；返回实际状态。用户修改产生新revision不改变旧job绑定，只把当前界面的旧预览标记为过期。

## 8 HTTP与工具边界

HTTP面向产品用户和前端；tool-registry面向已运行的Agent。Agent不通过拼HTTP路径绕过工具权限。Coordinator可以提交编辑和渲染，其他角色只能建议修改或读取证据。只读工具可按输入与版本缓存；产物生成工具需要幂等键和预算预留，即使它不修改时间线也有计算副作用。

inspect_overview/inspect_clip/read_region提交短耗时作业并返回job ID；工作流进入WAITING_JOB，持久调度器在结果事件到达后恢复。禁止让模型用紧密循环轮询大型渲染任务。

## 9 SSE与前端一致性

事件持久保存event_id和单run递增sequence。前端以event_id去重，使用Last-Event-ID恢复，收到过期cursor时重新读取工作流快照并订阅。事件payload中携带实际run、revision或artifact身份，不能以“最新一条消息”推断当前预览属于哪个版本。

事件中不包含密钥、完整provider请求、签名长期URL或模型内部推理内容。费用和进度可逐步更新；无法知道总量时total_units为空，界面显示当前阶段而不显示虚假的百分百。

## 10 删除与缓存失效

删除先记录tombstone并拒绝新读取/检索/渲染，再取消相关未完成作业，清理索引、缓存、派生产物和源媒体。引用同一物理blob的其他已授权逻辑资产不能被误删；跨workspace不能因为digest相同共享可访问路径。明确已导出文件保留策略；原视频删除后的编辑项目显示缺失源并阻止依赖源的重新渲染。

缓存键至少包括workspace、源revision、操作版本、必要模型/prompt配置和变换参数。提升模型版本或更改采样不能复用旧结果而隐瞒来源。所有失效传播事件可重放、可去重。

## 11 合同变更和兼容

新增可选字段通常兼容；改变字段单位、枚举、状态语义或删除字段属于破坏性变化，必须新版本或显式迁移。不要把整数微秒改成浮点秒以适配某个库；由adapter转换并验证。生产迁移按expand、backfill、switch、contract分步进行，先验证回滚和旧版本共存。

## 12 完整读路径与刷新恢复

MediaAsset提供source_artifact_id和playback_artifact_id；固定历史媒体版本从media-revisions端点读取。原容器不适合浏览器播放时生成proxy，并保留proxy_time_mapping_artifact_id。播放器不能使用上传PUT URL当播放地址。

项目下有workflow-runs和edit-projects列表，素材下有processing-runs列表，编辑项目下有render-jobs列表。页面刷新和新设备访问从这些服务端记录重建界面，不依赖localStorage保存业务历史。Observation独立可读取，使证据卡能显示转写/OCR原文与视觉观察的来源区别。

RenderJob返回compiled_manifest_artifact_id。其CompiledTimeline绑定具体render job、profile和revision，保存每个片段请求源范围、实际源范围、输出微秒与帧边界。前端在预览生成前可显示名义长度，预览生成后以该映射实现精确跳转和字幕定位，不自行累加浮点长度。

## 13 预算 恢复与目标替换

WorkflowRun含BudgetUsage：输入输出token、图像和调用次数、已提交与已预留费用、价格版本和计价状态。未知金额为null，不能假报0。要求货币硬预算但定价配置缺失时禁止付费调用。预算变更只通过授权用户的typed API，不能由模型工具自行扩大上限或改变币种。

恢复请求先按run/key查receipt，再校验payload和state_version。AWAITING_INPUT可补充信息后继续；FAILED仅在error.retryable为true时追加新attempt并继续，保留原失败记录。SUCCEEDED、CANCELLED和SUPERSEDED不可恢复。resume提交checkpoint、receipt与outbox同事务。

运行中改变目标时，WorkflowCreate可显式携带supersedes对象，指定旧run和其expected_state_version。事务内创建新run并将指定非终态旧run标为SUPERSEDED，传播取消信号；不影响同项目其他并行任务。旧run已经终态或版本过期返回409，由调用者读取状态后选择后续任务。

所有可取消作业持久保存cancel_requested_at，因此用户刷新后仍能看到“正在停止”。JobView用kind区分PROCESSING、INSPECTION与RENDER，并给出受约束状态、terminal和结果引用。PARTIAL是本次处理的终态；补处理创建新run或新attempt记录，并复用已验证产物，保留原始结果。

## 14 检索候选与全局答案

/search仅返回同步候选。query_id标识单次检索；query_run_id标识执行全局验证的WorkflowRun。FIRST、ALL和COUNT应进入持久工作流核查，候选响应以verification_required提示该需求。不能将candidate_count视为已确认事件总数。

SearchResult的coverage_summary记录请求、已核查和未核查范围，truncated明确候选或展示被截断。即使某个既有验证协议完成，也不能用topK列表冒充完整ALL清单。最终AnswerResult记录逐条主张和证据、found_event_count及其OBSERVED_ONLY或WITHIN_PROTOCOL限定；完整事件清单单独保存为QUERY_RESULT产物。语义无遗漏保证始终为false，协议内核查结果必须保留相应限定。
