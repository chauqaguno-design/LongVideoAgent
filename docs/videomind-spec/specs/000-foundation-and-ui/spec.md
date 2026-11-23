# 基础环境与产品界面规格

本规格覆盖M0公共基础与跨阶段界面，需求为REQ-PRJ-001、REQ-UX-001、REQ-UX-002、REQ-UX-003、REQ-OPS-001、REQ-OPS-006；具体产品行为见docs/01_PRD.md与02_UX_FLOWS.md。

Given存在用户未提交的仓库改动，When Codex开展审计，Then保留改动并记录实际基线，不清空工作树。Given用户已上传源片，When进入项目素材页，Then可获取合法播放artifact并显示真实处理能力。Given后台阶段失败，When刷新页面，Then从服务端恢复任务和能力状态，不错误显示全部完成。Given收到旧revision预览，When新版本已成为head，Then保留其历史价值并明确旧预览，不能自动覆盖当前预览。

验收覆盖新浏览器进入项目、未知处理总量、空结果、部分覆盖、SSE断线、版本冲突、模型能力未配置与取消请求。界面展示业务状态与必要证据，不暴露内部prompt或秘密配置。
