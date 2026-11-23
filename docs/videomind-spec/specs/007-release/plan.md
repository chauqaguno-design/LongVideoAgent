# 发布与质量实施计划

以docs/08_VERIFICATION_RELEASE.md为唯一质量门槛说明，traceability/acceptance-matrix.json为逐项验收入口。合成真值fixture早于复杂模型开发建立；开发集与盲测集分离。媒体验证同时检查容器解码与语义约束，不能由生成答案的同一次模型自评替代独立审查。

按领域、集成、媒体、provider live、浏览器E2E、质量基准、故障注入、恢复演练逐层进行。每个结果记录commit、环境、配置、模型版本、命令、退出码与artifact。基准固定后报告分组和失败案例，不为了通过而改变任务定义。

部署沿用既有基础设施，锁定版本，先在测试副本迁移和恢复。完成出站效果对账与清理策略后才能扩大并发。最终release report显式列出尚未通过的能力，只有强制AT全部通过才标记RELEASE_READY。
