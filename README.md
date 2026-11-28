# CutSieve · 剪筛工作台

CutSieve 是一个本地优先的长视频多模态检索与粗剪工作台。上传视频后自动提取时间对齐的字幕、语音、画面文字与采样帧；你可以用自然语言查找台词或无声画面，核实原片时间戳后直接在版本化时间线上粗剪、预览并导出成片。

## 核心架构

默认以一个本地 API 进程运行。

```text
Vue 3 前端（对话工作台 / 播放器 / 时间线）
              │ HTTP + SSE
              ▼
FastAPI 应用进程（本地 SQLite WAL + 本地磁盘 + 可选 Qdrant）
  ├─ 素材离线处理：分片续传 → FFprobe PTS 校准 → 播放代理 / 抽帧 / 字幕 / Whisper / OCR / 向量
  ├─ 多路混合检索：词法检索 + 空间图像特征 + 可选 Qdrant 向量（RRF 融合）
  └─ 单循环原生工具调用 Agent（deepseek-v4-flash）
        ├─ 八个工具覆盖检索、读稿、看帧、读写时间线、渲染与澄清
        ├─ SQLite 检查点保存对话、工具调用和证据，支持重启续跑
        ├─ 不可变版本化时间线（expected_revision 乐观锁 + 回滚）
        └─ FFmpeg 异步渲染，通过 SSE 发布时间线和成片
```

- 本地 SQLite（WAL 模式）保存项目、媒体索引版本、对话检查点和时间线快照。原片、代理与成片从本地磁盘读取。Qdrant 支持本地文件目录或独立服务。
- 对话支持直接附上截图，也能在工具结果中查看 FFmpeg 抽取的源画面或成片帧。超过 8,000 字符的文本工具结果保存为 JSON 产物，对话保留摘要和产物 ID。
- 每轮默认最多 15 次模型调用、15 次工具调用和 24 张图，执行期限为 300 秒，等待用户回复期间暂停计时。配置费率后，运行状态会显示估算费用。

更多模块设计与数据流见 [系统架构说明](docs/ARCHITECTURE.md)。

## 1 分钟本地启动

需要 **Python 3.12+**、**Node.js 22.12+**，以及 PATH 中的 **FFmpeg / ffprobe**（或通过 `CUTSIEVE_FFMPEG_DIR` 指定目录）。

### 1. 启动后端

使用对话工作台前，按 `.env.example` 配置 `CUTSIEVE_MODEL_BASE_URL`、`CUTSIEVE_MODEL_MODEL` 和 `CUTSIEVE_MODEL_API_KEY`。所选部署应支持原生工具调用和 `role: "tool"` 中的 `image_url`。

```powershell
git clone https://github.com/chauqaguno-design/CutSieve.git
cd CutSieve
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints-py312.txt -e ".[test]"
.\.venv\Scripts\python.exe -m uvicorn cutsieve.presentation.api.app:create_app --factory --host 127.0.0.1 --port 8173 --workers 1
```

### 2. 启动前端

```powershell
cd client
npm ci
$env:CUTSIEVE_DEV_PROXY_TARGET = 'http://127.0.0.1:8173'
npm run dev -- --host 127.0.0.1 --port 5175
```

打开 `http://127.0.0.1:5175/` 进入本地工作区。创建项目并上传视频，待索引发布后，可在对话中查找带时间戳的台词，或者让 Agent 保存粗剪并按请求预览或导出。预览使用 MP4，导出支持 MP4、MKV 和 WebM。

- 完整启动步骤与可选本地向量编码服务见 [本地快速开始](docs/QUICK_START.md)。
- 可选本地 Whisper / PaddleOCR / Qdrant 及在线视觉模型配置见 [环境变量示例](.env.example)。

## 验证与测试

```powershell
# 后端回归
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src scripts alembic

# 使用新的 SQLite 文件验证迁移
$env:CUTSIEVE_DATABASE_URL = "sqlite:///work/verify-$([guid]::NewGuid()).db"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic check

# 前端单测、组件测试与生产构建
cd client
npm test
npm run test:components
npm run build
```

## 当前能力边界

- 无模型部署时，分片上传、素材处理、索引检索、手动时间线编辑和 FFmpeg 导出可在本机运行。对话任务会返回 `MODEL_NOT_CONFIGURED`。
- 语音转写、画面识字、语义向量和以图搜帧向量可按 `.env.example` 配置 Faster-Whisper、PaddleOCR、Qdrant 和本地编码器。素材处理阶段的远程视觉观察默认关闭。

## 许可

本项目基于 [MIT License](LICENSE) 开源。
