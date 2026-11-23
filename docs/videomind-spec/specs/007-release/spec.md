# 发布验收规格

关联REQ-OPS-001至REQ-OPS-006以及全部最终业务需求。Given应用实现完成，When执行发布检查，Then每条强制AT都必须有实际运行证据；NOT_RUN、FAILED或BLOCKED不能映射成通过。Given质量或时延未达拟定门槛，When生成release report，Then保留原阈值并报告差距。

Given重复消息、过期worker和数据库/对象存储之间的崩溃窗口，When恢复任务，Then不重复发布可见成片、不覆盖新revision、不丢失已提交证据。Given从空环境恢复备份，When打开项目，Then能访问合法源、证据、旧版本和导出，并能重新渲染。

发布范围包含运行代码、测试、迁移、配置、部署和恢复手册、真实demo及质量报告。外部发布是否执行仍以用户授权和环境权限为准。
