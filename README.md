# 映溯 CineTrace

在浏览器中上传一部 MP4，查看台词、字幕和画面动作，点击任何一条证据即可跳回原片。Python 后端同时提供网页和 API，前端使用原生 HTML、CSS、JavaScript。

## 用自然语言在一部电影里找片段

电影检索入口是 `python -m cinetrace.film`，可以直接处理本地 MP4，生成中文概述、事件时间线和可跳转的 HTML。这个入口只依赖 Python 库和模型 API。

| 环节 | 第一版选型 | 作用 |
|---|---|---|
| 画面 | 百炼 `qwen3-vl-plus`，通过 OpenAI Python SDK 调用 | 每 30 秒建一个窗口，每 5 秒取一帧，记录环境、人物、物件、动作和互动。查询时以 4 fps 回看候选片段。 |
| 影片资料和文本 | DeepSeek `deepseek-flash`，使用同一 SDK | 根据片名、台词等线索查找带引用的片目资料，合并相邻事件、概括全片并扩展查询词。 |
| 媒体和台词 | PyAV、FFmpeg、faster-whisper | 优先使用指定的 SRT 或内封文本字幕，再复用原片已有的转写或在本机转写。无音轨也能建画面索引。 |
| 存储和检索 | SQLite、Python 词项匹配 | 保存原片时间、观察和处理进度。按人物别名、物件、动作与台词找到候选；稀有词和整段短语的匹配权重更高。 |
| 操作与回看 | Python 命令行、原生 HTML 和 JavaScript | 在终端建立或查询索引，打开 HTML 点击时间即可播放原片对应区间。 |

在仓库根目录使用 Python 3.11 或 3.12。首次安装只需电影入口的依赖。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-film.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

打开根目录的 `.env`，填写百炼的 `DASHSCOPE_API_KEY`、同地域工作空间的 OpenAI 兼容地址 `DASHSCOPE_BASE_URL`，以及 `DEEPSEEK_API_KEY`。两个服务各自使用自己的 Key。`plan` 只探测片长和窗口数，可以先运行。[百炼视觉接口](https://help.aliyun.com/zh/model-studio/vision) · [DeepSeek 联网工具](https://api-docs.deepseek.com/zh-cn/guides/web_tools/)

```powershell
$filmPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CineTrace-15s.mp4'
.\.venv\Scripts\python -m cinetrace.film plan "$filmPath"
.\.venv\Scripts\python -m cinetrace.film index "$filmPath" --no-web --html data/films/sample.html
.\.venv\Scripts\python -m cinetrace.film find "$filmPath" "画面里有人在交谈的片段在哪里？" --budget 3 --html data/films/find.html
Start-Process data/films/find.html
```

要处理整部电影，将 `$filmPath` 换成原片路径。已知片名时，可将示例中的 `--no-web` 换成 `--title "Meet John Doe (1941)"`，让 DeepSeek 核对公开资料。已有时间对齐的字幕可加 `--srt .\movie.srt`。公开资料能提供人名和别名；模型使用片段内身份线索判断谁在场，证据不足时保留衣着、位置等描述。

每部电影的索引存在 `data/films/`。重复运行同一条 `index` 命令会复用已完成的窗口。运行 `timeline "$filmPath" --html data/films/sample.html` 子命令可离线重建报告。整两小时对应 240 个视觉观察请求，另有资料查询与文本整理调用；每次提问的 `--budget` 只限制候选视频复核次数。返回的区间覆盖模型回看的原片，5 秒采样之间的短动作可能在初次检索时漏掉。

## 启动本地上传工作台

在仓库根目录打开 PowerShell，使用 Python 3.11 或 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python -m cinetrace
```

打开 [本地视频工作台](http://127.0.0.1:8791/)。上传完成后可关闭网页，后台进程会继续构建时间线。重新打开页面可查看历史素材。修改 `.env` 后重新启动服务。Linux / macOS 可使用 `.venv/bin/python` 执行相同模块。

## 选择模型

| 功能 | 配置 |
|---|---|
| 语音 | 默认使用本机 CPU 上的 faster-whisper `turbo`。首次加载会下载权重。可把 `CINETRACE_WHISPER_MODEL` 设为 `base` 或已下载的模型目录。 |
| 动作 | 本地 Qwen3-VL-2B-Instruct 比较镜头内的连续画面，用中文描述人物、可见动作和物体交互。SmolVLM2 提供轻量的代表帧描述。配置百炼的 `DASHSCOPE_BASE_URL` 和 `DASHSCOPE_API_KEY` 后，系统优先把静音片段发给 `qwen3-vl-plus`。 |
| 字幕 | 自动读取内封文本字幕。烧录或位图字幕可通过 `CINETRACE_OCR=1` 启用本地 PP-OCRv5。 |

本地模式先检测硬切，再把长镜头均分为最多 4 秒的小段。每条证据的时间是模型看过的片段范围。在「证据 → 动作」中展开「对照原片画面」，可以查看并点击片段前、中、后部的三张原始画面。已有素材可点「重新分析画面」，复用原片、语音和字幕，更新动作证据、场景和索引。两小时影片至少需要 1800 个这样的窗口，适合先用短片评估速度和质量。

```powershell
.\.venv\Scripts\python -m pip install -r requirements-local-vision.txt
.\.venv\Scripts\hf download Qwen/Qwen3-VL-2B-Instruct --revision 89644892e4d85e24eaac8bacfd4f463576704203 --local-dir data/models/Qwen3-VL-2B-Instruct
```

在 `.env` 中设置 `CINETRACE_LOCAL_VISION=data/models/Qwen3-VL-2B-Instruct` 并重启。模型权重约 4.3 GB。若选择轻量模型，将该变量指向下方的 SmolVLM2 目录。它从每个小段选择一张代表帧，英文描述由本机翻译并缓存；「查看英文原文」可用于对照。

```powershell
.\.venv\Scripts\hf download HuggingFaceTB/SmolVLM2-256M-Video-Instruct --exclude 'onnx/*' --local-dir data/models/SmolVLM2-256M-Video-Instruct
.\.venv\Scripts\hf download Helsinki-NLP/opus-mt-en-zh --revision 408d9bc410a388e1d9aef112a2daba955b945255 --include '*.json' '*.spm' 'pytorch_model.bin' --local-dir data/models/opus-mt-en-zh
```

要启用烧录字幕识别，安装 `paddlepaddle==3.2.2` 和 `paddleocr==3.3.2`，再设置 `CINETRACE_OCR=1`。PP-OCRv5 的检测和识别权重会在首次使用时下载。完成配置后可在原任务上点击「重试未完成的处理」，复用已完成的轨道。

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
