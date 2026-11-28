# CutSieve 架构

CutSieve 是本地优先的长视频证据查证与可控粗剪工作台。前端基于 Vue 3 + Vite（单屏暗房三栏布局 + 抽屉式素材笔记/运行轨迹/系统状态），后端基于单进程 FastAPI，内置素材处理、工作流 Harness、FFmpeg 渲染与 SSE 事件投递四个协作循环。

本地工作台直接免登录进入固定本地工作区，按项目与不可变素材版本（`media_revision_id`）严格隔离读写边界。

---

## 1. 存储与检索底座

- **SQLite（WAL 模式）**：持久化项目、媒体版本、处理覆盖账本、对话检查点（`vm_workflow_runs.checkpoint`）、不可变时间线版本与渲染任务队列，默认数据目录为 `work/r1-media`。
- **SQLite FTS5 全文倒排索引**：在 SQLite 连接上维护 `media_evidence_fts` 虚拟表（不侵入 SQLAlchemy `Base.metadata`，保持 Alembic 零漂移）。写入时同步对中文片段生成二元分词并保留原始词项，支持毫秒级中英文混合检索（离线评测均值 ~6.2 ms），在非 SQLite 环境或零命中场景自动回退内存 BM25。
- **本地文件系统**：存储原片、播放代理、代表帧、拼图核验图与预览/导出成片；FFprobe 探测真实 PTS/时间基/流信息，FFmpeg 执行抽帧与渲染。
- **可选 Qdrant 向量索引**：配置本地编码器与 Qdrant 目录/服务后，与 FTS5 词法结果及 8×8 空间色彩/图像特征通过 RRF 融合。

---

## 2. 长视频入库流水线（素材 → 证据）

1. **分片上传与媒体体检**：分片断点续传、`SHA-256` 校验、FFprobe 探测流元数据与试解码校验，不兼容浏览器播放的封装自动生成 H.264 MP4 代理。
2. **160px 镜头边界自适应采样与静止去重**：
   - 使用低分辨率（160px 宽）FFmpeg 场景变化扫描获取全片镜头边界，内置 `>= 0.8s` 防频闪合并；
   - 短镜头（`< 5s`）取镜头正中清晰帧（避开切镜首帧模糊），长镜头（`>= 5s`）按间隔保底抽帧，并利用轻量 8×8 RGB 帧间差值剔除连续静止重复帧，同时受全片帧数预算上限约束。
3. **三轨证据提取与本地离线 ASR**：
   - **台词轨**：优先提取内嵌字幕；无内嵌字幕且配置了 `CUTSIEVE_WHISPER_DIR`（默认直接使用当前 Python 环境中的 `faster-whisper` int8 CPU 推理，通过 WAV 适配器直读 worker 生成的 PCM 音频）时，自动执行离线语音转写并写入带时间戳的语义片段。
   - **画面文字与视觉观察**：按配置执行可选本地 OCR 或限量离线 VLM 观察，所有模态在覆盖账本中如实记录真实覆盖比例。
4. **FTS5 同步、观影笔记与原子发布**：按时间段生成结构化观影笔记、同步写入 FTS5 全文索引并原子发布不可变索引版本。

---

## 3. 单循环原生工具调用 Harness

`POST /v1/projects/{project_id}/conversation-runs` 启动一轮对话，通过 OpenAI 兼容的 Chat Completions 接口调用支持原生 `tools` 与 `role: "tool"` 多模态 `image_url` 的模型（默认 `deepseek-v4-flash`），附带素材清单、当前时间线头版本及两份精简规则文档（`editing_rules.md`、`visual_verification.md`）。

### 8 个正交工具

| 工具 | 核心职责与 TOP 2 增强 |
| --- | --- |
| `get_media_overview` | 查看项目素材清单、各模态覆盖状态、能力就绪情况与时间码观影笔记。 |
| `search_evidence` | 融合 SQLite FTS5、8×8 空间色彩/图像特征与可选 Qdrant 向量，返回带原片时间码的候选证据。 |
| `read_transcript` | 按 180 秒窗口分页读取对齐的字幕、离线 Whisper 转写、OCR 与视觉观察。 |
| `inspect_frames` | 从原片或成片提取 1–6 张带时间戳 JPEG；支持 `contact_sheet=True` 将多帧拼成带 `MM:SS.s` 角标的 1 张拼图（省视觉槽位）。 |
| `get_timeline` | 读取当前剪辑项目、`head_revision_no`、片段清单与 `audio_fade_ms` 策略。 |
| `edit_timeline` | 新建/原子替换/增量编辑时间线；自动兼容 `asset_id` / `media_revision_id`，支持 `audio_fade_ms`（`0..3000ms`）与 `expected_revision` 乐观锁；成功返回完整回执，失败返回实时 `valid_assets` / `head_revision_no` / `current_clips` 引导 1 步自修复。 |
| `render_video` | 基于已保存的时间线版本提交 FFmpeg 预览（MP4）或导出（MP4 / WebM / MKV），对非静音片段应用 `afade` 首尾淡入淡出并做解码验收。 |
| `ask_user` | 在需求真实存在歧义时挂起并向用户给出具体可选项。 |

### 上下文工程与防膨胀护栏

- **历史视觉帧自动卸载**：每轮请求模型前扫描 `checkpoint.messages`，仅保留用户首轮附图与最近一次工具回合产出的 `image_url`，将更早工具消息中的图片替换为紧凑时间戳文本占位并累加 `usage.images_unloaded`，记录 `IMAGE_UNLOAD` 轨迹事件。
- **原子事务历史压缩**：当消息条数超过 `CUTSIEVE_COMPACT_MAX_MESSAGES`（默认 18）或字符估算超过 `CUTSIEVE_COMPACT_CHAR_LIMIT`（默认 48,000）时，按完整 `assistant(tool_calls)` + 对应 `tool` 回复边界压缩中段历史为单条结构化摘要，累加 `usage.compactions` 并记录 `COMPACTION` 事件。
- **硬上限与大输出外置**：默认每轮限制 15 次模型/工具调用、24 张图片、300 秒执行墙钟（等待用户回复时暂停计时）；超 8,000 字符的工具文本自动外置为产物 ID。

---

## 4. 状态恢复、事件流与 Harness Trace 可观测性

- **检查点与租约恢复**：`vm_workflow_runs.checkpoint` 持久化完整对话消息、`tool_steps`（含 `step_index`、`args_preview`、`duration_ms`、`error_code`、`repaired`、`images_added`）、`trace_events`（`MODEL_TURN`、`IMAGE_UNLOAD`、`COMPACTION`、`SELF_REPAIR`、`RENDER_WAIT`）、`citations`、`usage` 与挂起状态；时间线写入、渲染入队与检查点在同一数据库事务提交。
- **异步渲染挂起唤醒**：`render_video` 将运行置为 `WAITING_JOB` 并释放租约；渲染 Worker 完成 FFmpeg 编码与解码校验后发布 `ARTIFACT_READY` 唤醒主循环继续向模型回报结果。
- **Trace API 与前端抽屉**：`GET /v1/workflow-runs/{run_id}/harness` 返回完整的 `status`、`usage`、`warnings`、`tool_steps` 与 `trace_events`，供前端 [HarnessTrace.vue](../client/src/editing/HarnessTrace.vue) 抽屉实时展示（同时向后兼容不含扩展字段的历史运行记录）。
- **自动化评测**：[eval_harness.py](../scripts/eval_harness.py) 提供 `--mode offline`（零费回归验证 FTS5、镜头采样、自修复、帧卸载压缩与音频淡入淡出）和 `--mode live`（真片全链路采集 `/harness` 指标生成 Markdown 报告）。

本地部署、离线 Whisper 配置与运行命令见 [本地快速开始](QUICK_START.md)。
