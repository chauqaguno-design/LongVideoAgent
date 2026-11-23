# VideoMind 开发基线

2026 年 11 月 23 日。对应 T00-001、REQ-PRJ-001 与 REQ-OPS-006。

## 源码身份

工作目录为桌面 VideoAgent 下的独立 VideoMind checkout。上游为 `Morimi-Kazuha/VideoMind`，开发分支为 `codex/spec-development`。开始改动前 HEAD 为 `6bfbe9580271406fd0c2942f7800635d4a824e0e`，与规范记录的参考提交以及本机源码压缩包的 Git 提交标记一致。实际规范目录为 `docs/videomind-spec`。

外层 Git 仓库为 DOVideo-AI。同级 LongVideoAgent 分支 `codex/film-timeline-search` 的 HEAD 为 `8f8d22b04f71bb2d5da60ddb2409fedc3afc096f`，包含未提交的 CineTrace、前端和测试改动。本次开发从独立检出的 VideoMind 提交起步。适用的仓库协作说明来自解包后的 `docs/videomind-spec/AGENTS.md`。

## 已有能力与实施位置

| 能力 | 原有源码或配置 | 本轮接入位置 |
| --- | --- | --- |
| 登录、素材、上传、回放 | `src/dovideo/presentation/api/app.py`、`runtime.py`、`r4_runtime.py` | 复用登录身份，新增项目和不可变源版本，通过 `presentation/api/edit_sources.py` 接入已上传素材或签名上传 |
| 领域数据与持久化 | `application/media.py`、`infrastructure/persistence/sqlalchemy.py`、`alembic/versions/0001` 至 `0004` | 新增 0005、0006 迁移及修订、幂等回执、队列与产物，独立存储源时间和编译后的输出时间 |
| 视频分析 | `application/agent.py`、`infrastructure/media/`、`application/temporal_read.py` | 明确时间区间的编辑可独立运行。原片探测、实际 PTS 边界和 FFmpeg 渲染在 `infrastructure/edit_media.py` |
| 检索与模型 | `application/retrieval.py`、`infrastructure/vector/qdrant.py`、`infrastructure/providers/model.py` | 保留原分析入口，Flash 图片和工具合同单独预检；真实模型、混合检索质量与团队恢复需各自验收 |
| 服务与工作台 | `docker-compose.r2.yml`、`infrastructure/celery_runtime.py`、`client/src/App.vue` | 在 FastAPI 和 Vue 工作台加编辑接口、源视频预览、版本与任务入口 |

## 环境与验证

后端使用 Python 3.12.9。依赖来自 `pyproject.toml` 与 `constraints-py312.txt`。前端依赖锁在 `client/package-lock.json`。测试入口由 `.github/workflows/ci.yml` 和 README 给出。FFmpeg 与 ffprobe 的本机版本均为 5.1.2，通过 `DOVIDEO_FFMPEG_DIR` 选择。

| 实际检查 | 结果 |
| --- | --- |
| `git ls-remote https://github.com/Morimi-Kazuha/VideoMind.git HEAD` 和 `git rev-parse HEAD` | 均退出 0，提交一致 |
| 未修改的参考提交运行 `python -m pytest -q` | 退出 1，1945 通过、47 跳过、4 个原有失败，见下表 |
| 隔离临时 SQLite 库调用 `upgrade_schema(engine)` 和 `require_schema_head(engine)`，连续两次 | 退出 0，head 为 `0006_edit_timeline`，共 8 张 vm_ 表；Alembic 对比这八张表的实际结构与当前 metadata，差异为空 |
| `python -m pytest tests/infrastructure/test_alembic_migrations.py tests/spec_005/test_patch_transactions.py -q` | 退出 0，10 通过。覆盖空库迁移、已存在表的接管、并发版本提交、幂等、回退和任务租约 |
| 独立 MySQL 8.0 / InnoDB 临时实例连续两次升级并检查 head；设置 `VIDEOMIND_TEST_MYSQL_URL` 后运行 `python -m pytest tests/spec_005/test_patch_transactions.py -q` | 退出 0，head 为 `0006_edit_timeline`，编辑表结构差异为空；SQLite 与真实 MySQL 两套参数共 8 通过，含竞争提交和租约接管 |
| `python -m pytest tests/spec_005/test_editing_api.py -q`，配置上述 FFmpeg 目录 | 退出 0，1 通过。经登录、上传、添加两个区间、后台渲染、签名播放，得到 800000 微秒的编译时间线，下载字节与 SHA-256 相符 |

| 参考提交上已有的失败用例 |
| --- |
| `test_comparison_metrics_are_deterministic` |
| `test_instrumentation_preserves_all_twelve_rankings_and_sanitizes_gold[True]` |
| `test_live_arm_orchestration_with_test_doubles_verifies_scope_cleanup_and_three_arms` |
| `test_real_mp3_and_no_audio` |

MySQL 检查使用本机已安装的服务端程序、临时数据目录和单独的回环端口。验收结束后已关闭临时实例。可通过 `VIDEOMIND_TEST_MYSQL_URL` 指定独立可写测试库来复现。Docker 服务处于停止状态。Compose 内多服务、独立进程故障和真实模型验收状态分别记录在规范包的进度与验收表中。
