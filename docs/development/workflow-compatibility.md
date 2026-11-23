# 工作流与旧分析历史

参考提交 `6bfbe9580271406fd0c2942f7800635d4a824e0e`。旧分析在 `application/value_objects.py` 以整数素材 ID、目标和分析模式组成 TaskKey，由 `application/checkpoint_service.py` 和 `infrastructure/persistence/codec.py` 保存步骤结果。旧执行历史的记录格式为 `x2-b-record-v1`，支持 `agent-execution-semantics-v1` 和 `agent-execution-semantics-v2`，读取入口在 `application/historical_replay.py`。旧工具账本中 RESULT_STORED 和 CONSUMED 的含义仍由 `application/tool_state.py` 定义。

新接口使用 `/v1/workflow-runs` 和 UUID 标识。迁移 0009 保存工作流快照、事件游标、工具调用、产物、任务卡和信箱。每次写时间线前核对工作流租约和原时间线版本。旧的 `/analysis/executions/{execution_id}/replay` 仍使用原读取器。

`DOVIDEO_WORKFLOWS_ENABLED=false` 会关闭新任务的提交。运行中的作业继续执行，历史状态、结果与事件仍可按工作区读取。默认采用单工作流。设置 `DOVIDEO_WORKFLOW_TEAM=true` 才会为新任务建立按角色划分的持久任务板。项目、用户与任务均沿用原账户授权。

确定的原片时间区间和可逐字核对的台词可以在本机处理。远程模型路径须显式开启 `DOVIDEO_WORKFLOW_LIVE_MODEL=1`，需要相同部署的预检报告，并在选定币种下设置 `DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION`、`DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION` 及 `DOVIDEO_FLASH_PRICE_VERSION`。美元配置使用 USD 前缀。费率单位为每百万 token 的百万分之一货币单位。工作流金额上限默认是 0；真实模型采购和现场验证另行授权。

兼容性样本 `tests/spec_004/fixtures/legacy-checkpoint.json` 由参考提交的 JsonCheckpointCodec 编码。可与存量历史用例一同验证。

如需在已授权的部署使用模型，运营方还须配置工作区累计金额上限 `DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS`（USD 使用对应前缀）。默认值为 0。每次物理调用先占用工作区与当前运行的额度，服务返回用量后按固定价格版本结算。失去回执的费用继续占用额度。团队运行也可以通过 `DOVIDEO_WORKFLOW_TASK_MAX_MODEL_CALLS` 限制每个阶段的物理调用次数。

检索页可以把截图、文字、指定时间段、FIRST/ALL/COUNT 等条件一起提交至 `POST /v1/projects/{project_id}/search-workflows`。请求体含 `search` 和 `idempotency_key`。完成后从 `GET /v1/workflow-runs/{run_id}/result` 获取 AnswerResult，使用其中的 `complete_event_list_artifact_id` 分页读取匹配记录。删除任一引用原片会撤回相关工作流和此前签发的结果链接。

本地事件由 SQL outbox 投递。配置 `DOVIDEO_WORKFLOW_OUTBOX_TRANSPORT=rabbitmq` 和 `DOVIDEO_BROKER_URL` 可使用持久 RabbitMQ 队列。消息只携带事件 ID，消费者从数据库加载工作区和资源。队列中断时，工作进程仍会扫描已提交的检查点。

开启 Team 后，明确的时间与台词任务可继续在本机执行。若同时启用已验证的模型部署，协调器确认目标，素材成员提交带证据引用的交接记录，编辑成员给出提案，复核成员读取该版本的成片校验。四个角色绑定同一部署。每个成员只接收本阶段输入和上一阶段的交接产物。编辑成员尝试写时间线时，服务会返回工具拒绝。协调器负责按原版本提交。

处理中可通过 `GET /v1/workflow-runs/{run_id}/jobs/{job_id}` 查询该运行依赖的索引、视觉复看或渲染作业。复看会保存原片范围、任务轮次和原始序列引用。一次复看得到的帧数不足以判断动作时，作业以 `PARTIAL` 结束。工作进程的排他领取、心跳和接管使用数据库时间。

```powershell
python -m pytest tests/spec_004/test_history_compatibility.py tests/application/test_x2c_historical_replay.py tests/infrastructure/test_checkpoint_repository_8a.py
```
