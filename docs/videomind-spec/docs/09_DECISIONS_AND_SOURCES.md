# 决策记录与事实依据

日期 2026-11-23。本文将用户已确定的选择、当前源码观察和待实施设计分开，避免把建议写成现有能力。

## 1 已确定的选择

| 决策 | 内容 | 原因与后果 |
|---|---|---|
| ADR001 | 以现有VideoMind为主体增量开发 | 保留上传、任务、存储、检索和前端基础；实际checkout仍要审计 |
| ADR002 | 通用LLM/VLM全部DeepSeek Flash | 统一模型成本和运维；专用ASR/embedding不受此限制 |
| ADR003 | 长视频先分块记忆，查询时局部复看 | 预处理可复用；采样无法保证零漏检，必须保存覆盖和来源 |
| ADR004 | Watch工具思想作为adapter参考 | 复用工具分工，避免嵌套两套主Agent和状态体系 |
| ADR005 | 源时间使用整数微秒与真实PTS | 支撑可追溯引用和剪辑；输出时间单独编译 |
| ADR006 | 索引用generation原子切换 | 重建失败不破坏旧查询，代价是短期保留两套索引 |
| ADR007 | 先显式剪辑，再语义选片 | 先验证实际编辑和渲染，降低同时调试多条不确定链路的难度 |
| ADR008 | 不可变TimelineRevision与CAS | 多轮修改可审计；冲突需要重新读取head而非静默覆盖 |
| ADR009 | 幂等receipt与outbox | 适配至少一次队列，外部副作用仍需对账与去重 |
| ADR010 | v1顺序原速硬切 | 保证基本剪辑可靠；复杂转场、变速、生成式补帧为后续范围 |
| ADR011 | Team使用同Flash的独立上下文 | 分工不等于更换模型；引入额外调度和预算成本，M5再实施 |
| ADR012 | 团队仅Coordinator提交修改 | 避免多角色并发写时间线，其他角色产出建议与证据 |
| ADR013 | 原始证据和冲突都保留 | 不固定以声学、字幕或ASR其中一路压过其他来源 |
| ADR014 | 质量门槛与实现状态分开 | 文档不等于实测；真实API缺失只阻塞依赖能力，不伪造通过 |

## 2 当前源码基线

VideoMind参考提交为6bfbe9580271406fd0c2942f7800635d4a824e0e，审阅时main指向此提交。参考目录为src/dovideo。已观察到Planner/Executor/Critic式分析循环、证据工具、checkpoint与历史记录等基础。当前provider以结构化FINAL/TOOL_REQUEST JSON形成工具请求闭环，不能将它写成原生API tools协议已经完整接入。详见架构文档的具体文件与永久链接。[S02]

video-watch-plus参考提交为8ca266f9cf9e5ffce7e9d424aa0e937a897b608d。它提供FFmpeg量测、采样、拼图、连续帧、裁图和音频启发式检查；未提供我们目标中的完整长视频语义索引与剪辑运行时。其上游是CFITCorporation/video-watch-skill，与oxbshw/watch-skill不同。[S03][S04]

此次规范包没有运行上述应用的完整真实链路，也没有把其测试结果转写成本项目已通过的验收。Codex开始开发后应以实际checkout和可重现运行更新baseline report。

## 3 模型能力依据

截至文档日期，DeepSeek官方说明deepseek-flash支持图片输入、JSON输出和工具调用。模型alias背后的服务版本可能更新，应记录实际响应标识并运行合同测试。视觉接口按图片输入设计；本方案不假定直接上传整部MP4即可获得完整长视频理解。[S05][S06][S07]

本包不冻结模型价格、最大图片数或图片token常量。价格与限额在部署时从官方文档复核并写入有版本配置；实际usage作为计费统计依据。通用Flash不能被当作专用embedding API，视觉向量空间和ASR服务分别接入并验证。

## 4 待实施时冻结的决策

| 项目 | 默认方向 | 何时完成决策 |
|---|---|---|
| 目标checkout | 用户实际VideoMind仓库 | M0审计 |
| 图像embedding | 支持截图相似度；若需文字跨模态检索须同空间验证 | M2，开发集比较后记录模型ID、许可证、维度与资源 |
| ASR执行方式 | 优先复用现有服务，必要时独立faster-whisper等adapter | M2，结合语言和时间对齐要求 |
| 采样和片段粒度 | 60秒窗4+4；10秒片段、2秒重叠作为起点 | M2开发集，保持盲测隔离 |
| 队列和存储版本 | 沿用现有锁定配置 | M0，避免无关大升级 |
| 成本预算 | 用户/项目/run三级可配置 | M0/M3，按实际额度和usage |
| 发布环境 | 用户授权的环境 | M6；没有授权不对外发布 |

这些是有边界的实施决策，不应使Codex重新询问已经确定的产品目标。

## 5 主要来源

[S01] Spec Kit官方流程与模板：https://github.com/github/spec-kit 。README审阅blob为a4ea4f520add603de250c59f96037f0eb7be46e5。命令使用方式以已安装版本为准。

[S02] VideoMind参考代码：https://github.com/Morimi-Kazuha/VideoMind/tree/6bfbe9580271406fd0c2942f7800635d4a824e0e 。具体集成点见docs/03_ARCHITECTURE.md。

[S03] video-watch-plus代码：https://github.com/xinhailaopo/video-watch-plus/tree/8ca266f9cf9e5ffce7e9d424aa0e937a897b608d 。重点为vendor/vw.py、scripts/asr_hallucination_check.py、SKILL.md和README。

[S04] video-watch-plus上游归属：https://github.com/xinhailaopo/video-watch-plus/blob/8ca266f9cf9e5ffce7e9d424aa0e937a897b608d/NOTICE 。另一个检索参考项目：https://github.com/oxbshw/watch-skill 。实际引入第三方代码时保留所需许可与署名。

[S05] DeepSeek模型能力：https://api-docs.deepseek.com/quick_start/pricing/ 。

[S06] DeepSeek图片输入：https://api-docs.deepseek.com/guides/vision/ 。

[S07] DeepSeek工具调用：https://api-docs.deepseek.com/guides/tool_calls/ 。

[S08] learn-claude-code教学参考：https://github.com/shareAI-lab/learn-claude-code 。可参考团队任务、消息与工作流设计，不能把教学代码的进程状态当成生产持久化保证。

[S09] Pi代码参考：https://github.com/earendil-works/pi 。可参考agent loop与扩展接口，采用前仍需核对具体版本；本方案不要求替换VideoMind为Pi运行时。

## 6 ADR新增模板

记录编号、日期、问题、已知事实、候选方案、选择、受影响REQ/合同/任务、成本与风险、验收证据以及是否需要用户作出决定。不要把尚未测试的假设写为确认事实。
