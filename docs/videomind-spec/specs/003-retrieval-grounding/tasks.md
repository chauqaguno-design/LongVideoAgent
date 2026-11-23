# 003 — 可审查任务清单

以下状态对应本地实现与验收记录。

### T03-001 建立查询计划、范围和权限过滤

- [x] VERIFIED
- 需求：REQ-RET-001、REQ-RET-003、REQ-RET-005、REQ-OPS-001
- 依赖：T00-003、T00-004、T01-009、T02-001
- 产物：SearchRequest映射、内部QueryPlan、固定授权/媒体快照、generation绑定、query fingerprint、范围与模式校验。
- 验收：跨媒体FIRST无顺序不得猜测；模型不能扩大范围；各种向量/词法路调用前具有完整权限filter；拟建 `pytest -q tests/retrieval_grounding/test_query_scope.py`。

### T03-002 接入文字词法与语义召回

- [x] VERIFIED
- 需求：REQ-RET-001、REQ-RET-003
- 依赖：T03-001、T02-008、T02-012
- 产物：词法/BGE-M3路、ASR/OCR/caption来源、每路候选和超时信息、中文与混合语言策略。
- 验收：真实generation可检索已知台词/画面描述；其他租户更相似素材不挤掉本租户topK；路失败可见，不以空结果隐藏。

### T03-003 接入截图上传、图像检索和来源定位

- [x] VERIFIED
- 需求：REQ-RET-002、REQ-RET-003、REQ-OPS-001
- 依赖：T03-001、T02-010、T02-012、T00-003
- 产物：统一图像Artifact入口、ROI、同空间查询、actual_us定位、重复来源候选与能力错误。
- 验收：真实裁切/缩放/压缩截图检回正确源帧；关闭OCR辅助仍有图像路；错encoder空间、越权图像、不同来源近似图测试通过。

### T03-004 实现RRF、来源去重与候选分组

- [x] VERIFIED
- 需求：REQ-RET-003、REQ-MEM-004
- 依赖：T03-002、T03-003
- 产物：版本化RRF参数、原子候选保留、重叠实例来源归并、route_ranks、候选trace。
- 验收：固定输入排序可复现；重复chunk不无限加分或计数；不同模型raw score不直接相加；拟建 `pytest -q tests/retrieval_grounding/test_fusion.py`。

### T03-005 实现有界重排与Flash语义复核

- [ ] IMPLEMENTED
- 需求：REQ-RET-003、REQ-RET-004、REQ-RET-006
- 依赖：T03-004、T02-005
- 产物：可选专用reranker适配、同Flash候选复核、证据ID白名单、单帧/时序主张检查、实际usage和预算。
- 验收：真实Flash复核有明确artifact来源；单帧不能确认先后顺序；模型越界/伪造ID/schema错误不进入verified；未配置专用reranker不伪称已部署。

### T03-006 实现原片回看与时间边界细化

- [ ] IMPLEMENTED
- 需求：REQ-RET-004、REQ-RET-006、REQ-ING-003
- 依赖：T01-005、T02-003、T02-004、T03-005
- 产物：连续帧/ROI/音频回看、实际PTS引用、有界加密策略、query-run Evidence、边界精度标记。
- 验收：拿起/放下/转身对照样本能给支持或不确定结果；真实VFR媒体引用正确；预算达到后停止，不生成假精确边界。

### T03-007 实现查询覆盖扫描与FIRST门槛

- [ ] IMPLEMENTED
- 需求：REQ-RET-005、REQ-RET-006、REQ-ING-004
- 依赖：T01-006、T01-007、T01-010、T03-001、T03-006
- 产物：持久扫描QueryPlan、QUERY_VERIFIED记录、按指纹复用、必要前缀核查、失败gap、取消/恢复。
- 验收：45分钟命中且前缀稀疏时只能称“目前发现最早”；前缀未解决候选阻止首次结论；换问题不能复用不兼容验证覆盖。

### T03-008 实现ALL/COUNT实例清单与完整性检查

- [ ] IMPLEMENTED
- 需求：REQ-RET-005、REQ-RET-006
- 依赖：T03-007、T02-006、T00-003
- 产物：全范围公平核查、实例定义/归并、已确认下界、完整结果Artifact、AnswerResult计数限定、展示分页与扫描完成区分。
- 验收：跨块一次+后续两次得到3个实例；超过topK仍保留完整清单；预算不足/失败gap/未知身份不输出无条件精确总数；OBSERVED_ONLY/WITHIN_PROTOCOL与coverage_summary一致。

### T03-009 完成搜索与证据API/播放器联调

- [x] VERIFIED
- 需求：REQ-RET-001、REQ-RET-002、REQ-RET-006、REQ-UX-003
- 依赖：T03-003、T03-006、T03-008、T00-003
- 产物：SearchResult/AnswerResult/Evidence/Artifact读模型、verified与coverage_summary展示、原片跳转、完整清单/工作流入口、冲突说明。
- 验收：真实“截图→命中→回看→证据→源时间跳转”闭环；/search始终只返回候选；global的verification_required为true；score不显示为事实概率；删除或撤权后的结果打不开；部分覆盖与无匹配有不同呈现。

### T03-010 建立开发集、固定盲测与指标runner

- [ ] BLOCKED
- 需求：REQ-OPS-005、REQ-RET-001、REQ-RET-002、REQ-RET-004、REQ-RET-005
- 依赖：T03-001、T00-002
- 产物：独立开发集与24素材/12h/320查询盲测manifest、稳定GT实例ID、许可/digest、标注复核、指标实现与冻结说明。
- 验收：GT重叠去重、真实重复保留；LOCAL事件集合Recall与global指标分开；截图GT区间/孤立帧±2s、不同来源负例明确；手算小样本与runner结果一致。

### T03-011 完成隔离、竞态、取消与故障测试

- [ ] IMPLEMENTED
- 需求：REQ-OPS-001、REQ-OPS-005、REQ-RET-003、REQ-RET-005、REQ-RET-006
- 依赖：T03-004、T03-007、T03-008、T03-009
- 产物：多租户topK前过滤、generation并发切换、删权、路径/图片输入、提示注入、worker恢复和预算故障测试。
- 验收：拟建 `pytest -q tests/retrieval_grounding`；所有路的越权/混读/半成品可见为0；取消与失败不能让覆盖升级；记录重投递后的幂等结果。

### T03-012 执行真实检索与Grounding发布验收

- [ ] BLOCKED
- 需求：REQ-RET-001、REQ-RET-002、REQ-RET-003、REQ-RET-004、REQ-RET-005、REQ-RET-006、REQ-OPS-005
- 依赖：T02-013、T03-009、T03-010、T03-011
- 产物：固定配置/commit/model/generation的盲测报告、分组指标、真实usage/时延/成本、失败样例与未完成项。
- 验收：执行拟建benchmark命令并保存原始结果；文本Recall@20≥0.90、截图≥0.85、Grounding R@5@tIoU0.5≥0.80、主张支持precision≥0.95、适用事件边界P95≤3秒；覆盖越权结论和租户泄漏为0。未达目标如实报告并保持M3未通过，不修改盲测、分母或阈值迎合实现。
