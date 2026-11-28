# CutSieve 本地快速开始

准备 Python 3.12、Node.js 22.12 或以上版本和 npm。可将 `.env.example` 复制为本地 `.env`。如果 FFmpeg 和 ffprobe 已在 PATH 中，`CUTSIEVE_FFMPEG_DIR` 可以留空。后端与前端开发服务器会读取根目录 `.env`。

## 配置对话模型

设置 `CUTSIEVE_MODEL_BASE_URL`、`CUTSIEVE_MODEL_MODEL=deepseek-v4-flash` 和 `CUTSIEVE_MODEL_API_KEY`。该部署需要支持 OpenAI 兼容的 `tools`、`tool_calls` 和工具消息中的 `image_url`。如果响应中的模型名与请求不同，可用 `CUTSIEVE_MODEL_VERSION` 设置期望的响应模型名。

有效模型配置默认启用对话。设置 `CUTSIEVE_WORKFLOW_LIVE_MODEL=0` 会关闭模型调用，对话任务将返回 `MODEL_NOT_CONFIGURED`。素材处理、索引检索、手动剪辑和导出仍可使用。

`CUTSIEVE_HARNESS_MAX_STEPS` 默认是 15，分别限制每轮模型调用数和工具调用数。`CUTSIEVE_HARNESS_MAX_IMAGES` 默认是 24，包含附图和抽帧。可选的 Flash 输入与输出费率用于显示估算费用。

## 启动工作台

在仓库根目录打开 PowerShell，启动一个后端进程。

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -c constraints-py312.txt -e ".[test]"
& .\.venv\Scripts\python.exe -m uvicorn cutsieve.presentation.api.app:create_app --factory --host 127.0.0.1 --port 8173 --workers 1
```

另开一个 PowerShell，从仓库根目录启动前端。

```powershell
Set-Location client
npm ci
$env:CUTSIEVE_DEV_PROXY_TARGET = 'http://127.0.0.1:8173'
npm run dev -- --host 127.0.0.1 --port 5175
```

打开 `http://127.0.0.1:5175/` 进入本地工作区并创建项目。上传一段带内嵌字幕的视频，等索引发布后，在对话框搜索一句已知台词。也可以上传截图，结合时间戳回看候选片段。

对话中输入「把这句台词所在的片段剪出来并生成预览」，Agent 会保存时间线并提交 FFmpeg 任务。预览就绪后可继续要求调整时间线，或导出为 MP4、WebM 或 MKV。

数据默认保存在 `work/r1-media`。需要换目录时，在启动后端前设置 `CUTSIEVE_R1_MEDIA_DIR`。需要语音转写或 OCR 时，设置 `CUTSIEVE_WHISPER_DIR` 或 `CUTSIEVE_PADDLE_OCR_DIR`，并通过 `CUTSIEVE_MEDIA_PYTHON` 指定装有对应依赖的 Python。

## 启用 Qdrant 向量检索

以下示例使用 BGE-M3 文本权重和嵌入式 Qdrant。在装有 PyTorch、Transformers 和 Pillow 的 Python 环境中，从仓库根目录启动本地编码器。

```powershell
python scripts/serve_search_encoder.py --text 'C:/models/bge-m3' --port 18176
```

停止后端后，在同一终端安装 `search-index` 扩展并设置索引目录，再用第一节的 uvicorn 命令重启后端。

```powershell
& .\.venv\Scripts\python.exe -m pip install -c constraints-py312.txt -e ".[test,search-index]"
$env:CUTSIEVE_SEARCH_ENCODER_URL = 'http://127.0.0.1:18176'
$env:CUTSIEVE_SEARCH_QDRANT_PATH = 'work/r1-media/editing/qdrant'
```

重新处理已上传的素材以发布带向量的新索引。截图向量检索还需要用 `--vision` 加载本地视觉编码权重。

了解素材版本、证据和渲染的关系，见[架构](ARCHITECTURE.md)。
