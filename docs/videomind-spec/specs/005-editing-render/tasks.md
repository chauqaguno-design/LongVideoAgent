# 005 — 可执行任务清单

状态见各项清单与 status/PROGRESS.md。T05-001～013 构成 M1，必须先于完整 M2/M3/M5 交付。T05-014～021 为 M4 增强。以下测试路径和脚本为待创建的应用验收入口；本规范包未实现它们。

## M1：第一条真实剪辑链

### T05-001 接入最小不可变媒体源

- [x] VERIFIED
- 需求：REQ-EDT-001、REQ-PRJ-001、REQ-ING-003、REQ-OPS-001
- 依赖：T00-001、T00-002、T00-003、T00-004
- 产物：复用已有上传的 MediaRevision digest、旧 ID 映射、授权 source resolver、ffprobe/PTS metadata adapter、source/playback/proxy mapping artifact 引用与固定版本 GET；与 001 共用身份模型。
- 验收：真实上传可重复读取同一 bytes/version；不同 workspace 不可解析；有/无音轨、非零 PTS、旋转 fixture 的 probe artifact 正确；不依赖 ASR/视觉索引。

### T05-002 建立编辑领域与迁移

- [x] VERIFIED
- 需求：REQ-EDT-003
- 依赖：T05-001
- 产物：EditProject、revision 0、不可变 TimelineRevision/EditOperation、head 指针及唯一索引。
- 验收：test_patch_transactions.py 检查空初始版本、旧版本不可变、UUID 与 workspace 约束；迁移前后旧分析数据可读。

### T05-003 实现受限 Patch 与快照校验

- [x] VERIFIED
- 需求：REQ-EDT-001、REQ-EDT-003、REQ-EDT-005
- 依赖：T05-002
- 产物：append/remove/replace/reorder/profile/caption/restore 的确定性操作及统一字段校验；ClipDraft 输入与服务生成 clip_id_assignments。
- 验收：test_timeline_compiler.py 覆盖完整 reorder、replace 保留 ID、append receipt IDs、无临时 ID、同项目 restore、未知字段/越界/crop 错误、任一失败无部分修改。

### T05-004 实现 CAS、幂等 receipt 与回退

- [x] VERIFIED
- 需求：REQ-EDT-003、REQ-EDT-006、REQ-AGT-004
- 依赖：T05-003
- 产物：same-key-first 的事务、canonical digest、head CAS、operation/revision/outbox 原子提交。
- 验收：真实 MySQL 并发测试同 key 同请求重放、同 key 不同请求冲突、不同 key 同 revision 单胜者、提交后断连重试、restore 产生新编号。

### T05-005 编译实际 PTS 与输出时间线

- [x] VERIFIED
- 需求：REQ-ING-003、REQ-EDT-005、REQ-EDT-007
- 依赖：T05-001、T05-003
- 产物：公开 CompiledTimeline/CompiledClipMapping、内部 typed RenderPlan、common time origin、source PTS/time_base、输出 offset/CFR 有理数策略和量化说明。
- 验收：test_timeline_compiler.py 使用 VFR/非零起点/音视频偏移真值；不出现 t*fps 推算源精确帧号，长片段无浮点累计漂移。

### T05-006 生成真实基础预览和导出

- [x] VERIFIED
- 需求：REQ-EDT-001、REQ-EDT-007
- 依赖：T05-005、T00-002
- 产物：受控 FFmpeg argv renderer、原速 trim/concat、固定 PREVIEW/EXPORT profile、视频规格归一。
- 验收：test_real_media.py -m real_media 生成并播放两个真实区间；顺序、边界、分辨率/帧率、完整解码通过；无模型与 shell 字符串执行。

### T05-007 对齐音轨、缺音轨与静音策略

- [x] VERIFIED
- 需求：REQ-EDT-007、REQ-EDT-005
- 依赖：T05-006
- 产物：keep/mute、无音轨静音补齐、48 kHz 归一、相对音视频起点保留。
- 验收：真实有/无音轨混剪 fixture 的声音标记在期望位置；接点不串音、不短轨截断、不凭 concat 默认行为决定结果。

### T05-008 实现裁切与字幕时间映射

- [x] VERIFIED
- 需求：REQ-EDT-007、REQ-EDT-005
- 依赖：T05-005、T05-006
- 产物：rotation/SAR/crop 映射、scale/pad、caption 三种策略、cue 求交平移与固定字体依赖。
- 验收：真实旋转/字幕跨点 fixture 验证 ROI 与字幕对齐；缺字体失败；none 保留源像素烧录字；错误坐标不启动外部命令。

### T05-009 建立异步 job、缓存与共享 outbox

- [x] VERIFIED
- 需求：REQ-EDT-004、REQ-AGT-004
- 依赖：T05-004、T05-006
- 产物：RenderJob、compiled_manifest_artifact_id、请求幂等 receipt、固定 revision/profile/renderer cache key、共享 outbox/dispatcher 与 job 查询。
- 验收：test_render_jobs.py 覆盖同 key、不同 key 同 cache、profile 改变、空 timeline、broker 断连；M1 可独立运行，004 后续复用该 outbox。

### T05-010 完成 Worker 租约、验证与幂等发布

- [x] VERIFIED
- 需求：REQ-EDT-004、REQ-EDT-005、REQ-AGT-005
- 依赖：T05-007、T05-008、T05-009
- 产物：claim/fencing/heartbeat、staging manifest、VALIDATING、实际文件校验、发布事务、恢复与取消。
- 验收：test_render_recovery.py 在编码后/DB提交前杀进程，重复 delivery、旧租约返回、取消/成功竞争；无重复可见导出或坏文件成功状态。

### T05-011 暴露编辑与渲染 API

- [x] VERIFIED
- 需求：REQ-EDT-001、REQ-EDT-003、REQ-EDT-004、REQ-OPS-001
- 依赖：T05-004、T05-009、T05-010
- 产物：对齐 /v1/edit-projects、revisions、operations、render-jobs 查询/取消；现有路由兼容。
- 验收：合同测试全字段/状态码对齐；跨 workspace、失效 source、旧 revision 仍被正确处理；HTTP 长任务只返回 job。

### T05-012 接入最小版本化剪辑 UI

- [x] VERIFIED
- 需求：REQ-UX-001、REQ-UX-003、REQ-EDT-006
- 依赖：T05-011
- 产物：显式区间列表、源播放/顺序/替换/删除、版本查看、预览/导出、任务失败/取消与结果关联。
- 验收：浏览器实测上传→两段预览→修改→导出；新旧 job 乱序完成不覆盖当前版本，刷新后状态从 API 恢复。

### T05-013 通过 M1 真实媒体闭环

- [x] VERIFIED
- 需求：REQ-EDT-001、REQ-EDT-003、REQ-EDT-004、REQ-EDT-005、REQ-EDT-006、REQ-EDT-007
- 依赖：T05-010、T05-011、T05-012
- 产物：A05-01～12/15 的适用证据、真实 preview/export、probe/QA 报告、revision 链与故障记录。
- 验收：tests/spec_005 基础与 real_media gate；全流程不调用通用模型；没有实际成片或恢复记录不能声明 M1 完成。

## M4：证据驱动的智能剪辑

### T05-014 解析并冻结 EditBrief

- [ ] IMPLEMENTED
- 需求：REQ-EDT-002、REQ-AGT-001
- 依赖：T05-013、T04-003、T04-005、T04-015
- 产物：同一 Flash 的 EditBrief parser、硬约束/偏好、schema 校验、含糊条件补充与 brief artifact。
- 验收：test_intelligent_edit.py 检查 source scope、duration tolerance、must_include/exclude 与输出规格；能力缺失 BLOCKED，不换模型。

### T05-015 接入真实候选与局部边界复核

- [ ] IMPLEMENTED
- 需求：REQ-EDT-002、REQ-RET-004、REQ-RET-006
- 依赖：T05-014、T03-012；M3 的 003 检索与局部复看验收通过
- 产物：evidence 到 clip proposal 的映射、源版本/区间检查、动作/台词边界复核与冲突记录。
- 验收：真实素材中的动作/台词候选有可回放 evidence；单帧相似不当作动作完成，跨源/失效 ID 拒绝。

### T05-016 实现时长、选择和排序约束

- [ ] IMPLEMENTED
- 需求：REQ-EDT-002、REQ-EDT-005
- 依赖：T05-015
- 产物：候选过滤/选段方案、总时长计算、顺序决策、无法满足约束时的缺口报告。
- 验收：真值任务覆盖素材不足、硬要求冲突、用户顺序；不自动重复、越界或变速填时长，明确需要用户取舍的条件。

### T05-017 接入多轮自然语言修改

- [ ] IMPLEMENTED
- 需求：REQ-EDT-006、REQ-AGT-003
- 依赖：T05-016、T04-014、T04-016
- 产物：稳定 clip ID 的修改 proposal、当前 revision 读取、单协调器提交及 CAS 冲突反馈。
- 验收：多轮“去掉上一段/换成后面动作/恢复旧版”产生可解释 patch；并发编辑不覆盖新版本，旧 run/job 不改变当前预览。

### T05-018 增加真实成片语义 QA

- [ ] BLOCKED
- 需求：REQ-EDT-005、REQ-AGT-006
- 依赖：T05-016、T05-010、T04-003
- 产物：结构与媒体 QA 独立报告、同一 Flash 对实际输出和 Brief/evidence 的语义验证、冲突/不确定结论。
- 验收：test_intelligent_edit.py -m live_provider 验证正确语义但损坏文件仍失败；错误选段不能被流畅文案掩盖，能力缺失不伪通过。

### T05-019 验证智能路径取消与旧结果竞态

- [ ] IMPLEMENTED
- 需求：REQ-EDT-004、REQ-EDT-006、REQ-AGT-005
- 依赖：T05-017、T05-018、T04-013
- 产物：steering/new run 与 render 生命周期的联合恢复策略。
- 验收：在检索、局部查看、patch 提交、预览、QA 各边界取消/改目标；旧结果可追溯但不能提交到新 head 或恢复终态。

### T05-020 处理依赖删除与缓存失效

- [x] VERIFIED
- 需求：REQ-EDT-004、REQ-OPS-003
- 依赖：T05-009、T05-010
- 产物：source/subtitle/font 依赖引用、授权失效、staging GC、不可用 artifact 的错误和保留策略。
- 验收：源被删除或权限撤回后不创建新导出；缓存不能绕过授权；正在使用/历史保留的产物按已批准策略处理，GC 不误删可见结果。

### T05-021 通过 M4 智能剪辑真实验收

- [ ] BLOCKED
- 需求：REQ-EDT-001、REQ-EDT-002、REQ-EDT-003、REQ-EDT-004、REQ-EDT-005、REQ-EDT-006、REQ-EDT-007
- 依赖：T05-014、T05-015、T05-016、T05-017、T05-018、T05-019、T05-020
- 产物：自然语言→证据→版本→预览→修改→导出完整报告、费用/延迟、失败案例和限制。
- 验收：A05-01～15 适用范围全部通过；real_media 与 live_provider 分开记录；M1 功能回归无退化，缺能力真实 BLOCKED。
