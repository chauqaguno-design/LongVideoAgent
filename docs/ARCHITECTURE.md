# CutSieve 架构

CutSieve 是本地优先的视频检索与剪辑工作台。Vue 3 和 Vite 提供界面，FastAPI 提供 HTTP API。本地模式运行一个 API 进程，由它启动素材处理、工作流、渲染和事件投递循环。

工作台直接打开本地工作区。API 使用固定的工作区身份，在该工作区内按项目和素材版本限定操作。

## 存储与检索

- **SQLite** 保存项目、素材与索引版本、对话检查点、时间线和任务状态。默认数据根目录是 `work/r1-media`。
- **本地文件** 保存原片、处理产物、预览和成片。FFprobe 读取媒体信息，FFmpeg 提取素材并渲染输出。
- **Qdrant** 为已配置本地编码器的工作台提供向量索引，可使用嵌入式目录或 HTTP 服务。词法检索可独立查询已提取的字幕和转写。

## 从素材到证据

1. 上传完成后，后台处理为原片版本生成带时间戳的证据和覆盖记录。已启用的字幕、转写、OCR 和画面处理各自记录实际覆盖范围。
2. 检索限定在当前工作区、项目、原片版本和已发布的索引中。候选结果保留可回看的原片区间。查询首次出现或总次数时，结果会报告实际核查范围。

## 单循环原生工具调用

`POST /v1/projects/{project_id}/conversation-runs` 创建一轮对话。省略 `media_revision_id` 或传 `null` 时，搜索范围为项目中已可用的素材版本。`parent_run_id` 关联先前的对话与当前剪辑项目，`image_artifact_id` 将项目内上传的查询图片作为消息附件发给模型。

Agent 通过 OpenAI 兼容的 Chat Completions 接口向 `deepseek-v4-flash` 发送素材清单、当前时间线摘要、`editing_rules.md` 和 `visual_verification.md`。模型可直接回答，也可以在同一循环中调用以下工具。

| 工具 | 用途 |
| --- | --- |
| `get_media_overview` | 查看素材、处理状态、能力和观影笔记。 |
| `search_evidence` | 根据文字或截图查找有原片时间戳的证据。 |
| `read_transcript` | 按 180 秒窗口分页读取对齐的字幕、转写、OCR 与视觉观察。 |
| `inspect_frames` | 从原片或成片提取最多六张 JPEG。带时间戳的 `image_url` 直接放在工具消息中。 |
| `get_timeline` | 读取当前剪辑项目、头版本和片段。 |
| `edit_timeline` | 新建剪辑项目、原子替换片段或提交增量编辑。写入使用 `expected_revision` 乐观锁。 |
| `render_video` | 按已保存的时间线版本提交 FFmpeg 预览或导出。 |
| `ask_user` | 保存需要用户选择的问题和具体选项。 |

工具校验失败时，模型收到带 `code`、`message` 和 `hint` 的工具结果，可以调整参数再试。超过 8,000 字符的文本工具结果以工作流产物保存，模型上下文保留摘要和 `artifact_id`。

## 运行、恢复与事件

- SQLite 的 `vm_workflow_runs.checkpoint` 保存对话消息、工具步骤、证据、用户补充、等待中的工具调用和用量。Worker 使用租约和 fencing token 确定写入者。时间线变更、渲染入队及其检查点使用同一事务。
- `render_video` 让运行进入 `WAITING_JOB` 并释放租约。渲染队列发布已校验的产物后唤醒运行，工作流也会轮询。成功时发布 `ARTIFACT_READY` 并将结果交给模型，失败或取消的渲染也会返回结构化工具结果。
- `ask_user` 发布 `NEEDS_INPUT` 后进入 `AWAITING_INPUT`。客户端用 `expected_state_version`、`message` 和 `idempotency_key` 调用 `/v1/workflow-runs/{run_id}/resume`。回复写回原来的工具调用。可重试的失败使用同一端点。
- `/v1/workflow-runs/{run_id}` 提供状态与 `usage`，`/conversation` 提供对话链，`/result` 提供答案、证据、工具步骤和剪辑产物。`/events` 支持用 `Last-Event-ID` 继续读取序列化事件。`/cancel` 可取消运行及其等待中的渲染。
- 每轮默认限制模型调用和工具调用各 15 次、图片 24 张，执行期限为 300 秒。等待用户回复暂停计时。模型费用按可选费率估算，用于 `usage.estimated_cost_cny`。

启动后端、前端和可选的向量检索服务，见[本地快速开始](QUICK_START.md)。
