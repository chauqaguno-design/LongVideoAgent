# 005 — 实现计划

状态：TODO。新增模块名为建议，Codex 必须先复查真实 checkout。目标是最小增量接入现有 VideoMind，先交付 M1，不等待完整 M3/M5。

## 1. 复用与新增

既有 Media 上传/存储、R4Runtime 依赖装配、MySQL、MinIO、RabbitMQ/Celery、TaskLeaseKeeper 与媒体 subprocess runner 是可复用基础。新增的领域对象是 EditProject、TimelineRevision、EditOperation、RenderJob 与 Artifact 关系；AnalysisResult 和 AgentPlan 不承担时间线职责。

参考的 application/checkpoint_service.py 中 staged revision 是分析计划重做，可能清理原 plan/draft/result；不能用于版本剪辑或撤销。application/value_objects.py 的旧 TaskKey 用 media_id、goal、mode，同一用户重复“剪一个短片”不能因此命中同一编辑任务。

建议 application 增加 EditingService、TimelineCompiler、RenderService；基础设施提供 SQL repositories、MinIO Artifact resolver、FFmpegRenderer 与队列适配。现有 infrastructure/media/runner.py 已使用 argv subprocess 和超时/取消回收；继续复用进程管理，增加持久 job 状态与发布协议。

## 2. M1 最小素材接入

M1 需要真实媒体，不得为了提前交付绕过 source identity：

1. 复用已有上传和对象存储入口；为上传 bytes 确认 content digest 与不可变 MediaRevision。
2. 复用共同 Workspace/Project/MediaAsset 身份，建立旧 int media_id 到新 UUID 的服务端映射；不另建一套剪辑专用媒体身份。
3. 使用 ffprobe/受控解码获得 duration、streams、time_base、实际 start_time、rotation、SAR、像素/音频规格；保留原始 probe artifact。
4. resolver 在服务层验证 ownership 与依赖有效性，给 Worker 有限生命周期的本地/对象访问句柄。
5. GET /v1/media-revisions/{media_revision_id} 返回固定版本、source_artifact_id、playback_artifact_id、proxy_time_mapping_artifact_id 与 streams；player 通过受控 artifact 读取源或代理，代理跳转遵守时间映射。
6. 001 后续增强同一 MediaRevision 的处理产物和索引；M1 不需要提前完成 ASR/OCR/视觉观察。

素材仍在上传、不可解码、时域不确定或已删除时，不创建可执行渲染计划。代理不能传文件路径替代受控 resolver。

## 3. 数据与操作算法

核心唯一约束建议：

- TimelineRevision: (edit_project_id, revision_no) 唯一；revision_id UUID 全局唯一。
- EditOperation: (workspace_id, edit_project_id, idempotency_key) 唯一；保存规范化 request_hash、结果 revision 与 actor。
- active render claim: 按 job ID 的 owner + fencing_token 条件更新。
- visible render cache: (workspace_id, cache_key) 唯一；不同租户即使 bytes 相同也不能通过缓存相互发现。
- Artifact 存不可变内容 digest、类型、依赖、实际存储位置/版本；公开响应提供 ID，签名 URL 为临时访问方式。

Patch 在锁定 head 后对完整快照执行。所有操作成功才提交；reorder 检查完整集合，append/replace 验证源版本，restore 读取同项目历史且创建新编号。append/replace 输入用 ClipDraft；append 的 clip_id 由服务分配并在 receipt.clip_id_assignments 按 operation_index 回传，replace 保留目标身份。UUID 分配与 receipt 在同一事务，重放不能再生成新 IDs。v1 无临时 ID；新 append 若需重排，在返回 revision 后再发 reorder patch。

初始 revision 0 空 clips。先查同 key receipt 再检查 CAS。并发 unique-key 冲突不能直接 500：重读 receipt，比 digest，相同返回 replayed=true，不同 409。新请求过期 expected_revision 不自动重基。一个事务写 revision、operation、head、outbox；MinIO 写入或 broker 调用不放进该 SQL 事务阻塞。

## 4. 时间线编译

编译器输入只有固定 TimelineRevision、受控源 metadata、固定 renderer profile/version。输出公开 CompiledTimeline artifact，RenderJob.compiled_manifest_artifact_id 引用它；每个 CompiledClipMapping 保存 requested_source_range、realized_source_range、output_start_us/end_us 和 output_start_frame/end_frame（半开区间）。内部 typed RenderPlan 再保存受控源 PTS、解码/变换步骤、轨道策略、字幕/字体依赖和 digest，必须与公开映射一致。模型既不能提供 argv，也不能提供已编译时间坐标。

原速 clip i 的理想长度为 end_us-start_us；理想输出起点为前序长度累加。源字幕区间 [a,b) 与 clip [s,e) 的交集映射为 [max(a,s)-s+offset, min(b,e)-s+offset)。交集为空时删除，不把结束点视为包含帧。

source_us 与 stream PTS 的换算使用已记录 common origin 和 time_base 有理数。保留音视频相对起始偏移；trim 后对所有轨道使用同一 clip 基准，再按 output offset 拼接。输出 CFR 网格用有理数计算，避免长视频累积浮点漂移；逐帧验证报告记录取整策略与误差，不能伪造 source_frame_index。

v1 为精确区间语义默认重编码，不以任意 keyframe stream-copy 当作精确剪辑。默认去除不可控的 metadata 后按明确旋转/SAR规则归一显示空间，再 crop、scale/pad 到 output profile；采用同一映射给图像证据和 crop，避免旋转素材 ROI 错位。

## 5. FFmpeg 适配与音字幕

渲染器用生成的 argv/filter graph，不接受模型字符串插入；素材路径、字体和字幕通过 resolver，限制协议与访问目录。保留 ffmpeg/codec build、profile 和字体版本在 renderer_version/依赖中。允许的分辨率、帧率、像素格式、编码器与资源上限由部署白名单控制。

每个输入片段先归一：视频重采样至目标 fps/尺寸；keep 的音频按源时域裁切并归一采样率/声道；无音轨和 mute 合成等长静音。v1 固定输出音轨策略，不能通过 concat 是否报错决定。最后原速顺序 concat，再限制目标长度、写 metadata 与验证。

字幕先解析为受控 timed cue，不执行用户字幕中的命令；preserve_source 生成映射后的 sidecar/明确支持的软字幕产物，burn_aligned 生成受控字幕资源并使用已固定字体。缺少必要字体或字幕源应在执行前失败，不输出缺字成片当作成功。caption_policy none 只控制独立轨；已有烧录字幕随像素保留。

PREVIEW 与 EXPORT 展开为不同固定 profile，低分辨率预览仍引用相同 source ranges。不能以代理文件的近似帧率修改原片时间戳。参数不同则缓存键不同。

## 6. 异步作业和发布

RenderService 接收固定 revision_id、profile 与 idempotency_key，校验 scope 和依赖，计算 canonical cache key。优先返回同 key receipt；存在可复用成功 artifact 返回对应结果；同 cache key 活跃 job 合并订阅；否则同事务创建 job/request receipt/outbox。

Worker claim QUEUED → RUNNING 时分配 owner 和递增 fencing_token。heartbeat 以 DB 服务时间延租；旧 token 在进度、完成、取消或发布时全部拒绝。消息 ack 时机与 DB 权威状态协调；RabbitMQ 重投不意味着创建新业务 job。M1 先实现共享 outbox/claim 基础，004/006 直接复用，不复制第二套表与 dispatcher。

输出写到 attempt 专属 staging。成功编码后生成 artifact manifest 与 checksum，进入 VALIDATING。实际文件通过验证后，先确定完整对象已经存在，再以 SQL 事务发布 artifact、cache pointer、job SUCCEEDED、outbox。Artifact.timeline_revision_id 绑定固定编辑版本，media_revision_id 用于源相关产物，不能混用为含糊的 source_revision_id。对象存储和 SQL 无跨系统原子事务；通过 immutable key/manifest 与恢复器对账。数据库不能提前指向未上传完的对象。

崩溃恢复先查 job、attempt manifest、staging、已发布 cache；若有效可补验证/提交，否则重跑受控 attempt。旧 attempt 临时数据按引用与 TTL 回收。承认可能重复 CPU/编码计算，仅保证发布幂等与一致可见。

失败终态保留错误类别、可重试性、日志 artifact 和输入版本；新显式重试创建可追溯 attempt/job，不原地擦掉失败历史。取消先写请求，停止发布/回收子进程后仲裁终态；用户新版本不默认删除旧版本产物。

## 7. 验证与 M4 选材

结构 Validator 在提交和 render 入队前执行；Output Validator 读取真实 artifact，不只检查退出码。至少检查 ffprobe streams/profile、可完整解码、实际时长、首尾和接点帧、声音标记/同步、字幕 cue 以及 crop 区域。测试 fixture 自带真值和冻结容差：视频量化按输出网格，源边界按实际 PTS 采样，音频按采样/编码帧；业务 duration tolerance 与这些误差分开。

M4 使用 003 的 evidence search 与 local inspect，再由 004 的 Flash Gateway 生成 EditBrief/clip proposals。选材器把 must_include/exclude、source scope、target duration/tolerance 当硬约束；选择顺序、冗余和衔接做可解释决策。可先采用确定性候选过滤/时长装箱，再让 Flash 提建议；不能让模型未验证的秒数直接进入 FFmpeg。

语义 Reviewer 读取真实输出片段与源证据，报告哪些要求已支持、冲突或不确定。不可机器判定的审美偏好不做伪造的“100%通过”。必要视觉能力未通过，相关语义 gate BLOCKED；媒体渲染服务仍能用于 M1。

## 8. API 与 UI

沿用合同中的 /v1/edit-projects、版本查询、operations、render-jobs 查询/取消，以及项目 workflow/edit 列表、素材 processing 历史和编辑版本 render 历史。UI 最小包含：素材选择与 source player、区间列表、顺序/删除/替换、输出规格、版本列表、当前版本预览、导出按钮、任务进度/失败/取消。首次提交后显示服务器返回的 revision 与 clip ID，后续操作引用稳定 ID。

用户看到的“当前版本”“预览版本”和“正在渲染版本”分别展示。SSE PREVIEW_READY 必须含可解析产物引用，客户端加载前核对 revision。重新连接先读快照再补事件。下载链接刷新不创建新 artifact 或 render job。

## 9. 待创建测试与门槛

以下均为实施要求，不是已运行测试：

~~~bash
python -m pytest tests/spec_005/test_patch_transactions.py tests/spec_005/test_timeline_compiler.py
python -m pytest tests/spec_005/test_render_jobs.py tests/spec_005/test_render_recovery.py
python -m pytest tests/spec_005/test_real_media.py -m real_media
python -m pytest tests/spec_005/test_intelligent_edit.py -m live_provider
~~~

真实素材集至少包含有音频、无音频、VFR/非零 PTS、不同分辨率/帧率、旋转、字幕跨裁切点、多来源混剪；用可辨认的画面/声音标记检查范围和同步。M1 gate 保存 timeline、typed plan、真实 preview/export、probe/验证报告、一次修改的 revision 链与故障恢复记录。M4 再增加真实选材与语义 QA，不能用 mock URL 代替。

## 10. 源码依据

- [value_objects.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/value_objects.py)
- [checkpoint_service.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/checkpoint_service.py)
- [runner.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/media/runner.py)
- [celery_tasks.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/infrastructure/celery_tasks.py)
- [task_lease.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/application/task_lease.py)
- [r4_runtime.py](https://github.com/Morimi-Kazuha/VideoMind/blob/6bfbe9580271406fd0c2942f7800635d4a824e0e/src/dovideo/presentation/api/r4_runtime.py)
