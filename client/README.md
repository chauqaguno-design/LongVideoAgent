# CutSieve 剪筛工作台

Vue 3 / Vite 单屏暗房工作台。桌面端左侧为原片与成片监视器、单轨时间线，右侧为智能剪辑助手。窄屏按监视器、时间线、助手的顺序纵向排列。

```bash
npm ci
npm run dev
```

开发代理默认指向 `http://127.0.0.1:8000`。可用 `CUTSIEVE_DEV_PROXY_TARGET` 改地址。独立部署可设置 `VITE_API_BASE_URL`。后端启动见 [本地快速开始](../docs/QUICK_START.md)。

## 剪辑流程

- 在顶栏新建或选择项目，点击「添加视频」。上传可暂停，重新选择同一个文件可续传。素材条显示处理状态，点击素材可切到原片。
- 监视器可播放原片或成片预览。用入点、出点和原声或静音设置加入片段。第一次加入片段会自动创建剪辑方案。
- 时间线支持比例轨道、改序、替换、移除、版本恢复和字幕输出策略。可生成 MP4 预览，或导出 MP4、WebM、MKV。点击轨道片段或源片映射可回看对应区间。
- 在助手中描述台词、画面或剪辑要求，也可附带截图。发送时使用所选素材范围和时间线方案。点击原片引用可在监视器播放该区间，点击澄清选项可直接继续任务。助手提交时间线或完成渲染时，工作台会读取相应的当前版本和产物。
- 「素材与笔记」提供处理进度、重试、取消、观影笔记与原始观察。「系统状态」显示后台队列和最近任务的 Token 与费用估算。

`/#edit?project=…&edit=…&run=…` 链接可恢复项目、剪辑方案和对话。

## 验证

```bash
npm test
npm run test:components
npm run build
node --check e2e/edit.mjs
node --check e2e/project.mjs
node --check e2e/conversation.mjs
```

浏览器端到端用例需要运行中的独立本地工作区。每次运行会新建测试项目，默认访问 `http://127.0.0.1:5175`，可用 `CUTSIEVE_EDIT_UI` 指定地址。

- `npm run test:e2e:edit` 验证手动剪辑、预览、导出、版本恢复与响应式布局。设置 `CUTSIEVE_EDIT_SAMPLE` 为至少四秒的样片。
- `npm run test:e2e:project` 验证素材笔记、引用回看、导出和系统状态。设置 `CUTSIEVE_EDIT_SAMPLE` 和片中台词 `CUTSIEVE_TEST_QUOTE`。可用 `CUTSIEVE_QUERY_IMAGE` 一并验证截图查询。
- `npm run test:e2e:conversation` 用 FFmpeg 生成含重复台词的样片，验证多轮查询、澄清选项、预览与导出。对话用例需要启用 Agent Harness 的模型服务。
