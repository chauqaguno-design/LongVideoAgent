# 001 — 媒体接收、时间轴与异步处理规格

状态：TODO；目标里程碑：M2。M1 可以复用已有上传/原片读取能力完成显式区间剪辑；若既有路径不满足不可变媒体版本和源时间合同，应先补本规格的最小基础子集，不等待 ASR、视觉或索引全部完成。

## 1. 用户问题

用户上传一条长视频后，应能知道文件是否有效、哪些处理正在进行、后半段有没有被处理、失败部分能否重试，以及结果究竟对应哪个原始文件。系统必须可恢复地处理长视频，并为后续检索和剪辑提供真实源时间，不能因刷新页面、worker 重启或重复请求重新制造素材和任务。

## 2. 范围与需求

| 需求 | 本规格中的可交付行为 |
|---|---|
| REQ-PRJ-001 | 项目内管理素材，原始 bytes 形成不可变 MediaRevision |
| REQ-ING-001 | 接收/复用上传、服务端校验、幂等完成与受控去重 |
| REQ-ING-002 | 持久异步阶段、进度、重试、取消、恢复 |
| REQ-ING-003 | 真实 PTS、time base、源区间与抽帧命中时间 |
| REQ-ING-004 | 跨全片公平采样、失败区间与覆盖账本 |
| REQ-ING-005 | generation 构建隔离、验证和原子发布协调 |
| REQ-OPS-001 / REQ-OPS-003 | 资源鉴权、删除后阻断访问与派生回收 |
| REQ-OPS-006 | schema 一致性、受控媒体解码与安全参数 |

包含本地文件上传及现有上传适配器，不默认实现任意远程 URL 抓取。包含量测、抽帧、代理与音频块生成，但具体 ASR/OCR/视觉观察和向量生成由 002 实现。包含 generation 发布机制，但不把未构建向量的空 generation 标成完整智能检索能力。

## 3. 业务对象与状态

MediaAsset 是项目素材入口；MediaRevision 固定原始 bytes digest。替换文件创建新 revision，历史证据、编辑时间线与渲染引用仍指向原 revision，禁止原地覆盖对象存储原片。

MediaAsset 使用合同状态 UPLOADING、PROBING、AVAILABLE、REJECTED、DELETING、DELETED。AVAILABLE 只表示原片可用于已验证的基础操作，不表示所有智能能力可用；capabilities 必须分别展示 asr、ocr、visual_observation、text_embedding、image_embedding 的 UNCONFIGURED/PENDING/AVAILABLE/PARTIAL/FAILED。

ProcessingRun 使用 QUEUED、RUNNING、PARTIAL、SUCCEEDED、FAILED、CANCELLED。每次运行固定 pipeline_version、媒体 revision、请求能力集合和有效配置快照。SUCCEEDED 表示此次声明的目标全部完成，不能把未请求或未配置的能力显示为可用。PARTIAL 是本次运行的终态，必须带缺失阶段、失败区间和受影响能力；停止后的任务不能继续自动发布结果。统一 JobView 通过 kind、status、terminal 表达等待结果，PROCESSING/INSPECTION 的 PARTIAL 必须 terminal=true；补处理创建关联新运行并复用缓存。

IndexGeneration 使用 BUILDING、READY、ACTIVE、RETIRED、FAILED；只有 ACTIVE 对普通新检索可见。覆盖使用 PLANNED、DECODED、MODEL_OBSERVED、QUERY_VERIFIED、FAILED，具体语义见 [媒体与检索设计](../../docs/04_MEDIA_RETRIEVAL.md)。

## 4. 功能验收场景

### ING-S01 上传中断和恢复

**Given** 用户有项目写权限，上传采用合同允许的 MULTIPART 或 EXISTING_ADAPTER。

**When** 网络中断、页面刷新或签名 URL 过期，用户继续上传。

**Then** 服务端核对已有 part 与归属，复用同一 upload_id，按合同刷新未完成部分；已完成 bytes 不要求无条件重传。若底层仅支持 SINGLE_PUT，应明确其重传语义，不伪装成分块续传。

### ING-S02 完成请求与去重

**Given** 同一上传会话已接收全部数据。

**When** completeUpload 使用相同 idempotency_key 和同一 payload 重复提交。

**Then** 返回同一个结果，不创建第二个 MediaRevision 或重复初始任务；相同 key 携带不同 payload 返回 409。仅凭客户提供 digest 不能跳过服务端校验。同 bytes 的素材是否复用存储不得泄露其他 workspace 的存在。

### ING-S03 校验失败

**Given** 客户端提交了视频 MIME、文件名和预期大小。

**When** 实际 bytes 不匹配、文件不可解码、像素/时长/体积超限，或者文件名带路径穿越内容。

**Then** 使用 bytes 检测和受控解析做决定，进入 REJECTED 或明确的可重试错误；不依赖扩展名，不执行文件名中的字符，不生成可检索内容，不把原始内部路径放入错误消息。

### ING-S04 原片可用与智能能力分离

**Given** 原片已完成基础校验和源时间解析，但视觉模型尚未配置。

**When** 用户打开素材并提交一个有效显式区间。

**Then** 可使用 AVAILABLE 原片继续 M1 剪辑闭环；visual_observation 显示 UNCONFIGURED，智能检索不得返回伪造证据。媒体上传成功与智能处理成功是两个独立事实。

### ING-S05 CFR / VFR 的真实时间

**Given** 一段变帧率视频，其中名义 fps 与实际相邻帧间隔不同。

**When** 系统在 requested_us 抽帧。

**Then** 保存实际命中的 actual_pts、time_base、actual_us，source_frame_index 只有真实枚举时存在；按已冻结的选帧规则命中预期夹具。禁止用 requested_us×fps 当作实际帧号，展示与引用使用实际源时间。

### ING-S06 音视频起点不一致

**Given** 视频流与音频流起始时间存在偏移，或原始 PTS 不从 0 开始。

**When** 生成音频块、帧与源区间。

**Then** 都映射到同一媒体播放原点，保留原始偏移与 time base；跨模态证据在误差范围内指向同一事件。不能把每个流都独立清零后直接拼接。

### ING-S07 长视频全片预算

**Given** 一条两小时视频，在每个 60 秒窗口都有变化候选，片尾另有短暂事件。

**When** 执行默认采样计划。

**Then** 先为每窗安排 4 个均匀点，再在该窗安排最多 4 个事件候选；后段的基础份额不会被前段耗光。真实命中同一帧应去重，窗口缺口仍在账本可见。此场景只验公平分配，不宣称片尾短事件必被采样识别。

### ING-S08 预算不足

**Given** 总预算不足以完成默认全片基础观测。

**When** 预算控制器停止或调整采样。

**Then** 已规划的全片窗口与未完成范围仍保留，ProcessingRun 为 PARTIAL 或相应明确终态，capabilities/coverage 显示限制；不以“已处理前 36 帧”宣称视频已全部看完。

### ING-S09 覆盖阶段不混淆

**Given** 帧已解码，但 Flash 请求失败或尚未执行。

**When** 用户查看覆盖进度。

**Then** 该部分最多记为 DECODED，不是 MODEL_OBSERVED，更不是 QUERY_VERIFIED。同一时间窗的 ASR 成功不得填补视觉路的失败。

### ING-S10 重试与 worker 崩溃

**Given** 某个窗口已写入 artifact，但 worker 在提交完成消息前退出。

**When** 消息重新投递或 lease 过期后接管。

**Then** 通过持久幂等记录和产物核验复用已有结果，观察/帧/账本不重复计数；孤立对象可对账回收。不能依靠 Redis 临时锁作为唯一去重依据。

### ING-S11 取消

**Given** ProcessingRun 正在解码或等待外部能力返回。

**When** 有权限的用户发出取消。

**Then** 取消状态持久化，未启动任务停止领取，FFmpeg 等受控本地进程被终止；已无法撤回的外部调用结果不得再推进该次运行发布。界面说明已发生费用，不假装请求被远端撤销。

### ING-S12 原子发布

**Given** 旧 generation A 正在服务查询，新 generation B 仍在写向量。

**When** B 构建中断，或者两个发布者竞争切换。

**Then** 新查询继续只见 A，或在 B 全部验证完成后一次切到 B；不能部分模态读 A、部分读 B，也不能发布空/缺失但未声明的组件。CAS 失败的发布者重新对账，不覆盖更新的版本。

### ING-S13 模型或策略版本变化

**Given** 原片 bytes 未变，但采样策略、caption prompt 或 embedding 版本发生变化。

**When** 请求重建。

**Then** 新 ProcessingRun 固定新配置，复用输入相同的中间产物，重新构建受影响依赖；ACTIVE generation 保持不可变直至新版本发布。旧证据仍能追溯旧版本。

### ING-S14 删除与权限撤销

**Given** 某素材有原片、缓存帧、旧 generation 和在途任务。

**When** 素材删除或调用者权限撤销。

**Then** 后续读、签名 URL 生成、抽帧、检索立即被授权状态阻断；后台取消任务并回收派生产物。不能等 Qdrant 清理完成才禁读，也不能让延迟到达的 worker 重新激活已删除素材。

### ING-S15 失败重跑

**Given** 某阶段临时失败，原片与成功阶段产物仍完整。

**When** 用户或有界自动重试策略启动恢复。

**Then** 对非终态失联任务按 lease 接管，对 PARTIAL/FAILED/CANCELLED 后的补处理创建关联新运行，只重跑缺失/失效部分并复用有效缓存；格式损坏、越权或合同不符不得无限重试。旧终态保持不变，新的运行/尝试关系必须可审计。

## 5. 非功能要求与拒绝条件

- 权限由服务端解析；workspace_id 不能从模型工具参数直接信任。数据库、对象存储解析和 Qdrant payload 都须绑定资源归属。
- FFmpeg 使用受控 argv、超时、资源配额与固定 resolver 生成路径；不使用 shell=True，不接受任意协议/文件路径作为模型输入。
- 进度显示当前阶段与已完成/总 units；尚未确定总量时 total_units 为 null，不制造虚假的 100%。
- 处理吞吐与每小时视频成本在明确硬件/模型环境测量。时间轴正确性、重投递幂等和全片公平性不因成本优化而放宽。
- 外部能力未配置、接口合同测试失败或真实依赖不可达，记录 BLOCKED 验收项；mock 通过不等于生产能力通过。

## 6. 完成定义

全部 GWT 有自动化或明确人工验收记录；真实 FFmpeg 处理至少 CFR、VFR、无音轨、损坏输入与两小时素材；完成 upload/worker crash/cancel/publish/delete 故障测试；合同示例与实际 API 一致；未配置的智能模态可正确降级。进入 M2 完整验收时，需与 002 的真实观察和索引构建共同验证，不能只用空白索引证明发布成功。
