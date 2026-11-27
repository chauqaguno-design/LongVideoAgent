# CutSieve · 剪筛工作台

CutSieve 是一个本地优先的长视频多模态检索与粗剪工作台。上传视频后自动提取时间对齐的字幕、语音、画面文字与采样帧；你可以用自然语言查找台词或无声画面，核实原片时间戳后直接在版本化时间线上粗剪、预览并导出成片。

## 核心架构

零重型中间件依赖（无需 MySQL / Redis / RabbitMQ / MinIO），默认本机单进程启动：

```text
Vue 3 前端（对话工作台 / 播放器 / 时间线）
              │ HTTP + SSE
              ▼
FastAPI 应用进程（本地 SQLite WAL + 本地磁盘 + 可选 Qdrant）
  ├─ 素材离线处理：分片续传 → FFprobe PTS 校准 → 播放代理 / 抽帧 / 字幕 / Whisper / OCR / 向量
  ├─ 多路混合检索：词法检索 + 空间图像特征 + 可选 Qdrant 向量（RRF 融合）
  └─ 单协调器工作流状态机（PLAN → MATERIAL → PROPOSE → COMMIT → RENDER → REVIEW）
        ├─ 只读工具隔离与工作区/素材作用域校验
        ├─ 持久化 Checkpoint + Worker 租约防崩溃双写 + 幂等键
        ├─ 不可变版本化时间线（expected_revision 乐观锁 + 回滚）
        └─ FFmpeg 预览/导出渲染 + 独立上下文语义验收与最多 2 轮有界局部替换
```

- 本地 SQLite（WAL 模式）保存项目、媒体索引版本、对话检查点、预算账本与时间线快照。原片、代理与成片直读本地磁盘。Qdrant 支持本地文件目录或独立服务。
- **确定性上下文与成本控制**：大工具结果落库、入窗截断预览，上下文超限时保留冻结约束与授权引用编号并支持前后 15 秒按需回读；模型调用前先做预算预扣与额度结算。

更多模块设计与数据流见 [系统架构说明](docs/ARCHITECTURE.md)。

## 1 分钟本地启动

需要 **Python 3.12+**、**Node.js 22.12+**，以及 PATH 中的 **FFmpeg / ffprobe**（或通过 `CUTSIEVE_FFMPEG_DIR` 指定目录）。

### 1. 启动后端

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

打开 `http://127.0.0.1:5175/` 即可进入本地工作区。创建项目并上传视频，待索引发布后即可检索台词、预览并导出（支持 MP4 / MKV / WebM）。

- 完整启动步骤与可选本地向量编码服务见 [本地快速开始](docs/QUICK_START.md)。
- 可选本地 Whisper / PaddleOCR / Qdrant 及在线视觉模型配置见 [环境变量示例](.env.example)。

## 验证与测试

```powershell
# 后端回归与迁移零漂移检查
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src scripts alembic

# 前端单测、组件测试与生产构建
cd client
npm test
npm run test:components
npm run build
```

## 当前能力边界

- **纯本地离线模式**：无需外部 API Key，可完成分片上传、内嵌字幕读取、明确时间段与精确台词剪辑、版本回滚及 FFmpeg 导出。
- **可选多模态增强**：语音转写（Faster-Whisper）、画面识字（PaddleOCR）、语义/以图搜帧向量（Qdrant + 本地编码器）及在线视觉复核需按 `.env.example` 配置对应本地权重或 OpenAI 兼容模型接口。
- **已知边界**：当前离线视觉观察默认关闭以控制费用；在线候选复核每次默认最多检查前 3 个候选区间，小时级长视频的镜头级抽帧、动态变焦复核与持久化全文索引正在后续迭代规划中。

## 许可

本项目基于 [MIT License](LICENSE) 开源。
