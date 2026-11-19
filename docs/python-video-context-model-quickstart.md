# 从长片取 15 秒，试通首版视频模型

**最快试一次**　在已下载本地测试素材的项目根目录，运行下方命令，将原片 10:00–10:15 的预切无声样片交给百炼。只需 Python 3.11 或 3.12、同地域的百炼兼容 Base URL 和 API Key，不用安装第三方 Python 库或 FFmpeg。按提示填写地址和 Key，或预置 `DASHSCOPE_BASE_URL` 与 `DASHSCOPE_API_KEY`。这会发送一次按账户规则计费的请求。[视觉调用脚本](../examples/describe_clip.py) · [百炼 API Key 与地域](https://help.aliyun.com/zh/model-studio/get-api-key)

```powershell
python examples/describe_clip.py test-media/visual-600s.mp4 --start-ms 600000 --duration-s 15
```

在 GitHub 克隆的副本中，先按第 1 步用自己的长片裁出同样长度的窗口。要试全部轨道，挑一句实际台词和一个能从画面确认的动作，并在同一个 PowerShell 窗口执行以下步骤。本地模型第一次加载时会下载权重。

## 1. 切出同一时间窗的画面和声音

准备 Python 3.11 或 3.12、带 `ffmpeg` 和 `ffprobe` 命令的 FFmpeg。先在项目根目录运行下面的命令。输入影片路径后，工作目录会切换到这次测试专用的临时目录。示例取原片第 600 秒开始的 15 秒，可以调整 `$startSec`。[FFmpeg 下载](https://ffmpeg.org/download.html) · [流选择与裁切](https://ffmpeg.org/ffmpeg.html)

```powershell
$repoRoot = (Get-Location).Path
$movie = (Resolve-Path -LiteralPath (Read-Host '输入电影路径')).Path
$trial = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ('video-context-' + [guid]::NewGuid()))
Set-Location -LiteralPath $trial.FullName
python -m venv .venv
$python = (Resolve-Path .venv/Scripts/python.exe).Path
$startSec = 600
$env:CLIP_START_MS = [string]($startSec * 1000)
ffprobe -v error -show_entries 'stream=index,codec_type,codec_name:stream_tags=language:format=duration' -of json "$movie"
ffmpeg -v error -ss $startSec -i "$movie" -t 15 -map 0:v:0 -an -vf 'fps=30,scale=640:-2' -c:v libx264 -pix_fmt yuv420p -crf 25 visual.mp4
if ($LASTEXITCODE -ne 0) { throw '视频裁切失败' }
$audioStream = ffprobe -v error -select_streams a:0 -show_entries stream=index -of csv=p=0 "$movie"
if ($audioStream -ne $null) {
    ffmpeg -v error -ss $startSec -i "$movie" -t 15 -map 0:a:0 -ac 1 -ar 16000 -c:a pcm_s16le speech.wav
    if ($LASTEXITCODE -ne 0) { throw '音频裁切失败' }
}
```

`visual.mp4` 已移除音轨，因此适合单独测试模型能否认出动作。若要验证“无声、无字幕”，还要挑一段画面中确实没有烧录字幕的镜头。`speech.wav` 若存在，起点与 `visual.mp4` 共用同一个原片时间锚点。

## 2. 调百炼看动作

在百炼控制台启用 `qwen3-vl-plus` 并取得同地域的 API Key 与 OpenAI 兼容 Base URL。北京业务空间可使用形如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` 的地址，替换业务空间 ID，或直接复制控制台给本工作空间的地址。运行时在隐藏输入框填写 Key，也可以预置 `DASHSCOPE_API_KEY`。示例用 Python 标准库发一次 HTTPS 请求。[创建 API Key](https://help.aliyun.com/zh/model-studio/get-api-key) · [视频字段、采样与大小限制](https://help.aliyun.com/zh/model-studio/vision)

对于第 1 步自己裁出的片段，在当前临时目录执行下面的命令。省略 `--duration-s` 时脚本会调用 `ffprobe` 得到实际时长。

```powershell
& $python (Join-Path $repoRoot 'examples/describe_clip.py') visual.mp4 --start-ms $env:CLIP_START_MS
```

脚本输出原片起止毫秒、可见动作和百炼报告的用量。例如片内 3.5 秒会换算为原片 603.5 秒。时间是模型估计值，可打开原片核查动作、方向和起止点。成功响应若含 `"events": []`，表示模型没有观察到可确认的动作。网络或 HTTP 调用失败时脚本以退出码 2 结束，返回内容为空、损坏或时间越界时以 3 结束。每次运行仅尝试一次云端调用。

## 3. 转写语音，读取字幕

**语音**　若已生成 `speech.wav`，运行 `& $python -m pip install 'faster-whisper==1.2.1' 'av==16.0.1'`。这是本次 Windows 试通的音频解码依赖组合。将下面代码保存为 `speech.py`，运行 `& $python speech.py`。`turbo` 首次需要下载本地权重。逐段迭代才实际执行转写；静音测试可能返回零条。[faster-whisper 使用说明](https://github.com/SYSTRAN/faster-whisper#usage)

```python
import os
from faster_whisper import WhisperModel

offset = int(os.environ['CLIP_START_MS'])
model = WhisperModel('turbo', device='cpu', compute_type='int8')
segments, info = model.transcribe('speech.wav', vad_filter=True)
for segment in segments:
    print(offset + round(1000 * segment.start), offset + round(1000 * segment.end), segment.text)
```

**内封文本字幕**　查看第 1 步的探测结果。若流 2 显示为 `subrip`、`ass` 或 `mov_text` 等文本字幕，在 PowerShell 运行 `ffmpeg -v error -i "$movie" -map 0:2 subtitles.srt`，流号按探测结果替换。从完整原片提取的 SRT 已含原片时间，直接筛选与测试时间窗相交的条目。PGS、DVD 等位图字幕需先解码或渲染为图像，再做 OCR。

**烧录字幕**　挑一个确定有字的时间点。以下从测试片内第 5 秒取一帧、裁底部三分之一，适用于底部字幕。调整区域可验证其他位置。若整部影片没有字幕，应改用有烧录字幕的样片验证这一路。[PaddleOCR 环境安装](https://www.paddleocr.ai/main/en/quick_start.html) · [OCR 模型与接口](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/OCR.html)

```powershell
ffmpeg -v error -ss 5 -i visual.mp4 -vf 'crop=iw:ih/3:0:ih-oh' -frames:v 1 subtitle.png
& $python -m pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
& $python -m pip install 'paddleocr>=3,<4'
```

将下面代码保存为 `ocr_frame.py`，运行 `& $python ocr_frame.py`。单帧能验证文字内容；做整片时间线时需综合连续采样帧估算起止时间并保留代表帧。

```python
import os
from paddleocr import PaddleOCR

ocr = PaddleOCR(device='cpu', text_detection_model_name='PP-OCRv5_mobile_det',
                text_recognition_model_name='PP-OCRv5_mobile_rec',
                use_doc_orientation_classify=False, use_doc_unwarping=False,
                use_textline_orientation=False)
for result in ocr.predict('subtitle.png'):
    print('原片采样毫秒', int(os.environ['CLIP_START_MS']) + 5000, result['rec_texts'])
```

## 4. 验证文字和视频能生成同维度向量

这一项在独立的 Linux 或 WSL 环境执行，需要 Git、`uv` 和 Python 3.11 或更高版本，并能下载模型权重。将 `visual.mp4` 复制到下面克隆的官方仓库根目录，或使用该环境能访问的片段路径。检查官方仓库哈希后可用它记录本次实验使用的版本。[官方实现与输入格式](https://github.com/QwenLM/Qwen3-VL-Embedding#model-input-specification) · [模型卡](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B)

```bash
git clone https://github.com/QwenLM/Qwen3-VL-Embedding.git
cd Qwen3-VL-Embedding
uv sync --python 3.11
git rev-parse HEAD
```

将下面代码保存为官方仓库根目录的 `embed_clip.py`，运行 `uv run python embed_clip.py`，输入片段文件路径。查询文字和原始画面按两条独立记录编码，预期输出的形状为 `(2, 2048)`。

```python
from pathlib import Path
from src.models.qwen3_vl_embedding import Qwen3VLEmbedder

clip = Path(input('片段路径：').strip()).resolve(strict=True)
model = Qwen3VLEmbedder(model_name_or_path='Qwen/Qwen3-VL-Embedding-2B')
vectors = model.process([
    {'text': '有人从门口走过', 'instruction': 'Retrieve video clips relevant to the query.'},
    {'video': str(clip), 'fps': 1, 'max_frames': 16},
])
print(tuple(vectors.shape), '余弦相似度', float(vectors[0] @ vectors[1]))
```

要验证检索质量，取一段有该动作和一段没有的画面，用相同查询比较排名。正式入库时保留 `video_id`、块 ID、原片起止、向量来源和模型版本，再将视觉、台词、字幕和动作描述的向量分别写入 Qdrant。一次测试记录片段时长、各步骤用时、云端返回的用量、人工确认的证据区间；这些数据可用于后续长片的成本和定位质量评估。

## 用《Meet John Doe》核对结果

源文件为 320×240 H.264 画面和 48 kHz 双声道 AAC，无内封字幕。使用 PRD 末尾列出的版本，跳到原片 10:00–10:15。对这段逐秒抽帧目视检查，约 10:09 有一名女子走到两名男子之间，抽查画面未见烧录字幕。它可以作为视觉模型首次试跑的人工对照。[长片与校验值](./python-video-context-v1-prd.md#首轮测试素材)

同段音轨转为 16 kHz 单声道 WAV，用 `faster-whisper` 的英文小模型 `tiny.en` 在 CPU 上做了真实转写。返回 10 个语段，其中原片 609000–610000 毫秒的识别文本为 “Wait a minute.”。想复现这次低开销试跑，可把上面示例的模型名由 `turbo` 换为 `tiny.en` 并指定 `language='en'`。

本地工作台还可使用 SmolVLM2 离线处理画面。首轮样片已实际运行本地视觉、PP-OCRv5 和 Qwen 向量索引。百炼示例需要自己的工作空间和 Key。[工作台启动与模型配置](../README.md)
