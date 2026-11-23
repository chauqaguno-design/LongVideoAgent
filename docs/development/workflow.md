# 视频工作流运维

在视频工作台上传原片，然后在「视频对话」选中这部视频，直接输入请求。例如“保留原声，将第 1 至 3 秒和第 7 至 9 秒依次拼接并导出”。两个选区都使用原片时间，输出绑定本次生成的时间线版本。[启动工作台](operations.md)

## 一句话怎么分配给不同角色

| 用户输入 | 执行计划 | 返回内容 |
| --- | --- | --- |
| 哪一段说了「Wait a minute」？ | 检索 → 回答 | 可回看的台词候选和原片时间 |
| 他开门时手里拿着什么？ | 检索 → 核对画面 → 回答 | 画面证据，证据不足时标明缺口 |
| 把刚才第二段剪出来 | 读取上轮候选 → 剪辑 → 预览 | 引用同一原片区间的剪辑版本 |
| 找到他冲出房门的镜头，剪出来 | 检索 → 核对画面 → 剪辑 → 预览 | 有画面依据的片段，或需要用户确认的候选 |
| 前面再留两秒，然后导出 | 读取上一版 → 修改起点 → 预览 → 导出 | 新版本和可下载的成片 |

路由先识别明确指令、引号中的台词和对上一轮结果的引用。目标仍不明确时，启用在线模型并设置正数费用上限的部署可以请求一次意图分类。模型只能返回意图，服务端决定可用工具、素材范围和剪辑版本。“刚才那段”对应多个候选时，对话会先请用户选一段，保留原来的剪辑要求。切换视频会开始新对话。

## 提交、查看和恢复

| 操作 | 接口及行为 |
| --- | --- |
| 发起或接续对话 | `POST /v1/projects/{project_id}/conversation-runs` 接受 `message`、`media_revision_id` 和 `idempotency_key`。追问带上上一轮的 `parent_run_id`。 |
| 恢复对话 | `GET /v1/workflow-runs/{run_id}/conversation` 按时间顺序返回当前分支最近 20 轮的消息、执行计划、澄清记录、候选和版本引用。 |
| 提交任务 | `POST /v1/workflow-runs` 接受 `intent`、`message`、`scope` 和 `idempotency_key`，返回 202 与 `run_id`。修改已有剪辑时传入 `edit_project_id`。 |
| 核查一次检索 | `POST /v1/projects/{project_id}/search-workflows` 保存文字、截图、时间范围及 FIRST、ALL 或 COUNT 模式。完整匹配清单由结果里的 `complete_event_list_artifact_id` 指定。 |
| 查看进度 | `GET /v1/workflow-runs/{run_id}` 返回状态、阶段、预算及待完成作业。向同一路径的 `/events` 发起已认证的 SSE 请求。重连带上 `Last-Event-ID`，游标过期时重新读取快照。 |
| 补充或改目标 | `AWAITING_INPUT` 时向 `/resume` 提交回答、当前 `expected_state_version` 和幂等键。已结束的剪辑可基于原编辑项目开启下一轮。要替换进行中的目标，在新请求中用 `supersedes` 指定原任务及版本。 |
| 取得成片 | 成功后读取 `/result`。预览取 `preview_artifact_id`，明确要求导出的任务另有 `export_artifact_id`、`export_job_id` 和 `export_validation_report_id`。下载时用导出 ID 请求 `GET /v1/artifacts/{id}`，核对 `kind=EXPORT` 和 `timeline_revision_id=result.revision_id`，打开返回的短期 URL。 |

任务从 `QUEUED` 进入 `RUNNING`。处于 `WAITING_JOB` 时，到 `/jobs/{job_id}` 查看索引、视觉复核或渲染进度。复核只取得一帧时可返回 `PARTIAL` 并说明证据缺口。取消请求提交到 `/cancel`，已发出的模型请求可能仍待结算。以最终快照判断是否已取消。`FAILED` 的 `error.retryable` 为真时可以恢复。

刷新网页后，任务区从项目历史重新加载快照和结果。服务重启后，其他工作进程可在数据库租约到期时接管，从检查点和已提交的操作回执继续。旧工作进程的令牌无法写入新时间线。源视频删除后，已有导出、工作流结果及签名链接会重新检查来源权限。

## 工具、角色与预算

语义剪辑通过工具循环生成 EditBrief。模型提出调用，服务端校验注册表中的工具名、参数、角色和素材范围，再回填工具结果。规划最多六轮，无效格式只修复一次。确认选段后，服务提交带原版本号的剪辑操作，调用 FFmpeg 渲染，最后检查实际成片。原生工具回填使用提供方的调用 ID，JSON 工具模式使用结构化请求。

视频对话中的普通问答直接检索。需要核对画面或修改时间线时，系统按计划创建分工卡。设为 `DOVIDEO_WORKFLOW_TEAM=false` 可关闭分工。通过显式 `intent` 接口创建的任务保持原默认值，可用 `DOVIDEO_WORKFLOW_TEAM=true` 开启分工。启用在线模型时，四个角色共用同一部署。选材角色从原片和转写中提取证据，编辑角色编排片段，协调角色提交版本，审阅角色核查输出。分工与交接记录可从 `/team-tasks` 和 `/mailbox-events` 查询。

单次运行默认最多调用模型 8 次、工具 64 次、传入 32 张图，时限 900 秒，金额上限为 0。启用在线服务需要设置 `DOVIDEO_WORKFLOW_LIVE_MODEL=1`、该部署的预检报告、工作区及运行金额上限，并配置对应币种的输入输出费率和价格版本。工作区、任务和物理请求共享预算，未知费用保留为待核账。调整运行上限通过 `PATCH /v1/workflow-runs/{run_id}/budget` 提交新限额、原因、状态版本和幂等键。[具体环境变量](workflow-compatibility.md)

排查模型超时时，可在事件流用 `request_id` 对应 `model.reserved` 和 `model.failed`，读取模型版本、价格版本和延迟。提供方尚未返回用量时，`charged_microunits` 为空，`uncertain_microunits` 保留预留金额。排查导出耗时，可用 `render_job_id` 将 `render.submit` 对应到 `render.finished`，再由 `validation_report_id` 读取 FFmpeg 编码和校验耗时 `render_latency_ms`。[本地实测记录](evidence/usage-trace.json)

## 复现与验收范围

在仓库根目录使用配置好 FFmpeg 和 ffprobe 的 Python 3.12 环境执行。第一条读取已保存的部署报告。显式加 `--live` 将进行可能计费的在线预检。

```powershell
python -m dovideo.infrastructure.providers.flash_preflight
python -m pytest -q tests/spec_004/test_workflow_http.py tests/spec_005/test_intelligent_edit.py
python scripts/verify_media_services.py --extra-test tests/spec_004/test_workspace_budget.py --output work/spec-evidence/workflow/mysql.json
```

本机验收实际生成并下载了 MP4，也验证了对话中纠正台词、替换末段和恢复版本。四角色权限实验使用受控适配器，记录了 Editor 越权被拒后的重新提案。真实服务专项 16 项通过，覆盖 MySQL 8.0.43 上的预算竞争、服务进程被终止和隔离 Redis 清空后的恢复。另用 RabbitMQ 4.4.0 验证了队列重启和确认丢失后的重复投递。外部模型调用为 0。自由描述动作的盲测准确率、真实模型成本及单工作流与 Team 的质量差异仍待测量。[逐项验收记录](../../work/spec-evidence/workflow/ledger.json) · [真实服务记录](../../work/spec-evidence/workflow/mysql.json)
