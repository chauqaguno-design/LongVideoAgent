# 媒体上传、处理与证据

## 从文件到可回看的笔记

1. `POST /v1/projects/{project_id}/assets` 登记文件大小、文件名与可选 SHA-256，返回上传地址。大文件按至少 8 MiB 分片。客户端可读取 `/v1/uploads/{id}`、刷新剩余分片的地址，再提交有序 ETag。
2. 服务验证字节数、SHA-256 并用 ffprobe 读取真实视频轨。提交回执、不可变媒体版本和首次处理任务在一次数据库事务中完成。相同回执可以重放。原来的 `/media/*` 分片 API 仍可用，导入时先校验旧素材的所有权。
3. 解码器读取视频和音频的原始时间基，选取实际展示帧，保存实际 PTS、源时间和帧摘要。每 60 秒预留四个基础采样位置，再从该窗口的画面变化中最多追加四帧。额度不足时先覆盖全片。推断事件和检索使用 10 秒、重叠 2 秒的区间。非浏览器可播放的输入另生成 MP4 代理与时间映射。
4. 保留封装中的文字字幕，另行运行配置好的本地语音转写和原图 OCR。每个 VAD 语音区间单独转写并还原到原声时间。当转写只给出段落时间时，使用同一段音频和文本进行声学强制对齐；缺少可用音频或对齐失败时保留段级精度。OCR 保存识别框和裁图。相邻帧中位置、文字相同的字幕可归并，分开的两次同一句台词保留各自的来源时间。同一片源可以同时展示文字字幕与原声转写。
5. 解码、语音、文字和视觉观察各自登记覆盖与错误。帧的存在只能证明设备解码了这个位置。只有启用本地视觉适配器并实际推理才会产生人物、场景的模型观察。未启用的能力以及实际提取失败会显示原因。
6. 工作进程按租约领取任务，每个窗口与阶段有持久检查点。新版本写入独立空间，校验文件摘要、证据归属和区间后原子发布。搜索可固定版本；源删除立即撤回访问，阻止正在构建的结果重新发布。任务终态与 outbox 通知在同一事务提交。
7. `GET /v1/assets/{id}/notes` 返回笔记文件 ID，可通过 `GET /v1/artifacts/{id}` 打开。每条陈述携带子证据、来源及原片起止时间。章节暂以五分钟聚合，不会把间隔中没有证据的部分自动补为剧情。

运行中的渲染每半秒检查数据库中的任务状态及持有者。用户取消或删除源素材会停止当前 FFmpeg/ffprobe、撤销本次发布；服务正常停机则让同一任务排队，由下一台 worker 凭原时间线版本重新开始。真实子进程终止及复跑记录见 `work/spec-evidence/processing/render-cancel.xml`。

导出的 MP4 经完整解码后，结构校验还会读取实际封装字幕轨，逐条比对原计划的显示时间。语义检查从成片中抽取真实帧，另外记录这份文件的 SHA-256、工件 ID 和时间线版本。开发样本的目标是画面由红转蓝，在 0.25–0.75 秒显示字幕。相同时间线的故障样片得到下列结果。

| 实际成片 | 结构校验 | 对红转蓝的画面复核 |
| --- | --- | --- |
| 正常导出 | 通过 | 支持 |
| 画面倒序 | 通过 | 冲突 |
| 字幕整体晚一秒 | 字幕时间不符 | 支持 |
| 缺失输出配置要求的音轨 | 音轨缺失 | 支持 |

复核器读取实际帧中首尾的颜色，适用于这组人工编写的真值样片。正式的开放式影片内容仍需真实 Flash 复核。可运行 `python -m pytest -q tests/spec_004/test_output_qa.py` 复测，结果见 `work/spec-evidence/render-qa-fixtures/test_playable_wrong_event_and_0/dual-qa.json`。

对相同素材重建索引时，视觉窗口会核对原片摘要、各帧像素摘要与时间、模型、服务路由、提示与响应格式版本。与上个已发布版本完全一致的窗口可以直接读取已校验的原始回复，按真实帧映射生成本次证据，并沿用原始生成记录。更改视觉提示只会使对应视觉缓存失效。四次构建的实际重用和计数见 `work/spec-evidence/processing/caption-cache.xml`。

## 代码入口

| 用途 | 文件与入口 |
| --- | --- |
| 旧上传与身份 | `src/dovideo/infrastructure/media/uploads.py` 中的 `ChunkUploadService`，`presentation/api/r2_runtime.py` 中的 `ProductionR2Services` |
| 新上传、兼容导入、原片授权 | `application/edit_sources.py` 中的 `EditSources`，`presentation/api/edit_sources.py` 中的 `install_source_routes` |
| 存储与恢复 | `infrastructure/upload_parts.py` 和 `infrastructure/object_sources.py`，本地模式用文件，R2 用 MySQL 分片回执、MinIO 原片及分片对象、本地解码缓存 |
| 租约、进度与原子发布 | `infrastructure/persistence/processing_worker.py` 中的 `ProcessingWorker`，`processing_catalog.py` 中的 `MediaCatalog` |
| 抽帧、证据与笔记 | `infrastructure/media/source_artifacts.py`、`processing_units.py`、`local_inference.py`、`application/media_memory.py` |
| 合同与版本 | `presentation/api/media_processing.py`、Alembic `0005`、`0007`、`0011`；依赖共同的 outbox 表 `0010` |

## 本机启动与验收

FFmpeg、ffprobe 通过 `DOVIDEO_FFMPEG_DIR` 或分别指定可执行文件。配置 `DOVIDEO_MEDIA_PYTHON` 为已安装 faster-whisper 和 PaddleOCR 的解释器，`DOVIDEO_WHISPER_DIR` 为本地转写模型目录，`DOVIDEO_PADDLE_OCR_DIR` 为现有 Paddle 模型目录。已部署的本地编码服务通过 `VIDEOMIND_SEARCH_ENCODER_URL` 提供空间说明，只有确实生成并校验向量的路径才能标记可用。

`DOVIDEO_MAX_MEDIA_FRAMES` 控制单任务采样预算，默认 4096 帧。`DOVIDEO_LOCAL_FLASH_INFERENCE=1` 只对已通过预检的本机 Flash 服务生效，另受 `DOVIDEO_MEDIA_VISUAL_MAX_WINDOWS` 限制，默认 8 个窗口。继续使用本地配置时，在线供应商调用数为零。

```powershell
python -m pytest -q tests/media_ingestion tests/multimodal_memory
python scripts/verify_local_processing.py C:/path/to/sample.mp4 work/spec-evidence/local-sample
python scripts/verify_media_services.py
```

验收用 Docker 脚本使用本机已有镜像和动态回环端口，运行结束后清理本次创建的三个容器。它会执行真实 MySQL 迁移、Redis 身份及分片测试，验证 MinIO 中的原片能从空解码缓存恢复。结果见 `work/spec-evidence/processing/services.json`。样片、OCR 和两小时测试分别见旁边的 `processing-sample`、`processing-ocr`、`two-hour` 目录。两小时素材是人工生成的稀疏帧视频，用来检验全片覆盖，耗时不代表真实电影的处理速度。

三台 R2 实例可以有不同的磁盘缓存。部署时为所有副本配置同一个至少 32 字节的随机 `DOVIDEO_MEDIA_SIGNING_KEY`。源文件、代理、帧、字幕轨和渲染结果均已验证能从第三台空缓存节点读取。实际迁移验收覆盖从 0012 回退到 0004 后重新升级。

### 按需启用付费视觉观察

默认不调用在线视觉模型。管理员须先配置现有 Flash 供应商的 HTTPS 服务及路由，并通过与 endpoint、model 和 transport 绑定的图像/JSON/工具预检。随后可设定 `DOVIDEO_MEDIA_REMOTE_FLASH=1`，手动创建包含 `visual_observation` 的任务。若希望每次上传均使用该能力，另设 `DOVIDEO_MEDIA_AUTO_VISUAL=1`。本地推理仍使用独立的 loopback 开关。

远端请求会在传输前检查 `DOVIDEO_MEDIA_VISUAL_MAX_CNY_MICROUNITS` 和 `DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS`，及 `DOVIDEO_FLASH_PRICE_VERSION`、`DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION`、`DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION`。微元是人民币的一百万分之一。每窗口最多 8 张真实采样帧和 768 输出 token。通过 `GET /v1/processing-runs/{id}/usage` 可查看已结算和待核对金额；网络中断后保留原预占金额和请求记录。OpenRouter 调用沿用预检中的 provider 限制。

远端路径已通过本地协议和记账故障测试，本轮没有向在线模型提交媒体。验收状态详见 `work/spec-evidence/processing/ledger.json`。本机仍缺真实通过预检的 Flash 服务。真实文本加图像双空间在 MySQL/Qdrant 的切换及跨缓存读取详见 `work/spec-evidence/search/server-qdrant/mysql-generation.json`。独立电影盲测另行登记。

65 秒的已知双声道样本在左声道约 1 秒、59 秒分别说了一次同一句话，右声道持续播放合成旋律。实测转写给出 0.72–2.04 秒、59.12–60.02 秒两个区间，见 `work/spec-evidence/processing-mixed-audio/quality.json`。可用 `python scripts/verify_media_audio.py spoken.wav output --phrase "Open the yellow door"` 重新验证。默认上传时长上限由 `DOVIDEO_MAX_MEDIA_DURATION_SECONDS` 控制，初始 86400 秒；单帧像素上限为四千万。

另一段三秒素材同时含英文原声和低对比度中文字幕。16 像素、28 像素的两组试验均独立得到原声 “Open the yellow door.”（0–1.02 秒）和画面文字“请把钥匙放在桌上”（0.375–2.625 秒）。每条观察各自带有定位和原始证据。可使用 `scripts/verify_media_ocr.py` 复测。已知原文及结果见 `work/spec-evidence/processing-av-small/quality.json`。

本机 Whisper 使用 CTranslate2 对已有台词和真实音频进行对齐。原始 JSON 中的 `word_timing` 标注识别器时间、声学对齐或段级降级。实测只有段级时间的输入恢复了四个词及各自的边界；片源平移十秒后再对齐，保留了正确的偏移。纯静音和无语音纯音的两个控制样本均返回空转写。结果见 `work/spec-evidence/processing/asr-alignment.json`，可用专用媒体解释器执行 `scripts/verify_media_alignment.py` 复测。
