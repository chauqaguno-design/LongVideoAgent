export const stateNames = {
  QUEUED: '排队中', RUNNING: '处理中', PARTIAL: '部分完成', SUCCEEDED: '已完成',
  FAILED: '失败', CANCELLED: '已取消', WAITING_JOB: '等待后台作业', AWAITING_INPUT: '等待补充信息',
  READY: '待执行', CLAIMED: '已领取', BLOCKED: '等待依赖', SUPERSEDED: '目标已替换',
  SUPPORTED: '有直接依据', UNCERTAIN: '待确认', NOT_CHECKED: '候选', CONFLICTING: '依据冲突',
  PLANNED: '已规划', DECODED: '已解码', OBSERVED: '已观察', QUERY_VERIFIED: '已按问题核查',
  AVAILABLE: '可用', UNCONFIGURED: '未配置', PENDING: '待处理',
  ROUTE: '识别任务目标', PLAN: '规划任务', PROPOSE: '拟定剪辑方案', COMMIT: '提交时间线', RENDER: '渲染成片', REVIEW: '核查结果', DONE: '已结束',
  SAMPLING: '抽取源帧', NOTES: '生成观影笔记', INDEXING: '建立检索索引', PUBLISHED: '索引已发布',
  RESUMING: '恢复处理中', DELETED: '清理已完成',
  ASR: '语音', OCR: '画面文字', VISUAL: '画面', COORDINATOR: '协调', MATERIAL: '选材', EDITOR: '编辑', REVIEWER: '审阅',
  asr: '语音转写', ocr: '画面文字', visual_observation: '画面观察', text_embedding: '文字索引', image_embedding: '图片索引',
  'embedded-subtitle': '内嵌字幕', 'faster-whisper': '语音转写', 'paddleocr': '画面文字', 'sampled-frame': '采样画面',
}
export const label = value => stateNames[value] || value
export const exportFormats = [
  { extension: 'mp4', label: 'MP4', mime: 'video/mp4' },
  { extension: 'webm', label: 'WebM', mime: 'video/webm' },
  { extension: 'mkv', label: 'MKV', mime: 'video/x-matroska' },
]
export const exportFormat = mime => exportFormats.find(format => format.mime === mime)
export const pending = run => ['QUEUED', 'RUNNING', 'WAITING_JOB'].includes(run?.status)
export function timecode(us) {
  const ms = Math.round(us / 1000)
  const minutes = Math.floor(ms / 60000)
  const seconds = String(Math.floor(ms / 1000) % 60).padStart(2, '0')
  const fraction = String(ms % 1000).padStart(3, '0').replace(/0+$/, '')
  return minutes + ':' + seconds + (fraction ? '.' + fraction : '')
}
export const interval = range => range.end_us - range.start_us < 1000
  ? timecode(range.start_us)
  : timecode(range.start_us) + ' – ' + timecode(range.end_us)
