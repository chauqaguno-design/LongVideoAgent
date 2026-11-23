# 基础环境与界面实施计划

先记录实际checkout和既有架构，复用项目的包管理、前端路由、认证、媒体播放器与组件库。将workspace context与artifact resolver作为共享adapter，避免每个工具独立拼路径。建立合同校验、两workspace fixture和应用测试命令；业务实现使用现有分层，禁止把规范文件当作已运行应用。

界面按阶段接入服务端读模型。M1接入素材播放和显式区间编辑；M2显示处理和能力覆盖；M3接入截图/文字/问答与SSE；M4接入自然语言修改和版本反馈；M5显示Team任务。客户端以UUID、revision与sequence处理状态，不能只靠本地消息顺序。

所有交互以OpenAPI和共享Schema为合同。支持列表恢复、空/失败/部分状态；预算未知使用null而非0。测试包括组件状态和真实浏览器E2E，分开记录mock与实际API验证。
