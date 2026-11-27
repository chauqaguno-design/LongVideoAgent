# CutSieve 前端

Vue 3 / Vite 暗房剪辑工作台客户端，打开即进入本地工作区。客户端提供对话式检索与剪辑、素材离线处理监控、多模态证据检索、可视化比例时间线与预览导出。完整产品说明和后端启动步骤见 [根目录 README](../README.md) 与 [本地快速开始](../docs/QUICK_START.md)。

```bash
npm ci
npm run dev
```

开发代理默认指向 `http://127.0.0.1:8000`；更改后端地址可设置 `CUTSIEVE_DEV_PROXY_TARGET`。独立部署前端时可设置 `VITE_API_BASE_URL`。测试与构建命令为 `npm test`、`npm run test:components` 与 `npm run build`。

## 核心工作流

- **对话与任务**：首页和 `/#edit` 默认打开对话工作台，支持多轮上下文、任务计划展示、候选片段点击回看原片、澄清追问、预览播放与成片下载，刷新可恢复最近 20 轮记录。
- **素材处理**：支持大文件分片上传与断点续传，展示各模态能力状态、处理阶段进度、画面分析用量账本与带时间戳的观影笔记。
- **检索片段**：支持台词关键词、原文精确核查、截图上传及像素区域（ROI）检索，右侧提供粘滞式证据原片播放器。
- **时间线**：左侧原片监视器支持精确到微秒的入/出点取点，右侧提供比例可视化轨道条、版本历史回滚、字幕策略切换以及 MP4 / WebM / MKV 预览与导出。

## 浏览器端到端验收

浏览器端到端验收使用独立的本地工作区，每次运行会创建测试项目。

- 运行 `npm run test:e2e:edit` 前设置 `CUTSIEVE_EDIT_SAMPLE`（至少 4 秒视频）与 `CUTSIEVE_EDIT_UI`（默认 `http://127.0.0.1:5175`），即可验证上传、两段剪辑、改序、刷新恢复、预览播放、导出与版本回滚。
- `npm run test:e2e:conversation`：基于回环地址的 `CUTSIEVE_EDIT_UI`，用 FFmpeg 生成带重复台词的测试片，验证多轮查询、澄清、预览与导出。
- 架构说明见 [ARCHITECTURE.md](../docs/ARCHITECTURE.md)。
