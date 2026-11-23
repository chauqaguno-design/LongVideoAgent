# 检索、证据与本地复现

## 从查询到证据

登录后调用 `POST /v1/search`。服务先确定项目、素材版本、原片时间范围与索引快照，各路在相同的授权范围内排序。文字可以查源字幕、转写和 OCR。使用关键词或直接粘贴一句台词都可以召回候选。明确写成 `字幕：「给我三秒钟」` 后，问答工作流还能逐条核查指定范围内已提取的内嵌字幕。

截图用 multipart 字段 `file` 上传到 `POST /v1/projects/{project_id}/query-images`。把返回的 `artifact_id` 放入 SearchRequest 的 `image_artifact_id`。要查截图中的局部物件，调用 `POST /v1/projects/{project_id}/query-images/{image_id}/roi`，提交原图像素坐标 `{"x":20,"y":20,"width":80,"height":50}`，再用返回的新 ID 搜索。JPEG、PNG、WebP 上限 8 MiB、1600 万像素。

命中的 `route_ranks` 记录各路名次、原始分数与版本。RRF 使用常数 60，同一源观察在一条路内只计一次。点状画面命中提供与切块规则一致的可播放片段。点击证据可定位到单帧真实 PTS。静态截图能验证画面是否出现，同期中文台词能验证原文，确定说话人或连续动作需要额外证据。

直接搜索始终返回候选。问答工作流会固定查询计划、索引代次和核查协议。精确截图或带可信来源的查询裁剪会与最多八帧的 RGB 像素比较，匹配成功后返回查询专属 Evidence，引用查询截图、真实源帧和实际时间。重新压缩和缩放的截图仍能召回相似画面，像素不同的命中保留为候选。重投递从原快照恢复，源媒体删除后，新请求及原有证据链接会重新检查读取权限。

`FIRST` 需要单素材。对指定原文，只有目标之前的字幕覆盖完整，才会回答“首次”。`COUNT`、`ALL` 需要整个所选范围都完成同一协议，才能标注 `WITHIN_PROTOCOL`。存在空缺时标注 `OBSERVED_ONLY`，给出已确认下界和未核查区间。完整实例通过 `GET /v1/projects/{project_id}/query-results/{artifact_id}/occurrences` 分页，`top_k` 仅限制展示。字幕协议覆盖的是实际提取出的内嵌文字，画面事件、ASR 漏听及角色身份仍各有独立的不确定性。

## 图像空间选型

| 路径 | 已核实能力 | 运行条件 |
| --- | --- | --- |
| 空间像素特征 | 在本地三组裁切、缩放和 JPEG 压缩测试中都找到目标原帧。相似颜色和布局也可能被召回 | Pillow，CPU，无模型权重 |
| Qwen3-VL-Embedding-2B | 在同三组样本中都把目标排第一。实际输出 2048 维。开发样本只测量图图能力 | 本机模型卡标注 Apache-2.0，约 4.26 GB 权重。独立 Python 进程使用 CPU 或运行环境支持的设备 |
| BGE-M3 | 1024 维实际输出，按来源保留独立文本，当前查询与源观察各限 512 token，使用归一化 CLS。样片 ASR 上的中、英、混合三条检索分别命中了预期的第一项 | 权重 2,271,145,830 字节，MIT。通过 `--text` 指定本地目录。[BAAI 模型卡](https://huggingface.co/BAAI/bge-m3) |

图像输入转为 RGB，按比例限制到 384×384 内。服务以归一化向量检索。空间版本包含权重、配置、分词器、预处理及模型帮助代码指纹。实际 Qwen 空间指纹为 `d09957ac4463ea711d2f7721f31d3bb83aa6ae3df4c6e646cb576bb8234788c7`。更换模型后需要重建对应素材索引，即使维度相同也会拒绝混用。此次选用本机已有模型验证图图链路；相似动作、中文跨模态、跨片近似画面命中率需要独立评测。该模型 API 调用费为零，CPU、内存、电力和存储仍产生机器成本。

## 启动

在 VideoMind 仓库根目录执行。以下路径对应本机已有的权重与 Python 环境。

```powershell
$env:OMP_NUM_THREADS = '4'
$env:MKL_NUM_THREADS = '4'
..\LongVideoAgent\.venv\Scripts\python.exe scripts/serve_search_encoder.py --vision ../LongVideoAgent/data/models/Qwen3-VL-Embedding-2B --text work/models/bge-m3 --port 18176
```

另开终端，把编码服务配置给 API，再按项目 README 启动。通过 `Invoke-RestMethod http://127.0.0.1:18176/spaces` 查看实际加载的空间。所选素材需要经过包含该编码器的一次索引构建。仅使用视觉模型时可省去 `--text work/models/bge-m3`。

```powershell
$env:VIDEOMIND_SEARCH_ENCODER_URL = 'http://127.0.0.1:18176'
```

默认将向量表示保存在应用工作目录，按授权范围精确排序。要启用服务端 Qdrant，另设 `VIDEOMIND_SEARCH_QDRANT_URL`。本机隔离测验可设 `VIDEOMIND_SEARCH_QDRANT_PATH`，并在 API 环境中安装 `qdrant-client>=1.15,<2`。每个空间使用独立 collection，按 workspace、project、素材 revision、生产该索引的组件 generation 以及所选时间内的证据白名单过滤后，再取 topK。嵌入式 Qdrant 和独立 Docker Qdrant 1.15.5 已分别实测。未配置编码服务时可以直接使用词法和像素召回。服务断线时，搜索保留可用路线并返回 warnings。

本机 BGE-M3 下载自 BAAI 在 ModelScope 的 `e44369c5623cc146f016da906583db4ee0e3488d` 版本，主权重 SHA-256 与 Hugging Face 的 LFS 记录相同，为 `b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38`。下载文件清单与逐文件校验记录见 `work/spec-evidence/search/bge-acquisition.json`。在其他机器上，从该版本下载清单列出的配置、分词器与单一权重到 `work/models/bge-m3` 即可运行。[BAAI 官方模型镜像](https://modelscope.cn/models/BAAI/bge-m3)

## 复现实测

在 PowerShell 中给 `DOVIDEO_FFMPEG_DIR` 指向包含 ffmpeg 和 ffprobe 的目录。需要在当前 Python 环境安装项目依赖。使用 `--qdrant-dir` 时还需要 Qdrant Python SDK。

```powershell
$env:DOVIDEO_FFMPEG_DIR = "$env:USERPROFILE\miniconda3\envs\math\Library\bin"
.\.venv\Scripts\python.exe -m pytest -q tests/retrieval_grounding
.\.venv\Scripts\python.exe scripts/bench_screenshot_source.py ../LongVideoAgent/test-media/CineTrace-15s.mp4 --encoder http://127.0.0.1:18176 --output work/spec-evidence/search/dual-model
.\.venv\Scripts\python.exe scripts/bench_screenshot_source.py ../LongVideoAgent/test-media/CineTrace-15s.mp4 --encoder http://127.0.0.1:18176 --qdrant-dir work/spec-evidence/search/dual-model/index --output work/spec-evidence/search/dual-model
.\.venv\Scripts\python.exe scripts/check_scoped_qdrant.py --output work/spec-evidence/search/qdrant-scope.json
.\.venv\Scripts\python.exe scripts/bench_evidence_search.py --rounds 30 --output work/spec-evidence/search/benchmark-10k.json
.\.venv\Scripts\python.exe scripts/score_grounding.py work/spec-evidence/search/development-manifest.json work/spec-evidence/search/development-predictions.json --output work/spec-evidence/search/development-metrics.json
```

15 秒样片的 2.133333、6.400000、12.833333 秒各取一帧，中心裁 75%，缩为 142×100 并重复 JPEG 编码。两个独立本地排序和嵌入式 Qdrant 均在 3 张源帧中得到 3/3 第一名。另一个 Qdrant 用例故意放入五个更近的越权或越界向量，授权目标在 top1 返回。万块测试走真实认证 API、SQLite 和词法召回，30 次查询 P50 0.532 秒、P95 0.675 秒。10 个并发工作流受理请求 P95 0.164 秒，错误率 0。该轮仅测受理，队列等待和执行时长未采集。原始记录在 `work/spec-evidence/search`。

服务端测试使用独立的 `qdrant/qdrant:v1.15.5` 实例，绑定回环端口 18347。向截图脚本与权限探针传入 `--qdrant-url http://127.0.0.1:18347` 可复现服务端结果。文字测试运行 `scripts/bench_source_dialogue.py work/spec-evidence/processing-sample --qdrant-url http://127.0.0.1:18347 --output work/spec-evidence/search/server-qdrant/bge-dialogue.json`，输入是处理样片所得的 14 条真实观察。中文“有人请对方稍等片刻”对应英文台词的 8.900 秒，英文改写对应 10.060 秒，中英混合 John Doe 查询对应 2.760 秒。BGE 与最终融合各返回 3/3 第一名。三条查询耗时约 0.45–0.49 秒，API 费用为零。

MySQL 8.0.43 与 Qdrant 1.15.5 实例还跑了两次真实源数据构建。每个版本有四条观察，旧查询在发布后继续读取其固定快照，新查询读取新版本。两个版本的查询证据 ID 交集为零。另一个全空的编码缓存也能从 SQL 检索到已冻结的空间清单，用 Qdrant 返回文字和画面候选。复现时，为独立测试 MySQL 指定 `VIDEOMIND_BENCH_MYSQL_URL`，对同一库执行 Alembic 迁移；设好上述编码器和 Qdrant 地址后，运行 `scripts/check_search_generation.py 含字幕样片.mp4 --output work/spec-evidence/search/server-qdrant/mysql-generation.json`。

查询图片、ROI 出处、验证证据及完整事件清单在启用 MinIO 时写入 `videomind/query-files/{workspace}/{sha256}`。服务使用空工作目录接手时，会在通过当前权限检查后取回对象、校验摘要并缓存。两台节点需配置相同的 `DOVIDEO_MEDIA_SIGNING_KEY`，此前签发且尚在有效期的文件链接才能跨节点继续使用。

盲测使用 `scripts/score_grounding.py BLIND.json PREDICTIONS.json --dev DEV.json --output METRICS.json`。清单校验素材权利引用、SHA-256、来源家族互斥、标注与复核人、冻结时间、320 条问题构成、稳定实例 ID 等。预测须绑定盲测清单的 SHA-256 并包含逐条命中、验证、主张判定和越权计数。缺项会拒绝评分。按需求输出文字和截图 Recall@20、时段 Grounding R@5@tIoU0.5、主张 precision、起止点分别的 P95、全局查询和负例、分组统计。三张机械生成的截图只进入开发集；尚无 24 部、12 小时、独立人工复核的盲测成绩。开发集使用的本机视频没有在此项目内再分发。

## 两小时时间轴的编辑回归

运行 `scripts/verify_long_journey.py work/spec-evidence/two-hour-input/late-event.mp4 --output work/spec-evidence/long-journey`。输入由 460 帧黑画面、20 帧黄画面构成，每帧持续 15 秒，共两小时，无音轨。脚本封装带原始时间的 SRT，用新账号上传，经实际索引、文字和截图问答、自然语言预览、修剪和重新排序、冲突重试、导出及全量解码。新环境可用以下 FFmpeg 命令生成等价输入。

```powershell
ffmpeg -f lavfi -i "color=black:s=64x48:r=1/15:d=7200" -vf "drawbox=color=yellow:t=fill:enable='gte(t,6900)'" -frames:v 480 -c:v libx264 -pix_fmt yuv420p late-event.mp4
```

测试字幕从 01:59:30 开始，持续 2 秒。以黄色截图查询，能在 01:59:00 找到相同 RGB 的源帧。最终导出先播放黄色片尾，再播放缩短为 1.52 秒的片头，总长 3.52 秒。服务端签名下载的 SHA-256、与前一版本冲突的 409 响应以及重连后的连续工作流事件均记录在 `work/spec-evidence/long-journey/journey.json`。此次刻意只抽取少量帧、关闭在线模型，保留了部分覆盖状态。

## 盲测评分输入与失败判定

盲测问题须注明 `answerable`、`allowed_answers`，ALL 与 COUNT 还须注明 `exclusion_rule`。同一事件允许多个正确时段时，可在该 GT 的 `alternatives` 填入带 `media/start_us/end_us/basis` 的替代区间。同片近似场景或异片相似来源可以纳入查询范围；被明确排除的媒体版本列入 `negative_sources`，错误确认与候选召回分别列出。

每条预测提供 `execution_status`。失败、超时、取消继续计入分母。整份回答由人工在 `answer_review` 记录 `source: "human"`、`reviewer`、`basis`、`factual_claim_count`、`confirms_match` 及 `claims_exhaustive`。每个事实主张有文字、直接证据 ID、理由、评审者和支持判断。`coverage_review` 填入复核者、依据及 `verified`、`failed` 原片时段列表，时段格式为 `{media,start_us,end_us}`。FIRST 核查候选之前的前缀，完整计数检查全部请求时段。总体结果同时报告可回答率、拒答率、答案覆盖、负例误确认、失败时间比例、清晰/短/模糊/未配对事件。孤立帧按 ±2 秒计截图召回；没有时段重叠可验证时，最终定位的查询分母仍包含该正例。

每份独立控制测试收据为 JSON，包含 `commit`、`test_run` 和 `checks`。每个检查填写 `control`、`case`、`tested` 与 `violations`。跨租户检查还记录至少两个受测 workspace。完整用例名称见 `src/dovideo/evaluation/release_audits.py` 的 `CONTROLS`，覆盖六个权限入口、索引混读/半成品、成片结构及幂等副作用。将收据原始 SHA-256 加入预测文件的 `audit_evidence` 列表，然后用 `--audit 收据.json` 传入，可以重复传入。脚本会比对文件摘要及 commit。

开发集状态为 `DEVELOPMENT_ONLY`。盲测若已测指标未达门槛，返回 `FAILED`（退出码 1）。缺少必要指标、独立收据、完整覆盖评审或负例评测时返回 `BLOCKED`（退出码 2）。输入损坏也会用新的失败原因替换旧报告。这些判定只覆盖评测手册的质量与对应控制项，其余发布条件仍须由发布清单汇总。

## 删除后的检索数据

素材删除后，查询立即执行权限检查。保留期到期后，`scripts/cleanup_edit_files.py` 会列出可清理的已撤销查询证据、结果清单、扫描快照和缓存文件，指定 `--apply` 才会移除。启用 Qdrant 时会按 workspace、项目、素材版本、生产 generation 定向删除向量。原始查询截图、仍被其他素材引用的内容摘要及其他租户的数据继续可用。多节点的本地缓存应分别执行清理；共享数据库与对象删除支持重复执行。

## 同一镜头能否定位到原帧

在 15 秒开发样片 `CineTrace-15s.mp4` 上，Qwen3-VL-Embedding-2B 的 12 张查询图都把正确镜头排到第一，只有 2 张把原帧排到第一。之前的三张候选来自相隔较远的时间点。这次的候选包括同一镜头前后 1 帧和 8 帧，间隔约 33 毫秒和 267 毫秒。

用 FFmpeg 的 0.25 场景变化阈值找出三个切点，分别为 3.433、7.300、12.933 秒。在四个镜头各取中间一帧及其四个相邻帧，共用 20 张候选。下表列出真实原帧在这 20 张图中的名次。裁切保留中心区域宽、高各 75%，压缩使用 JPEG 质量 25。

| 查询图处理 | 1.700 秒 | 5.333 秒 | 10.100 秒 | 13.933 秒 | 原帧第一 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 中心裁切 | 2 | 2 | 4 | 1 | 1/4 |
| 整帧压缩 | 2 | 4 | 2 | 1 | 1/4 |
| 裁切、缩至 120×90、压缩 | 2 | 4 | 3 | 3 | 0/4 |

例如，10.100 秒画面裁切后，编码器最先返回 10.367 秒的邻帧，余弦相似度为 0.7678，原帧以 0.7543 排第四。全部 12 次查询的前五名都来自同一镜头，最相似的其他镜头均排第六。这个样片适合验证“跳回哪一段”，单凭这些分数指定精确帧已有十个反例。此处仅记录单片开发实测。[20 张候选的全部分数、帧号与真实时间](evidence/frame-neighbors.json)

```powershell
$env:DOVIDEO_FFMPEG_DIR = "$env:USERPROFILE\miniconda3\envs\math\Library\bin"
.\.venv\Scripts\python.exe scripts/bench_neighbor_frames.py "$env:USERPROFILE\Desktop\CineTrace-15s.mp4" --encoder http://127.0.0.1:18176 --output work/spec-evidence/search/neighbor-frames.json
```
