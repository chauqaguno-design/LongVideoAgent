# 001 — 实施计划

状态：待实施。先审查用户实际 checkout 与现有 AGENTS，复用既有上传、任务、存储和媒体服务；以下模块名表示职责，不要求重建一套目录。

## 1. 先做的仓库映射

M0 输出“现有入口 → 领域能力 → 缺口 → 目标改动位置”表，至少覆盖上传完成、媒体表、原片路径解析、Celery task、任务状态、FFmpeg 调用、Qdrant filter 和 MinIO URL 签发。没有证据的功能标记待核实。现有接口适配到统一合同，避免同时维护两套素材 ID 或两个独立任务系统。

## 2. 分层与操作合同

| 层/组件 | 职责 | 不应承担的职责 |
|---|---|---|
| API | 鉴权、schema 校验、提交/查询/取消作业、签名 URL | 长时间解码、直接执行模型计划 |
| 应用服务 | 幂等键、阶段 DAG、预算、版本快照、状态转换 | 将模型返回值当作权限 |
| 领域 | MediaRevision、源时间、覆盖、generation 发布条件 | 依赖 FFmpeg 命令行细节 |
| 基础设施 | MinIO、MySQL、RabbitMQ/Celery、FFmpeg、Qdrant 适配 | 私自宣布业务成功 |

公开操作沿用 [OpenAPI](../../contracts/openapi.yaml) 的 createUpload、completeUpload、getAsset、listAssets、createProcessingRun、getProcessingRun、cancelProcessingRun、getCoverage、deleteAsset；上传刷新操作按统一合同实现。禁止在实现中另造功能相同但字段不同的路由。统一等待通过 JobView.kind/status/terminal：PROCESSING 与 INSPECTION 的 PARTIAL 是终态，不能当作仍在运行的中间进度。

UploadComplete 的 parts/etag 与服务端存储会话核对；etag 不作为 SHA-256 的替代。ProcessingCreate 的 requested_capabilities 明确本次目标，force_rebuild 不等于覆盖旧产物。返回 MediaAsset.capabilities 与 ProcessingRun.status 时逐项映射，不用一个布尔 ready 替代。

## 3. 数据与约束

公开响应严格遵守 domain.schema.json。以下是需要在持久层补齐的内部元数据，不应直接作为 additionalProperties 塞进合同：

- MediaRevision：原片 object key、服务端 digest、size、流元数据、媒体原点、时长、解码可用性、不可变创建信息。
- ProcessingRun：有效配置 snapshot、阶段依赖、输入 digest、取消请求时间、lease、attempt、幂等 key+payload hash、预算与错误类别。
- 阶段产物依赖：输入版本、算法版本、参数 hash、artifact IDs、提交状态；支持对账查找“对象已存在但任务提交丢失”。
- CoverageRecord：公开字段之外内部保存所属 run、模态、策略版本、有效观测集合、query fingerprint（查询级时才有）、尝试记录。
- IndexGeneration：组件 manifest、scope、expected predecessor、校验摘要、active 指针与引用保留情况。

数据库约束至少包括：(workspace, operation, idempotency_key) 唯一；同源版本/步骤/输入hash/配置hash 的有效产物提交唯一；active 指针每发布 scope 唯一；所有业务表 workspace_id 非空。物理对象复用不得绕过逻辑权限或造成跨租户 digest 存在性查询。

## 4. 上传与初始量测算法

1. 鉴权并创建上传会话，生成服务端对象键；文件名仅作显示。
2. 根据现有能力选择 SINGLE_PUT、MULTIPART 或 EXISTING_ADAPTER，记录真实支持的恢复方式和过期时间。
3. 完成时验证会话归属、part 清单、实际大小和服务端 digest；事务写不可变 revision 与 outbox，不把队列发送成功作为提交必要条件。
4. 受控 worker 检测容器和流，校验可解码性、大小/时长/像素限制，确定媒体播放原点与源时间合同。
5. 使原片 AVAILABLE 或 REJECTED，智能 capabilities 保持准确状态；自动智能处理若启用，使用独立 ProcessingRun 和声明目标，不阻塞原片基础操作。
6. 对上传会话过期、完整对象未 finalize、对象成功但数据库提交失败分别设置对账任务，不直接清理仍有有效引用的对象。

## 5. 时间轴与产物算法

使用 ffprobe/解码器提供的真实 PTS、best effort timestamp 与 time base；明确 presentation order，不能将 decode order 当播放顺序。用有理数换算得到源微秒时间并保存原始值。帧选择策略在适配器固定，两个抽帧入口共用一个实现。

验证容器 duration、流末样本时间与帧显示时长，出现偏差时保留诊断。短片边界不能因固定“减 0.1 秒”而漏掉可观尾部；应选择最后一个真实可展示帧。重复 PTS、丢帧、无法解码部分分别记录，无法可靠重建的区间标记失败。

抽帧先输出临时 artifact，再校验图片能解码、宽高和内容 digest，最后提交 FrameArtifact。source_frame_index 非必需，不能为满足 UI 强行估算。ROI 坐标使用原片方向/旋转处理后已冻结的坐标约定，旋转元数据、实际展示宽高和 crop 变换都保存。

音频块保留源开始微秒、采样率、声道策略与样本数；有重叠的块都知道核心区间与上下文区间。代理生成与原片映射必须可验证，后续字幕和剪辑引用始终使用原片时间。

## 6. 采样计划与公平调度

### 6.1 两阶段计划

先计算全片 60 秒窗口 W，固定各窗基础位置与尾部窗口，再为所有窗口写 PLANNED。对每窗建立基础队列和额外候选队列；基础队列拥有独立保留预算。默认基础 4 点、额外最多 4 点。短片不足唯一帧数时按实际可用帧完成并记录原因。

事件评分可以使用镜头切换、像素差、活动边界等确定性信号；参数进入 sampling_policy_version。首版不训练语义事件检测器。额外候选与基础点在实际 PTS 上去重，候选过近不消耗额外有效帧名额；无法补齐时记录候选不足，不造假。

执行器按窗口轮转或加权公平队列领取基础任务，禁止先完整处理最早窗口再耗尽总预算。模型观察调度也沿用公平策略，否则虽然全片帧已解码，模型仍可能只看前半段。预算拦截发生时显式记录未完成范围和停止原因。

### 6.2 片段与覆盖

创建默认 10 秒、2 秒重叠 Segment；尾段覆盖至真实 duration。每个 Segment 记录核心/上下文范围及所属 60 秒窗口；跨窗口引用通过关系表解决，不复制观察为多个独立事实。

DECODED 只在产物提交后记录。MODEL_OBSERVED 由 002 在有效观察提交后推进，QUERY_VERIFIED 由 003 在查询协议执行后写入；001 不允许直接越级。覆盖聚合先去重实际帧，再计算边界与相邻间隔，不能用 requested 时间计算虚假的密集观测。

## 7. 作业恢复、取消与发布

采用持久阶段 DAG：量测 → 时间/窗口计划 → 帧/音频/代理产物 → 各模态观察（002）→ 索引组件（002）→ generation 验证 → 发布。独立模态可并行，状态提交、权限检查和发布必须顺序受控。

每阶段使用数据库幂等键与 lease；Celery retry 是 at-least-once，不能据 broker ack 宣称 exactly once。worker 每完成一个可恢复窗口就提交 checkpoint。lease 过期接管者检查产物、输入版本和取消状态再执行。临时故障指数退避且有上限；格式错误、超限、越权、模型 schema 不兼容应明确失败或 BLOCKED。

取消写数据库后通过 outbox 通知执行器；执行器周期检查且在不可逆提交前再次核对。终止 FFmpeg 子进程组、停止领取新工作、标记晚到结果不可发布。取消不是删除，保留已提交产物供明确的新运行复用。

generation 协调器在独立 namespace 写 Qdrant，校验向量数、空间版本、ACL payload、原片 revision 和覆盖清单。READY 后用 MySQL CAS 切换 active 指针，同时写 outbox；读取端绑定一个 index_generation_id，按 manifest 读取对应的全部 collection。旧版异步回收，切换失败不影响旧版。

## 8. 迁移与兼容

采用 expand → backfill → verify → switch → cleanup。先新增 revision、覆盖与版本字段，保留旧读取适配；对旧素材计算或核验 digest 和时间元数据，不假设旧的 float 秒/帧号准确。无法恢复实际帧来源的旧记录标为 legacy/未校验，不迁移成 SUPPORTED 帧精确证据。

旧向量进入明确 legacy generation，只有满足权限与版本 payload 的数据可被新路径查询。新旧观察不原地混合；发布失败回滚 active 指针即可恢复旧索引。大回填任务按 workspace/project 分批、可取消，并与在线上传设置资源隔离。

## 9. 验证与风险

拟建立 tests/media_ingestion/ 下的时间轴、采样、恢复、发布和授权测试；名称是实施要求，不代表文件已存在。真实 FFmpeg 夹具须包含 VFR、非零 PTS、旋转、无音轨、尾帧、损坏区间；长视频公平性可先用确定性时间轴验证算法，再用真实两小时媒体验证解码和账本。

| 风险 | 检查与缓解 |
|---|---|
| 时长或流偏移错误 | 原始 PTS/流起点持久化，已知真值夹具与播放器跳转核验 |
| 长片前段耗光预算 | 全片基础预算先预留，窗口分布断言与末段测试 |
| 半成品索引可见 | 隔离 generation、readback 校验、CAS 指针最后切换 |
| 重投递与取消竞态 | 持久幂等、提交前状态核验、晚到结果测试 |
| 过期 URL 或删除后缓存泄漏 | 签发时与读取时鉴权、短 TTL、删除 tombstone 优先 |
| 模型费用被误估 | 记录实际 usage；拼图数量与 token 上限分开，不用固定每图常量 |

验收结果需保留命令、运行环境、日志与产物引用。只有 mock 的外部能力标为 BLOCKED；该状态不得被总览中的“全部完成”掩盖。
