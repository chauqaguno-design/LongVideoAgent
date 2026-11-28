# CutSieve 本地快速开始

准备 **Python 3.12+**、**Node.js 22.12+**、npm，以及 **FFmpeg / ffprobe**。将 `.env.example` 复制为仓库根目录的 `.env` 后，后端与前端开发服务器会自动加载。若 FFmpeg 不在系统 PATH 中，请在 `.env` 设置 `CUTSIEVE_FFMPEG_DIR`。

---

## 1. 配置对话模型

在根目录 `.env` 中配置支持原生 `tools` / `tool_calls` 与 `role: "tool"` 内联 `image_url` 的 OpenAI 兼容接口（默认适配 `deepseek-v4-flash`）：

```dotenv
CUTSIEVE_MODEL_BASE_URL=https://api.deepseek.com/v1
CUTSIEVE_MODEL_MODEL=deepseek-v4-flash
CUTSIEVE_MODEL_VERSION=deepseek-flash
CUTSIEVE_MODEL_API_KEY=你的APIKey
CUTSIEVE_MODEL_VISION=1
CUTSIEVE_MODEL_NATIVE_TOOLS=1
```

常用 Harness 可选开关：
- `CUTSIEVE_WORKFLOW_LIVE_MODEL=0`：显式关闭在线模型（此时上传、检索、手动时间线编辑与导出仍可用，对话返回 `MODEL_NOT_CONFIGURED`）。
- `CUTSIEVE_HARNESS_MAX_STEPS=15` / `CUTSIEVE_HARNESS_MAX_IMAGES=24`：每轮最大步数与图片数上限。
- `CUTSIEVE_COMPACT_MAX_MESSAGES=18` / `CUTSIEVE_COMPACT_CHAR_LIMIT=48000`：上下文历史自动压缩触发阈值。
- `CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION` / `CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION`：配置单价后在界面与「运行轨迹」抽屉中显示估算费用（CNY）。

---

## 2. 配置本地感知

在仓库根目录创建 `.venv` 并安装后端、本地感知和嵌入式 Qdrant 的依赖。FTS5 随 SQLite 使用。

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -c constraints-py312.txt -e ".[test,search-index]" faster-whisper rapidocr-onnxruntime
```

将 `Systran/faster-whisper-tiny`（约 75 MB）和 `OFA-Sys/chinese-clip-vit-base-patch16` 的模型文件分别放入 `work/models/faster-whisper-tiny` 与 `work/models/chinese-clip-vit-base-patch16`。RapidOCR 使用软件包自带的 PP-OCRv4 ONNX 权重。

另开一个 PowerShell，在仓库根目录用装有 PyTorch、Transformers 和 Pillow 的 Python 环境启动本地编码器。若复用 `.venv`，可在该虚拟环境额外安装 `torch` 和 `transformers`，并用 `.venv\Scripts\python.exe` 运行同一脚本。

```powershell
python scripts/serve_search_encoder.py --vision work/models/chinese-clip-vit-base-patch16 --port 18176
```

`GET http://127.0.0.1:18176/spaces` 应返回唯一的 `vision` 空间，`dimensions` 为 `512`。向量索引使用 `qdrant-client` 直接写入本地目录，无需 Docker。

## 3. 启动后端与前端

回到后端 PowerShell，在仓库根目录设置本地感知变量并启动单进程服务。也可将这些变量保存到自己的 `.env` 供后续启动复用。

```powershell
$env:CUTSIEVE_WHISPER_DIR = 'work/models/faster-whisper-tiny'
$env:CUTSIEVE_OCR_ENGINE = 'rapidocr'
$env:CUTSIEVE_SEARCH_ENCODER_URL = 'http://127.0.0.1:18176'
$env:CUTSIEVE_SEARCH_QDRANT_PATH = 'work/r1-media/editing/qdrant'
& .\.venv\Scripts\python.exe -m uvicorn cutsieve.presentation.api.app:create_app --factory --host 127.0.0.1 --port 8173 --workers 1
```

Worker 默认复用后端的 `.venv`。文件默认保存到 `work/r1-media`，可用 `CUTSIEVE_R1_MEDIA_DIR` 指定其他位置。

另开一个 PowerShell 启动前端开发服务器：

```powershell
Set-Location client
npm ci
$env:CUTSIEVE_DEV_PROXY_TARGET = 'http://127.0.0.1:8173'
npm run dev -- --host 127.0.0.1 --port 5175
```

打开 `http://127.0.0.1:5175/`，在 **「系统状态」** 中确认 ASR、OCR、向量检索和 FFmpeg 均已就绪。也可查看 `GET http://127.0.0.1:8173/v1/capabilities`，其中 `embedding` 应为 `configured: true`、`ready: true`、`verified: true`，且 `versions` 仅含 `vision`。

1. 点击顶栏 **「+ 添加视频」** 上传素材，在 **「素材与笔记」** 查看镜头采样、索引进度与时间码观影笔记；
2. 在右侧对话助手中用自然语言查证台词或画面，点击带时间戳的引用胶囊即可让左上角监视器跳转到原片对应位置；
3. 让 Agent 执行跨时段粗剪、设置 `300ms` 音频淡入淡出、静音或导出 MP4 / WebM / MKV；
4. 点击顶栏 **「运行轨迹」** 或对话卡片上的 **「轨迹」**，查看每步工具耗时、自修复标记、帧卸载/压缩次数与生命周期事件。

---

## 4. 运行自动化回归与 Harness 评测

```powershell
# 1. 后端完整单测与集成测试
& .\.venv\Scripts\python.exe -m pytest -q

# 2. 离线 Harness Eval（0 API 费用，验证 FTS5、镜头采样、自修复、帧卸载/压缩与音频淡入淡出）
& .\.venv\Scripts\python.exe scripts\eval_harness.py --mode offline

# 3. 真片 Live Eval（消耗少量 Flash API，输出完整 Markdown 指标报告）
& .\.venv\Scripts\python.exe scripts\eval_harness.py --mode live --video work\samples\blender_cinema_27m.mp4 --out work\eval_report.md

# 4. 前端单测 + 组件测试 + 生产构建
Set-Location client
npm test
npm run build
```

---

## 5. 可选扩展 BGE-M3 文本向量

数百小时以上的片库可以把本地 BGE-M3 权重放入 `work/models/bge-m3`，然后用同一个端口重启编码器并额外启用 `text` 空间。

```powershell
python scripts/serve_search_encoder.py --vision work/models/chinese-clip-vit-base-patch16 --text work/models/bge-m3 --port 18176
```

在「素材与笔记」中重新处理现有素材，即可发布含新增向量空间的索引。日常单片的台词、转写与画面文字检索使用 FTS5，视觉召回使用 Chinese-CLIP。更多细节见 [架构说明](ARCHITECTURE.md)。
