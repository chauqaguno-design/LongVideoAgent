# ReelCue 工作台运行手册

## 启动本地工作台

项目使用 Python 3.12、FFmpeg、ffprobe 和 Vue。后端从进程环境读取设置。在本机仓库中，`work/local-media-env.ps1` 指向已安装的语音和 OCR 权重、字体与本地编码服务。其他机器可参照 `.env.example` 配置自己的路径。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -c constraints-py312.txt -e ".[test,search-index]" -r docs/videomind-spec/requirements-docs.txt
if (Test-Path ./work/local-media-env.ps1) { . ./work/local-media-env.ps1 }
.venv/Scripts/python -m uvicorn dovideo.presentation.api.app:create_app --factory --host 127.0.0.1 --port 8173 --no-access-log
```

另开终端启动前端。登录后打开项目，在「视频对话」里直接输入请求，或从视频工作台上传素材。同一账号可以在刷新后继续。[自动路由和对话接口](workflow.md)

```powershell
cd client
npm ci
$env:VITE_DEV_PROXY_TARGET='http://127.0.0.1:8173'
npm run dev -- --host 127.0.0.1 --port 5175
```

本地编码服务使用装有 PyTorch 和 Transformers 的 Python。模型目录传给独立进程。它公布各个空间的名称、指纹和维度。设置 `VIDEOMIND_SEARCH_ENCODER_URL` 后重新处理素材，创建带向量的新索引版本。设置 `VIDEOMIND_SEARCH_QDRANT_URL` 可连接 Qdrant 服务。词法检索能独立运行。[模型与检索实测](search.md)

```powershell
python scripts/serve_search_encoder.py --text C:/models/bge-m3 --vision C:/models/Qwen3-VL-Embedding-2B --port 18176
```

## 处理、预算与排错

上传完成后自动登记处理任务。页面显示哪些时间段完成解码、哪些有原文和转写、哪些实际经过画面观察。用同一幂等键重试提交会读回原回执。主动重新处理会生成新的候选索引版本，完成后再切换。每个渲染结果固定到时间线版本，输出映射保存源 PTS 和成片区间。[媒体管线](media.md)

「工作区服务状态」和登录后的 `GET /v1/readiness` 显示处理、工作流、渲染、事件队列的进程状态与本工作区计数。出现积压时先看工作进程是否在线，再查看具体任务的 stage、error、attempt 和事件记录。查询 `GET /v1/workflow-runs/{id}/events` 可从 Last-Event-ID 续读。失败的素材可以在「素材处理」重新提交。模型调用账目以提供方实际 usage 和锁定价格计算。中途断连且费用未确认时保留预占金额，可在 `/v1/processing-runs/{id}/usage` 或工作流预算页查看。

本地默认关闭付费调用。接入已授权的在线部署时，先设置该部署的凭据、价格版本、每百万输入及输出 token 的价格和费用上限。显式运行 `python -m dovideo.infrastructure.providers.flash_preflight --live` 会向该服务发送最多八个有计费可能的请求。预检通过后才启用所需的工作流或视觉开关。更换部署或适配器应重新预检。运行时可分别关闭 `DOVIDEO_WORKFLOW_LIVE_MODEL`、`DOVIDEO_MEDIA_REMOTE_FLASH` 和 `DOVIDEO_MEDIA_AUTO_VISUAL`。多角色调度开关是 `DOVIDEO_WORKFLOW_TEAM`。

要单独启用视频对话的模型调用，可设置 `DOVIDEO_CHAT_LIVE_MODEL=1`、`DOVIDEO_CHAT_MAX_COST_MICROUNITS` 和相应币种的工作区总额度。默认每轮为零。台词检索、候选裁剪和调整已有片段可在本地完成。模型分类与画面复核共用每轮预算。配置采用价格上界时，界面的金额为按该价格计算的估算。

## 数据恢复

**单机模式。** 停止 API 和后台 worker，再制作快照。备份中包含用户认证数据、原片、索引和成片，应按源数据的访问级别保存。恢复命令要求一个新的空目标路径，校验成功后原子发布。默认本地签名密钥不会随备份复制，恢复后重新登录以取得新的播放链接。

```powershell
python scripts/workspace_snapshot.py backup work/r1-media D:/VideoMind-backups/local-01
python scripts/workspace_snapshot.py restore D:/VideoMind-backups/local-01 D:/VideoMind-restored/local-01
```

**R2 模式。** 停止写入后保存 MySQL 一致性备份、MinIO 桶和已启用的 Qdrant 快照，以及部署配置、权重指纹和签名密钥。将 SQL 和对象恢复到新库和新桶，使用独立缓存目录。用原账号登录，查一条已知台词、获取旧 revision 的预览、核对 SHA-256 并再导出一次。独立 Docker 验收实际执行 MySQL 迁移往返、恢复 SQL 到新库、复制对象到新桶，并从空缓存做这些检查。真用户 Redis 会话和付费账目不应用开发演练中的数据覆盖。回退服务时先停接新任务，保留原片、SQL、对象及索引快照，再切回经过验证的服务版本。降级迁移会删除新表，只在演练副本上执行。

```powershell
python scripts/verify_media_services.py --extra-test tests/spec_004/test_workspace_budget.py --extra-test tests/spec_007/test_r2_restore.py --output work/spec-evidence/processing/restore-services.json
python -m pytest -q tests/spec_007/test_workspace_recovery.py
```

**保留期和空间回收。** 删除素材后立即撤销其回放与证据访问。服务清理处理产物，检查共享引用。渲染目录和远端对象清理由保留期控制。下面的命令只列出候选。确认后加 `--apply` 实际移除。R2 也可以用 `--r2` 取代本地工作目录。

```powershell
python scripts/cleanup_edit_files.py work/r1-media --retention-hours 24
```

## 故障与验收

在可独立销毁的本地 Docker 实例中，`scripts/verify_media_services.py` 使用已有 MySQL、Redis、MinIO 镜像和动态回环端口。RabbitMQ 重投测试使用 `VIDEOMIND_TEST_RABBIT_IMAGE=rabbitmq:4-management` 运行 `tests/spec_004/test_live_outbox.py`。已知样片用于功能回归，正式发布使用独立人工标注集。运行命令、里程碑状态和未满足的门槛见[当前验收记录](../videomind-spec/status/PROGRESS.md)。

以下命令从仓库根目录运行。先将 FFmpeg 和 ffprobe 所在目录加入 PATH。浏览器验收使用独立测试账号，`VIDEOMIND_EDIT_SAMPLE` 指向本地视频，`VIDEOMIND_TEST_QUOTE` 填入其中一句台词，`VIDEOMIND_QUERY_IMAGE` 指向该视频的截图。浏览器会核对原片的实际播放位置，以及下载成片的字节数和 SHA-256。

```powershell
python -m pytest -q
python scripts/verify_media_services.py --legacy --output work/spec-evidence/processing/legacy-services.json
npm --prefix client run test:e2e:project
```

API 的定点复现使用测试账号环境变量 `VIDEOMIND_TEST_USER` 和 `VIDEOMIND_TEST_PASSWORD`。项目 ID、媒体版本及台词来自要检查的素材。该脚本会建立问答与剪辑任务，读回生成的 MP4，并核对服务端解码报告与本地文件摘要。

```powershell
python scripts/verify_film_journey.py --project <project-id> --revision <media-revision-id> --quote "Wait a minute." --output work/spec-evidence/film-journey
```
