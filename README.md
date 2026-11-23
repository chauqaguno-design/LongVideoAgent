# ReelCue · 镜寻剪辑台

ReelCue 是一个可以边查边剪的视频工作台。选中一部视频，输入“哪一段说了『Wait a minute』？”，点时间戳回看原片。接着说“把刚才第一段剪出来”“前面再留两秒，然后导出”，工作台会沿用这次选中的片段和剪辑版本。

## 在工作台里能做什么

- **上传和读笔记。** 在「素材处理」上传视频，查看处理阶段和缺口。观影笔记把已取得的字幕、转写、画面文字和画面观察关联到原片时间。
- **查台词和画面。** 在「检索片段」输入文字，或上传截图并圈选局部。候选带有原片区间和证据，可以回看，也可以加入时间线。对已提取的内嵌字幕，可按原文核查出现次数和范围。
- **在对话里交代任务。** 每段对话选定一部原片。系统判断需要检索、核对画面还是生成剪辑，并显示执行计划。查到多个符合的片段时，可回复“第 2 段”继续。
- **调整和导出。** 在聊天中要求调整起止点，也可以在「时间线」手动排列片段、设置原声或静音、添加字幕。预览和导出都绑定到具体版本，默认成片为 MP4，也可选择 WebM 或 MKV。

当前动作复核最多检查检索返回的三个候选。查询全片首次出现或总次数时，可以从结果中查看实际覆盖范围。[检索和证据说明](docs/development/search.md)

## 本地启动

准备 Python 3.12、Node.js 22.12 或以上版本，以及 PATH 中的 FFmpeg 和 ffprobe。下例使用 Windows PowerShell，默认在本机保存媒体与任务。

```powershell
git clone https://github.com/chauqaguno-design/LongVideoAgent.git ReelCue
cd ReelCue
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -c constraints-py312.txt -e ".[test]"
.venv/Scripts/python -m uvicorn dovideo.presentation.api.app:create_app --factory --host 127.0.0.1 --port 8173
```

另开终端，在仓库根目录执行前端命令。

```powershell
cd client
npm ci
$env:VITE_DEV_PROXY_TARGET = 'http://127.0.0.1:8173'
npm run dev -- --host 127.0.0.1 --port 5175
```

打开工作台后，注册本地账号，创建素材项目。在「素材处理」上传一段带内嵌字幕的视频，待索引发布后就可以在「对话与任务」里查台词并剪辑。[打开本地工作台](http://127.0.0.1:5175/)

纯本地模式可以读取内嵌字幕、处理明确时间段并运行剪辑。语音转写、OCR、向量编码和在线画面复核需要各自的模型或服务。配置示例列出对应的环境变量。[配置示例](.env.example) · [运行与恢复](docs/development/operations.md)

## 代码与验证

前端使用 Vue 3 和 Vite，API 使用 FastAPI。Python 工作流持久化对话、候选证据、预算和版本，FFmpeg 负责预览与成片。[对话路由与工作流](docs/development/workflow.md) · [前端与浏览器测试](client/README.md)

```powershell
.venv/Scripts/python -m pytest -q
cd client
npm test
npm run test:components
npm run build
```

## 项目来源与许可

ReelCue 基于 Morimi-Kazuha 的 VideoMind 继续开发。前端沿用了 DOVideo-AI 的部分代码，原始 MIT 版权声明为 Copyright (c) 2026 Majst。[上游 VideoMind](https://github.com/Morimi-Kazuha/VideoMind) · [前端来源](client/NOTICE.md) · [MIT 许可](LICENSE) · [第三方声明与字体许可](THIRD_PARTY_NOTICES.md)
