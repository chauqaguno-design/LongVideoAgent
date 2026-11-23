# 002 — 可审查任务清单

以下状态对应本地实现与验收记录。

### T02-001 建立观察、证据与依赖持久模型

- [ ] IMPLEMENTED
- 需求：REQ-MEM-001、REQ-MEM-002、REQ-MEM-003、REQ-MEM-004
- 依赖：T01-002、T01-004、T00-003、T00-004
- 产物：Observation/Evidence 合同映射、原始响应 artifact、ROI/对齐私有元数据、依赖与质量标记模型、迁移。
- 验收：非法时间/跨媒体来源/跨 workspace 引用被拒；schema 样例 round-trip；空 ASR 不生成伪台词；拟建 `pytest -q tests/multimodal_memory/test_observations.py`。

### T02-002 接入真实 ASR、VAD 与声道适配

- [x] VERIFIED
- 需求：REQ-MEM-001
- 依赖：T01-005、T01-008、T02-001
- 产物：现有 ASR 能力清单、分块/声道/VAD 版本化配置、源时间转换、语言和时间精度记录。
- 验收：真实语音、无音轨、音乐叠人声与单边声道样本；确认 provider 限制和时间粒度；保存真实请求元数据/响应，不把同厂商 ASR 叫作 Flash 原生功能。

### T02-003 实现转写质量、重叠去重与对齐降级

- [x] VERIFIED
- 需求：REQ-MEM-001、REQ-MEM-004
- 依赖：T02-002
- 产物：源区间重叠去重、重复待核查标记、冲突处理、词/句时间不足时的对齐接口与段级降级。
- 验收：真实重复台词保留多次，纯 BGM 模板文字不直接入确定证据；无词时间时不均分伪造；拟建 `pytest -q tests/multimodal_memory/test_asr_quality.py` 并运行真实对齐样本。

### T02-004 接入原图 OCR 与文字复核

- [x] VERIFIED
- 需求：REQ-MEM-002、REQ-ING-003
- 依赖：T01-005、T02-001
- 产物：OCR adapter、原图 ROI/坐标变换、文字跨帧归并、可读性与 ASR 冲突来源。
- 验收：小字裁图可追溯到原帧；翻译字幕不覆盖原声；低对比度面板不因未验证硬阈值消失；真实 OCR 与人工真值对照。

### T02-005 实现 Flash 视觉观察与结构校验

- [ ] BLOCKED
- 需求：REQ-MEM-003、REQ-AGT-001、REQ-AGT-006
- 依赖：T00-002、T00-003、T01-005、T02-001
- 产物：同 Flash 的多图观察 prompt/schema、resolved model 快照、输入ID白名单、原子观察转换、有界修复、真实 usage。
- 验收：真实图片输入和工具能力 gate 通过；单帧不产出已验证顺序事实；伪造 artifact ID、越界时间和画面提示注入被拦；provider 不可用保持 BLOCKED。

### T02-006 实现片段事件候选与跨块实例归并

- [x] VERIFIED
- 需求：REQ-MEM-004
- 依赖：T01-007、T02-003、T02-004、T02-005
- 产物：10秒/2秒重叠片段证据汇聚、时间与语义联合归并、同一实例引用、无法确定关系的保留策略。
- 验收：跨块一次挥手只形成一个候选实例，稍后再次挥手形成另一个；相同文本不同源时间不被无条件合并；不凭衣服颜色创建确定跨镜头身份。

### T02-007 建立章节、素材摘要与失效关系

- [x] VERIFIED
- 需求：REQ-MEM-004、REQ-MEM-003
- 依赖：T02-006
- 产物：连续段/章节/素材摘要、child evidence IDs、覆盖与不确定性、摘要依赖 hash。
- 验收：摘要每个事实可展开到源证据；修改一个子观察只使相关父摘要失效；未覆盖区间不会被摘要补写成剧情。

### T02-008 接入 BGE-M3 与词法文本索引

- [x] VERIFIED
- 需求：REQ-MEM-005、REQ-OPS-001
- 依赖：T01-009、T02-001、T02-006、T02-007
- 产物：真实 BGE-M3 配置审计、分块/前缀/截断策略、词法路、独立来源 payload、BUILDING generation 写入。
- 验收：中文/英文/混合查询开发集可检索；每条向量有版本/来源/权限/generation；重复内容不会被错误放大；真实向量维度和有限值检查通过。

### T02-009 完成图像 encoder 选型与空间 ADR

- [x] VERIFIED
- 需求：REQ-MEM-005、REQ-RET-002
- 依赖：T01-005、T00-002
- 产物：候选能力/许可/成本/图图和图文语言/维度/预处理比较、独立开发集基准、冻结 space descriptor 与部署方案。
- 验收：用实际截图与裁切/压缩/相似负例测量，选型理由有数据；不写未经验证的维度或中文能力；不使用盲测集调参。

### T02-010 构建帧/ROI 图像向量索引

- [x] VERIFIED
- 需求：REQ-MEM-005、REQ-RET-002、REQ-OPS-001
- 依赖：T01-009、T02-001、T02-009
- 产物：帧与可选 ROI 编码器、同空间截图查询适配、独立 collection、实际源时间与 artifact payload。
- 验收：相同输入与配置可复用向量；相同维度不同空间被拒；截图可检回已知帧；OCR 文本路关闭时图像路仍真实运行。

### T02-011 实现依赖缓存与精确失效

- [x] VERIFIED
- 需求：REQ-MEM-003、REQ-MEM-004、REQ-MEM-005、REQ-OPS-003
- 依赖：T02-003、T02-004、T02-005、T02-007、T02-008、T02-010
- 产物：输入/模型/prompt/schema/空间版本 cache key、依赖图、局部失效、删除与权限检查。
- 验收：改变 caption prompt 不重跑无关 ASR，改变 encoder 不混入旧空间，改变媒体 bytes 不命中旧证据；删除后缓存不能继续泄露产物。

### T02-012 联调完整 IndexGeneration 构建与发布

- [x] VERIFIED
- 需求：REQ-ING-005、REQ-MEM-005、REQ-OPS-001
- 依赖：T01-009、T01-010、T02-008、T02-010、T02-011
- 产物：组件 manifest、覆盖汇总、readback 校验、完整/部分能力发布策略、真实 MySQL+Qdrant 切换。
- 验收：构建一套真实文本+图像索引再原子切换；缺 payload/错维度/删除竞态拒绝发布；切换中的请求不混 generation；故障时旧版仍可查。

### T02-013 完成多模态质量与 M2 集成验收

- [ ] BLOCKED
- 需求：REQ-MEM-001、REQ-MEM-002、REQ-MEM-003、REQ-MEM-004、REQ-MEM-005、REQ-OPS-005
- 依赖：T01-012、T02-003、T02-004、T02-005、T02-007、T02-010、T02-012
- 产物：全部 MEM 场景记录、真实调用与产物、模态消融测试、时间/费用/错误分布、未完成能力清单。
- 验收：拟建 `pytest -q tests/multimodal_memory`；真实 ASR/OCR/Flash/BGE-M3/image encoder 各有验收产物；音频幻觉/合法重复、内容注入、跨块去重、缓存失效和多租户测试通过。003 的截图与检索质量仍须单独验收，不能提前标记 M3 完成。
