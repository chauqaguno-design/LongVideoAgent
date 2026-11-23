# 005 — 版本化剪辑与异步渲染

状态：TODO。M1 先完成显式时间段 → 时间线 → 真实预览 → 修改 → 导出；M4 再加入自然语言选材与语义 QA。本规格不要求先建完 Agent Team 或全片多模态索引。

## 1. 用户结果与范围

用户可从已有或新上传素材指定多个源区间，调整顺序、音频、字幕、裁切与输出规格，获得可播放预览，并在后续修改中保留历史版本。自然语言模式必须使用真实证据和源视频边界，不能生成仅有文案的“剪辑结果”。

| 需求 | 本规格交付 |
|---|---|
| REQ-EDT-001 | 无需通用模型即可完成显式时间段的真实媒体剪辑 |
| REQ-EDT-002 | Flash 将自然语言整理成 EditBrief；硬约束可验证，未知条件可补充 |
| REQ-EDT-003 | 不可变 TimelineRevision、revision_no CAS、EditOperation 幂等 receipt |
| REQ-EDT-004 | 固定版本的异步 RenderJob、缓存、至少一次执行与对账 |
| REQ-EDT-005 | 时间线结构校验和实际成片校验分开，语义验收有来源依据 |
| REQ-EDT-006 | 多轮增删改顺序与回退；旧 job 不能覆盖新版本预览 |
| REQ-EDT-007 | 视频、源音频、独立字幕的同一时间映射；预览与导出可追溯 |

同时依赖 REQ-PRJ-001、REQ-ING-003、REQ-AGT-002、REQ-AGT-004、REQ-OPS-001、REQ-OPS-006、REQ-UX-001、REQ-UX-003。M1 只需要已有上传能力的最小 MediaRevision/ffprobe/资源解析适配；不等待 ASR、视觉语义索引或 screenshot search。

## 2. v1 编辑合同

TimelineRevision 是不可变快照。EditProject 保存 head_revision 整数与 head_revision_id UUID。revision 0 可以为空；空时间线不能 render。每次成功修改产生新的 revision_no，不原地修改旧 JSON。

时间线 v1 为顺序原速视频片段、硬切、单一 output_profile。片段采用 [source_start_us,source_end_us)，整数微秒；区间必须位于媒体有效时域内。clips 数组是输出顺序；服务根据操作结果重排数组并计算输出起点，禁止自行按源时间排序破坏用户叙事顺序，也不接受模型提供的 timeline_start_us。

片段保存稳定 clip_id、不可变 media_revision_id、源区间、evidence_ids、audio_policy keep/mute、可选 crop 和 caption_track_id。append/replace 输入使用无 clip_id 的 ClipDraft：append 由服务生成 UUID，OperationReceipt.clip_id_assignments 按 operation_index 回传；replace 保留目标 clip_id。同 key 重试回传同一组 IDs。v1 不支持同 patch 临时 ID；需重排新片段时先按 append 顺序提交，读取返回 revision/IDs，再发新 reorder patch。

显式选段可没有语义 evidence；其授权来源和用户指定区间仍应可审计。语义选段须关联真实 evidence，验证所属源版本和区间关系。编辑服务不得相信模型编造的已存在 ID。

当前操作包括 append_clip、remove_clip、replace_clip、reorder_clips、set_output_profile、set_caption_policy、restore_revision。reorder 必须提供当前片段的完整无重复集合；replace 保留被替换片段身份；restore_revision 仅能引用同 edit project 的已有版本，并单独提交。它会创建新版本，不倒退 revision_no。

输出 profile 由 schema 约束，服务进一步限制在已验证编码规格白名单内。v1 不接受变速、高级转场、多层叠加、任意 filter 字符串或背景音乐参数；不能把不支持字段静默丢弃。

## 3. 提交一致性

POST /v1/edit-projects/{edit_project_id}/operations 必须在业务服务执行以下语义：

1. 根据真实身份检查项目及所有资源归属。
2. canonicalize patch，计算 request_hash；包括 expected_revision 和完整操作语义。
3. 相同项目、相同 idempotency_key 已存在时，先比较 digest：相同返回原 receipt；不同返回 409。
4. 仅对新 key 检查 expected_revision == head_revision；不匹配返回 409，不自动把旧 patch 套到新版本。
5. 校验/编译全部操作；任何操作失败则整个 patch 无可见修改。
6. 同事务写 revision、operation receipt、head 指针和 outbox。

两个并发不同 key 对同一 head 修改只能有一个成功。同 key 重试即使 head 已前进，仍返回原 receipt。这与单写者策略一起保证一致性，不能把“协调器只有一个”当作数据库防并发机制。

## 4. RenderJob 与可见产物

POST /v1/render-jobs 接收固定 revision_id、PREVIEW/EXPORT 和 idempotency_key；异步返回 RenderJob。客户端与模型不能传“渲染最新版本”让 Worker 到执行时再解析。cache key 由服务器根据固定 revision、展开后的输出 profile、renderer_version 及素材/字幕/字体依赖 digest 计算。

状态只用 QUEUED → RUNNING → VALIDATING → SUCCEEDED，或适用的 FAILED/CANCELLED。任务进度不是完成证明；artifact_id 只有通过验证后才可作为成功产物公开。队列可重复投递，物理计算可能重复；通过 claim/fencing、幂等发布和对账保证同一缓存产物不重复可见。不能宣称 exactly-once 执行。

不同请求 key、相同有效缓存 key 的活跃任务可以合并；已成功且依赖仍有效的产物可以复用。终态失败任务的原请求 key 返回原失败记录；明确新请求可创建新的 retry job/attempt，但不能修改旧终态历史。共享 job 的取消必须尊重其他合法订阅者。

RenderJob.compiled_manifest_artifact_id 指向 CompiledTimeline，逐片段 CompiledClipMapping 同时保存 requested_source_range、realized_source_range、output_start_us/output_end_us、output_start_frame/output_end_frame。输出帧范围同样采用半开区间，可从成片反查源区间。manifest 是业务可读映射；内部解码/滤镜执行细节不能绕过它。

每个 Artifact 的 media_revision_id 与 timeline_revision_id 分别关联源媒体和编辑版本；PREVIEW/EXPORT 由 kind 与 job.profile 明确区分。旧 revision 的 job 晚到，只增加对应版本的产物；当前预览组件必须比较正在显示的 timeline_revision_id，不因最后收到一条 PREVIEW_READY 就自动替换。

## 5. 时间、音频与字幕

源时间、解码 PTS 和输出 timeline 时间分离：

- source_us 是统一媒体时间原点下的用户可见时域；实际解码使用 actual_pts 与 stream time_base。记录视频和音频各自 start_time，不分别盲目清零破坏同步。
- v1 原速片段输出起点为之前片段长度之和；字幕和音频按同一 clip 区间裁切再平移。
- 请求区间与实际视频采样网格不完全一致时，编译器记录实际 PTS、CFR 输出网格、补帧/丢帧策略与量化误差。不能声称 t*fps 是精确源帧号。
- keep 保留对应源音频；mute 与无音轨素材在 v1 统一生成等长静音，输出使用已固定的 48 kHz 音轨规格，避免拼接时轨道错位。
- caption_policy none 不导出独立字幕；preserve_source 导出已映射的独立字幕；burn_aligned 将映射后的字幕烧录。源像素已有烧录字幕仍在，不能通过 none 删除。
- crop 使用服务验证的图像坐标；明确旋转、SAR、缩放前后的坐标映射，禁止把 contact sheet 小图的坐标当原始源坐标。

## 6. 自然语言剪辑与 QA

M4 生成 EditBrief，记录 source_media_revision_ids、目标时长及 tolerance、order_policy、must_include/exclude、音频与输出规格。含糊且会改变结果的输入进入 AWAITING_INPUT；已指定内容不能被“为了更精彩”的偏好覆盖。

候选检索只负责找线索；动作完成、人物/对象关系、台词截断、镜头边界需局部复核。候选证据冲突或时长不足时给出缺口和可选调整，不虚构事件、不自动重复片段填满目标、不擅自加速。排序与选择先产出 proposal，再由同一编辑事务服务提交。

校验分两层：

1. 结构层：ID/权限、范围、片段顺序、时长计算、源存在性、crop、音频轨、字幕、编码规格、Brief 的可机器验证约束。
2. 输出层：读取真实产物，检查解码、长度、首尾/拼接点、音画同步、字幕对齐；M4 用同一 Flash 检查所见内容与证据、Brief 是否一致。

媒体帧/音频编码产生的可解释量化误差与语义目标 duration_tolerance_us 分开报告。禁止用“在 duration tolerance 内”掩盖明显音画错位。验收 fixture 在运行前固定具体容差；实现不能看到失败结果后放宽。

## 7. Given / When / Then 验收

| ID | Given / When / Then |
|---|---|
| A05-01 | Given 已上传有音频视频与两个合法显式区间；When 创建 timeline 并提交 PREVIEW/EXPORT；Then 无模型调用也能生成真实可播放文件，区间顺序、音频与输出规格符合请求 |
| A05-02 | Given 同 key patch 已提交且 head 又前进；When 重试原请求；Then 返回原 operation/revision 与完全相同 clip_id_assignments；改 payload 仍用同 key 则 409 |
| A05-03 | Given 两个不同 key 使用相同 expected_revision；When 并发提交；Then 仅一个新 revision，另一个 409；旧版本和 receipt 不变 |
| A05-04 | Given 越界区间、跨 workspace 源、重复 reorder ID、非法 crop 或未知字段；When提交；Then 422/404 等合同错误，事务无部分修改，不启动 FFmpeg |
| A05-05 | Given VFR、非零起始 PTS、旋转 metadata 或不同音视频 start_time 的真实 fixture；When 裁切拼接；Then 输出按冻结时间映射规则可解释，实际边界/同步在预先记录容差内，无 t*fps 帧号假设 |
| A05-06 | Given 有音轨/无音轨/mute 混合片段与不同分辨率帧率；When 渲染；Then 同一输出 profile，缺音轨部分静音，接点无串音/错位，未默默拉伸 |
| A05-07 | Given 字幕跨 clip 边界；When trim/reorder；Then 字幕先与源区间求交再平移，零长字幕被排除；none 不删除原画面已有字幕 |
| A05-08 | Given job/outbox 已落库但 broker 失败，或消息重复；When恢复投递；Then 可继续查询任务、单个有效 claim、同一可见导出，无“发送失败所以再建一份 job”的逻辑 |
| A05-09 | Given FFmpeg 已完成而 DB 提交前 Worker 崩溃；When重启；Then 检查 staging/manifest/digest 并验证补提交；旧 fencing token 不可发布；物理重复计算不会重复可见 |
| A05-10 | Given revision 2 的预览慢于 revision 3；When两者完成；Then UI 当前 revision 3 不被旧结果替换，用户仍能查 revision 2 的产物，并通过各自 compiled manifest 回查正确源片段 |
| A05-11 | Given 用户回退 revision 1；When提交 restore_revision；Then 创建 revision 4 等新编号，旧记录与当前历史都可追溯 |
| A05-12 | Given 导出与取消并发；When完成仲裁；Then 只公开一个一致终态，CANCELLED 无成功发布，SUCCEEDED 返回已完成事实，不谎报已取消 |
| A05-13 | Given NL Brief 要“所有某动作且约一分钟”但证据不全/素材不够；When规划；Then明确覆盖缺口和时长冲突，不以稀疏抽帧宣称全收录，不自动重复素材凑时长 |
| A05-14 | Given 模型选段语义正确但 FFmpeg 输出损坏或字幕错位；When QA；Then输出验证失败，不能通过语义评分覆盖媒体失败 |
| A05-15 | Given 临时 URL 已过期或 Redis 状态丢失；When打开已成功版本；Then根据 durable Artifact ID/SQL 状态重新授权解析；产物身份和任务历史不依赖 URL/Redis |

## 8. 验收门槛

M1 必须真实通过 A05-01～12、15 中的基础路径，尤其包含一次修改再导出和至少一种崩溃/重复投递。M4 增加 A05-13～14 与真实 Flash 选材/输出 QA。没有真实编码器、素材或模型能力时准确记录 BLOCKED；不能把时间线 JSON、mock URL 或假进度作为完成成片。
