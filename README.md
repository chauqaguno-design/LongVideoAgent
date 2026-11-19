# 映溯 CineTrace

在浏览器中上传一部 MP4，查看台词、字幕和画面动作，点击任何一条证据即可跳回原片。Python 后端同时提供网页和 API，前端使用原生 HTML、CSS、JavaScript。

## 本机启动

在仓库根目录打开 PowerShell，使用 Python 3.11 或 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m cinetrace
```

打开 [本地视频工作台](http://127.0.0.1:8791/)。上传完成后可关闭网页，后台进程会继续构建时间线。重新打开页面可查看历史素材。修改 `.env` 后重新启动服务。Linux / macOS 可使用 `.venv/bin/python` 执行相同模块。

## 选择模型

| 功能 | 配置 |
|---|---|
| 语音 | 默认使用本机 CPU 上的 faster-whisper `turbo`。首次加载会下载权重。可把 `CINETRACE_WHISPER_MODEL` 设为 `base` 或已下载的模型目录。 |
| 动作 | 配置同一百炼工作空间的 `DASHSCOPE_BASE_URL` 和 `DASHSCOPE_API_KEY`，系统会把移除音轨的短片发给 `qwen3-vl-plus`。需要在本机运行时，可用下方 SmolVLM2 配置。原片始终保存在本机。 |
| 字幕 | 自动读取内封文本字幕。烧录或位图字幕可通过 `CINETRACE_OCR=1` 启用本地 PP-OCRv5。 |

本地动作模型可以分析无声、无字幕的视频。工作台将英文画面观察离线翻译为简体中文，同时更新场景摘要中的动作描述。在动作证据下点「查看英文原文」即可核对。已上传的素材可直接查看，译文缓存在本机 SQLite 中。动作时间标到片段范围，人物关系和短动作可回看原片核对。

```powershell
.\.venv\Scripts\python -m pip install -r requirements-local-vision.txt
.\.venv\Scripts\hf download HuggingFaceTB/SmolVLM2-256M-Video-Instruct --exclude 'onnx/*' --local-dir data/models/SmolVLM2-256M-Video-Instruct
.\.venv\Scripts\hf download Helsinki-NLP/opus-mt-en-zh --revision 408d9bc410a388e1d9aef112a2daba955b945255 --include '*.json' '*.spm' 'pytorch_model.bin' --local-dir data/models/opus-mt-en-zh
```

在 `.env` 中设置 `CINETRACE_LOCAL_VISION=data/models/SmolVLM2-256M-Video-Instruct`。要启用烧录字幕识别，安装 `paddlepaddle==3.2.2` 和 `paddleocr==3.3.2`，再设置 `CINETRACE_OCR=1`。PP-OCRv5 的检测和识别权重会在首次使用时下载。完成配置后可在原任务上点击「重试未完成的处理」，复用已完成的轨道。

## 从上传到回看

1. 浏览器逐片校验和上传，每片 5 MiB。中断后重新选择同一文件即可续传。服务端校验分片、合并原片、验证可解码，再安排处理。
2. 单个后台 Worker 将轨道进度和时间戳写入 SQLite。重启后，任务从已保存的位置继续。某一轨缺模型或失败时，其他轨道照常可用。
3. 时间线以原片毫秒为准。每个约 30 秒的检索块引用原始证据，相邻块组成场景目录。场景提要从关联证据中摘录。页面支持从场景展开到检索块、筛选证据和定位播放。

本地业务数据在 `data/`。运行一个 API 进程即可同时提供上传和处理，播放器通过 HTTP Range 读取原片。

## 可选服务

- **Kafka**。设置 `CINETRACE_KAFKA=localhost:9092` 并安装 `confluent-kafka`。上传完成和重试时，SQLite 事务会同时标记待发通知；后台向 `cinetrace.context` 投递并在收到通知时唤醒 Worker。单机模式直接消费 SQLite 队列。
- **Qdrant**。设置 `CINETRACE_QDRANT_URL=local`，向量会写入本机 `data/qdrant/`；也可填已启动的 Qdrant HTTP 地址。启用后为每个检索块分别编码有内容的语音、字幕、动作描述，以及原始视频画面。索引结果包含影片、构建版本、来源和时间范围，重试按已有点 ID 补写。

使用 Qwen 2B 向量时，先在同一虚拟环境安装 `requirements-local-vision.txt`，再执行下面两行。将 `CINETRACE_EMBED_MODEL` 设置为下载目录，并重启服务。向量模型约 4.3 GB。

```powershell
.\.venv\Scripts\python -m pip install qdrant-client==1.20.0 qwen-vl-utils==0.0.14 accelerate==1.16.0
.\.venv\Scripts\hf download Qwen/Qwen3-VL-Embedding-2B --revision 9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda --local-dir data/models/Qwen3-VL-Embedding-2B
```

在已建好索引的素材详情接口 `GET /api/videos/{id}` 中复制影片 ID 和 `index.version`，即可在命令行按文字检索画面。结果是最多五个不同的检索块，带原片起止毫秒。

```powershell
$videoId = Read-Host '影片 ID'
$detail = Invoke-RestMethod "http://127.0.0.1:8791/api/videos/$videoId"
.\.venv\Scripts\python -m cinetrace.indexing $videoId $detail.index.version "有人走到桌旁" --source frames
```

## 测试与设计资料

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

用两小时样片实测过 103 个分片传到一半后恢复、整片摘要校验和片尾 Range 播放。15 秒短片实测过语音、静音画面描述、证据跳转和历史恢复。另用带内封字幕、带烧录中文字幕的样片分别验过两种字幕提取路径。

- [首版 PRD](docs/python-video-context-v1-prd.md) 包含两小时公版测试电影的下载地址和校验值。
- [模型调用指南](docs/python-video-context-model-quickstart.md) 演示如何从长片截取 15 秒，分别验证画面动作、语音、字幕和向量。
- [视觉调用示例](examples/describe_clip.py) 可把一段本地 MP4 交给百炼，返回动作在原片中的起止毫秒。
