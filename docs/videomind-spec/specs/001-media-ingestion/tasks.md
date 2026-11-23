# 001 — 可审查任务清单

以下状态对应本地实现与验收记录。

### T01-001 映射既有素材、上传和处理链

- [x] VERIFIED
- 需求：REQ-PRJ-001、REQ-ING-001、REQ-ING-002
- 依赖：T00-001、T00-002
- 产物：现有入口/表/任务/存储/媒体工具与目标职责的映射，复用决策，M1 所需最小媒体子集列表。
- 验收：逐个结论附真实文件与函数依据；确认不存在未说明的第二套素材 ID、第二套队列和强制技术栈迁移；按仓库 AGENTS 审阅。

### T01-002 建立不可变媒体版本与迁移

- [x] VERIFIED
- 需求：REQ-PRJ-001、REQ-ING-001、REQ-OPS-006
- 依赖：T01-001、T00-003、T00-004
- 产物：MediaRevision 与对象引用、digest、MediaAsset 状态/capabilities 映射；数据库约束与 expand/backfill 迁移。
- 验收：新版本不覆盖旧 bytes；两租户相同文件互不可见；旧素材可通过兼容适配读取；执行拟建 `pytest -q tests/media_ingestion/test_media_revision.py` 与迁移往返验证。

### T01-003 实现上传完成、恢复与服务端校验

- [x] VERIFIED
- 需求：REQ-ING-001、REQ-OPS-001、REQ-OPS-006
- 依赖：T01-002
- 产物：统一 UploadSession 适配、签名刷新/恢复、completeUpload 幂等、大小/digest/格式检查、过期会话对账。
- 验收：断网续传、重复 part、URL 过期、同 key 不同 payload、伪造 MIME/digest 和路径字符测试；使用真实 MinIO 完成一条上传，不能仅 mock。

### T01-004 实现源时间轴与真实 PTS 适配

- [x] VERIFIED
- 需求：REQ-ING-003、REQ-OPS-006
- 依赖：T01-002、T00-002
- 产物：媒体/流原点、time base、duration 校验、帧选择规则、FrameArtifact 字段转换与源区间验证。
- 验收：拟建 `pytest -q tests/media_ingestion/test_source_time.py`；真实 CFR/VFR/非零 PTS/音视频偏移夹具逐项匹配期望 PTS，换算舍入误差不超过 1 微秒；无 t×fps 伪造帧号。

### T01-005 封装受控抽帧、ROI 和音频块产物

- [x] VERIFIED
- 需求：REQ-ING-003、REQ-OPS-006
- 依赖：T01-004、T00-004
- 产物：唯一媒体 artifact resolver、受控 subprocess argv、实际命中时间、旋转/crop 坐标、音频块源映射、输入相同的产物缓存。
- 验收：恶意文件名与超大像素不会进入 shell；两个 requested 时间命中同一 PTS 只计一个实际帧；尾部、旋转、无音轨夹具产物可真实解码。

### T01-006 建立阶段作业、outbox 与进度读取

- [x] VERIFIED
- 需求：REQ-ING-002、REQ-OPS-002
- 依赖：T01-002、T01-003、T00-003
- 产物：ProcessingRun 配置快照、阶段 DAG、数据库 outbox、异步 API 和 completed_units/total_units、错误类别。
- 验收：请求立即返回持久 job ID；数据库已提交但消息发送失败时 outbox 可补投；同请求重复提交不建重复运行；未知总量显示 null。

### T01-007 实现跨全片公平采样与覆盖账本

- [x] VERIFIED
- 需求：REQ-ING-004
- 依赖：T01-004、T01-005、T01-006
- 产物：60 秒窗口 4+4 采样、10 秒/2 秒重叠 Segment、全片预留预算、实际帧去重、PLANNED/DECODED/FAILED 与覆盖聚合。
- 验收：确定性两小时时间轴前中后各窗均有基础配额；早段候选增加不减少后段基础预算；预算不足显示未完成范围；拟建 `pytest -q tests/media_ingestion/test_fair_sampling.py`。

### T01-008 接入窗口执行与代理视频映射

- [x] VERIFIED
- 需求：REQ-ING-002、REQ-ING-003、REQ-ING-004
- 依赖：T01-005、T01-006、T01-007
- 产物：公平领取窗口、受控并发、checkpoint、可播放代理与源时间映射、分阶段产物清单。
- 验收：真实两小时媒体完成基础产物；随机抽查前中后跳转对齐；同帧不重复记账；代理与原片关键时间点经播放器/帧产物核验。

### T01-009 建立 IndexGeneration 发布协调器

- [x] VERIFIED
- 需求：REQ-ING-005、REQ-OPS-001
- 依赖：T01-002、T01-006、T00-003
- 产物：隔离 generation namespace、组件 manifest、readback 校验接口、READY/ACTIVE CAS、outbox、旧 generation 引用与回收策略。
- 验收：构建中断仍读取旧版；并发发布仅一个 CAS 成功；读请求固定单一 generation；注入缺 payload/错 revision/缺组件时拒绝发布。完整真实索引集成由 T02-012 验证。

### T01-010 实现恢复、取消和晚到结果处理

- [x] VERIFIED
- 需求：REQ-ING-002、REQ-ING-005
- 依赖：T01-006、T01-008、T01-009
- 产物：持久 lease、阶段幂等、产物对账、重试分类、子进程组终止、取消后发布拦截。
- 验收：在解码后/提交前/消息确认前分别杀 worker 并恢复，产物与完成计数无重复；取消过程中晚到模型结果不改变终态、不发布新索引。

### T01-011 实现能力、覆盖与删除的 API 读模型

- [x] VERIFIED
- 需求：REQ-ING-004、REQ-OPS-001、REQ-OPS-003、REQ-UX-002
- 依赖：T01-007、T01-009、T01-010、T00-004
- 产物：getAsset/getProcessingRun/getCoverage 的合同响应、能力分路状态、删除 tombstone、派生对象/向量回收任务。
- 验收：DECODED 不显示成 MODEL_OBSERVED；已有原片可用而模型未配置时状态准确；删除后旧签名签发、检索、回看与缓存读取立即被拒绝。

### T01-012 完成媒体处理合同与故障验收

- [x] VERIFIED
- 需求：REQ-ING-001、REQ-ING-002、REQ-ING-003、REQ-ING-004、REQ-ING-005、REQ-OPS-005
- 依赖：T01-003、T01-004、T01-008、T01-009、T01-010、T01-011
- 产物：GWT 对应测试、真实媒体 fixture 清单、故障注入报告、成本/时长日志、迁移回滚与未通过项列表。
- 验收：拟建 `pytest -q tests/media_ingestion` 与真实依赖集成命令通过；覆盖上传中断、VFR、无音轨、损坏素材、两小时公平采样、重投递、取消、权限撤销、发布切换；外部未验证项保持 BLOCKED。

## 合并与里程碑门槛

T01-012 完成表示处理框架可验，不自动表示 M2 完成；M2 还需 T02-012/T02-013 的真实多模态索引。每项合并前同步受影响的 spec、schema 和测试；不得为了实现方便把本规格的真实 PTS、公平覆盖或原子发布要求改成宽松口径。
