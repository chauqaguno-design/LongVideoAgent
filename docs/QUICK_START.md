# CutSieve 本地快速开始

准备 Python 3.12、Node.js 22.12 或以上版本和 npm。复制 `.env.example` 为 `.env` 并填写 `CUTSIEVE_FFMPEG_DIR`（若 FFmpeg/ffprobe 已在 PATH 中可留空）与模型 API 配置；后端与前端开发服务器都会自动读取根目录 `.env`。

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

打开 `http://127.0.0.1:5175/`，注册并创建项目。在素材处理中上传一段带内嵌字幕的视频，等索引发布后搜索一句已知台词，回看原片并把片段加入时间线。可以预览并导出当前版本。

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
