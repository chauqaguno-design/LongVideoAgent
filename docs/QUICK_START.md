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

## 2. 启动后端与前端

在仓库根目录启动后端（单进程内置处理、工作流、渲染与事件投递循环）：

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -c constraints-py312.txt -e ".[test]"
& .\.venv\Scripts\python.exe -m uvicorn cutsieve.presentation.api.app:create_app --factory --host 127.0.0.1 --port 8173 --workers 1
```

另开一个 PowerShell 启动前端开发服务器：

```powershell
Set-Location client
npm ci
$env:CUTSIEVE_DEV_PROXY_TARGET = 'http://127.0.0.1:8173'
npm run dev -- --host 127.0.0.1 --port 5175
```

打开 `http://127.0.0.1:5175/` 即可进入本地工作台：
1. 点击顶栏 **「+ 添加视频」** 上传素材，在 **「素材与笔记」** 查看镜头采样、索引进度与时间码观影笔记；
2. 在右侧对话助手中用自然语言查证台词或画面，点击带时间戳的引用胶囊即可让左上角监视器跳转到原片对应位置；
3. 让 Agent 执行跨时段粗剪、设置 `300ms` 音频淡入淡出、静音或导出 MP4 / WebM / MKV；
4. 点击顶栏 **「运行轨迹」** 或对话卡片上的 **「轨迹」**，查看每步工具耗时、自修复标记、帧卸载/压缩次数与生命周期事件。

---

## 3. 启用本地离线 Faster-Whisper 语音转写（无字幕视频推荐）

对没有内嵌字幕的录屏、访谈或会议视频，只需在当前 `.venv` 安装 `faster-whisper` 并下载轻量模型（如 `Systran/faster-whisper-tiny`，约 75 MB）：

```powershell
& .\.venv\Scripts\python.exe -m pip install faster-whisper
```

然后在 `.env` 中指向本地模型目录（未指定 `CUTSIEVE_MEDIA_PYTHON` 时，Worker 默认复用当前 `.venv` 的 Python）：

```dotenv
CUTSIEVE_WHISPER_DIR=C:/path/to/faster-whisper-tiny
```

重启后端后访问 `GET http://127.0.0.1:8173/v1/capabilities`，看到 `ASR` 的 `ready: true` 即生效。数据默认保存在 `work/r1-media`（可通过 `CUTSIEVE_R1_MEDIA_DIR` 修改）。

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

## 5. 可选：启用 Qdrant 语义/跨模态向量检索

在装有 PyTorch、Transformers 与 Pillow 的环境中启动本地编码服务：

```powershell
python scripts/serve_search_encoder.py --text 'C:/models/bge-m3' --port 18176
```

在 `.venv` 中安装 `search-index` 扩展并配置环境变量后重启后端：

```powershell
& .\.venv\Scripts\python.exe -m pip install -c constraints-py312.txt -e ".[test,search-index]"
$env:CUTSIEVE_SEARCH_ENCODER_URL = 'http://127.0.0.1:18176'
$env:CUTSIEVE_SEARCH_QDRANT_PATH = 'work/r1-media/editing/qdrant'
```

在界面中重新处理素材即可发布带向量的新索引。更多内部机制见 [架构说明](ARCHITECTURE.md)。
