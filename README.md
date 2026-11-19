# 映溯 CineTrace

LongVideoAgent 是一个面向长视频素材的 Python 项目。首版规划把一部电影的台词、字幕和可见动作放到同一条时间线上，从场景摘要可以一路跳回原片证据。

当前仓库提供需求草案和可单独运行的视觉模型示例。上传服务、后台处理和检索页面处于规划阶段。

- [首版 PRD](docs/python-video-context-v1-prd.md) 描述上传、三轨时间线、检索块、存储与验收方式。文末附两小时测试电影的下载地址和校验值。
- [模型调用指南](docs/python-video-context-model-quickstart.md) 演示如何从长片截取 15 秒，分别验证画面动作、语音、字幕和向量。
- [视觉调用示例](examples/describe_clip.py) 可把一段本地 MP4 交给百炼，返回动作在原片中的起止毫秒。
