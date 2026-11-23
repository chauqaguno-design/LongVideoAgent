# 基础环境与界面任务

任务状态随本地实现和验收更新，执行记录见 status/PROGRESS.md。

### T00-001 审计实际仓库与参考基线

- [x] VERIFIED
- 里程碑：M0
- 需求：REQ-PRJ-001、REQ-OPS-006
- 依赖：无
- 实施：记录HEAD、工作树、AGENTS、分层、provider协议、既有测试与差异；不修改用户未提交文件。
- 产物：docs/development/baseline-report.md
- 验收：逐项给源码位置与实际命令/退出码；区分原有失败和新失败。

### T00-002 建立可复现开发与测试环境

- [x] VERIFIED
- 里程碑：M0
- 需求：REQ-OPS-004、REQ-OPS-006
- 依赖：T00-001
- 实施：复用锁文件与Compose，分离测试数据库，验证FFmpeg编码器、字体、对象存储和队列连接。
- 产物：env.example、开发启动与关闭入口
- 验收：新环境按README能启动现有基础服务；缺外部服务记录BLOCKED，不写入生产数据。

### T00-003 建立合同与验证入口

- [x] VERIFIED
- 里程碑：M0
- 需求：REQ-OPS-006、REQ-AGT-002
- 依赖：T00-001
- 实施：运行规范验证，接入OpenAPI/schema和provider schema bundling；把应用测试入口建成实际可执行命令。
- 产物：合同生成/检查脚本、正反例fixture
- 验收：Schema拒绝错误单位与额外字段；所有tool refs解析且provider_name唯一；文档验证和业务验证分开报告。

### T00-004 建立身份范围与资源解析公共边界

- [x] VERIFIED
- 里程碑：M0
- 需求：REQ-OPS-001、REQ-AGT-006
- 依赖：T00-002、T00-003
- 实施：复用现有认证，构造workspace context和受控artifact resolver；所有后续服务注入身份，不信任模型workspace参数。
- 产物：鉴权adapter、资源resolver、两workspace fixture
- 验收：伪造project/media/artifact身份被拒绝；公开链接短期有效，日志无密钥。

### T00-005 对齐项目与素材页面入口

- [x] VERIFIED
- 里程碑：M1
- 需求：REQ-UX-001、REQ-PRJ-001
- 依赖：T00-004、T05-001
- 实施：保留既有前端，接入服务端项目列表、素材版本与source/playback artifact；支持上传失败和代理准备状态。
- 产物：项目/素材页面增量改动
- 验收：刷新与新会话可从API恢复素材；源播放器不使用上传PUT URL。

### T00-006 展示分阶段处理与能力覆盖

- [x] VERIFIED
- 里程碑：M2
- 需求：REQ-UX-002、REQ-ING-004
- 依赖：T00-005、T01-011
- 实施：展示ASR/OCR/视觉/索引可用性、未知总量、时间覆盖缺口、处理历史与取消状态。
- 产物：任务中心和能力面板
- 验收：部分失败不显示全部完成；末尾未核查窗口可见；cancel_requested在刷新后保留。

### T00-007 完成文字截图与证据问答界面

- [x] VERIFIED
- 里程碑：M3
- 需求：REQ-UX-001、REQ-UX-003、REQ-RET-002、REQ-RET-006
- 依赖：T00-006、T03-009、T04-016
- 实施：串联截图上传、检索候选、答案主张、来源播放、冲突和全局限定；SSE恢复读取一致快照。
- 产物：检索问答工作台、浏览器E2E
- 验收：文字与图片命中点击回源；SSE重连无重复消息；topK截断不会显示完整事件总数。

### T00-008 完成自然语言编辑与版本反馈界面

- [x] VERIFIED
- 里程碑：M4
- 需求：REQ-UX-001、REQ-UX-003、REQ-EDT-006
- 依赖：T00-007、T05-017
- 实施：在已有M1 UI接入EditBrief、自然语言修改、约束冲突、compiled manifest映射和旧预览状态。
- 产物：编辑工作台与演示脚本
- 验收：修改后旧预览不顶替新版本；刷新后仍能选历史revision并继续编辑。

### T00-009 展示Team任务与预算状态

- [x] VERIFIED
- 里程碑：M5
- 需求：REQ-UX-002、REQ-TEAM-001
- 依赖：T00-008、T06-013
- 实施：展示角色任务、依赖、重试和可展示事件；不泄露内部推理或私有上下文；预算调整使用授权API。
- 产物：任务中心Team视图
- 验收：角色失败/接管可见，预算未知显示未知，模型不能通过工具提高预算。
