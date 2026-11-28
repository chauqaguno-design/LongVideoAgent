<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { renderArtifact, request } from './editApi.js'
import { interval, label, pending } from './projectView.js'
import ExportDownload from './ExportDownload.vue'
import UsageMetrics from './UsageMetrics.vue'

const props = defineProps({
  turn: { type: Object, required: true },
  state: Object,
  result: Object,
  liveSteps: Array,
  image: Object,
})
const emit = defineEmits(['seek', 'play', 'timeline'])
const screenshot = ref('')
const download = ref(null)
const savedUsage = ref(null)
const usageRequested = ref(false)
const playerBusy = ref(false)
const error = ref('')
const lifecycle = new AbortController()
const toolActions = {
  search_evidence: '检索带时间码的证据',
  read_transcript: '读取对齐的台词与观察',
  inspect_frames: '核查原片画面',
  get_media_overview: '读取项目素材',
  get_timeline: '读取时间线',
  edit_timeline: '保存时间线版本',
  render_video: '渲染视频',
  ask_user: '确认剪辑要求',
}

const status = computed(() => props.state?.status || props.turn.status)
const live = computed(() => pending({ status: status.value }))
const answer = computed(() => props.result?.answer || props.turn.answer)
const citations = computed(() => props.result?.citations || props.turn.citations || [])
const previewId = computed(() => props.result?.preview_artifact_id || props.turn.preview_artifact_id)
const exportId = computed(() => props.result?.export_artifact_id || props.turn.export_artifact_id)
const editId = computed(() => props.result?.edit_project_id || props.turn.edit_project_id)
const revisionId = computed(() => props.result?.revision_id || props.turn.revision_id)
const usage = computed(() => props.state?.usage || props.turn.usage || savedUsage.value)
const steps = computed(() => {
  const saved = props.result?.tool_steps || props.turn.tool_steps || []
  const merged = new Map(saved.map(step => [step.tool_call_id, step]))
  for (const step of props.liveSteps || []) {
    const known = merged.get(step.tool_call_id)
    const alreadyFinished = known && ['SUCCEEDED', 'FAILED'].includes(known.status)
    if (live.value && alreadyFinished && ['RUNNING', 'WAITING'].includes(step.status)) continue
    merged.set(step.tool_call_id, live.value ? { ...known, ...step } : { ...step, ...known })
  }
  return [...merged.values()].map(step => {
    const shouldWait = (step.name === 'ask_user' && status.value === 'AWAITING_INPUT') ||
      (step.name === 'render_video' && status.value === 'WAITING_JOB')
    return shouldWait && step.status === 'RUNNING' ? { ...step, status: 'WAITING' } : step
  })
})

function toolSummary(step) {
  const action = toolActions[step.name]
  if (!action || /\p{Script=Han}/u.test(step.summary || '')) return step.summary
  if (step.status === 'FAILED') return action + '失败'
  if (step.status === 'WAITING') return '等待' + action
  return (step.status === 'SUCCEEDED' ? '已' : '正在') + action
}

function tone(value) {
  if (['RUNNING', 'QUEUED', 'WAITING_JOB'].includes(value)) return 'running'
  if (value === 'SUCCEEDED') return 'ready'
  if (value === 'FAILED') return 'failed'
  if (value === 'WAITING' || value === 'AWAITING_INPUT') return 'partial'
  return ''
}

watch(
  [exportId, revisionId],
  async ([id, revision], _, onCleanup) => {
    download.value = null
    if (!id) return
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    try {
      const file = await renderArtifact(id, 'EXPORT', revision, controller.signal)
      if (!controller.signal.aborted) download.value = file
    } catch (cause) {
      if (!controller.signal.aborted) error.value = cause.message
    }
  },
  { immediate: true },
)

async function resolveScreenshot(id, signal) {
  try {
    const file = await request('/artifacts/' + id, undefined, signal)
    if (!signal.aborted) screenshot.value = file.url
  } catch {
    if (!signal.aborted) screenshot.value = ''
  }
}

let reloadedImage = false
watch(
  () => [props.turn.image_artifact_id, props.image?.url],
  ([id, url], _, onCleanup) => {
    reloadedImage = false
    screenshot.value = url || ''
    if (!id || url) return
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    resolveScreenshot(id, controller.signal)
  },
  { immediate: true },
)

function renewScreenshot() {
  if (reloadedImage || !props.turn.image_artifact_id) return
  reloadedImage = true
  resolveScreenshot(props.turn.image_artifact_id, lifecycle.signal)
}

async function playPreview() {
  playerBusy.value = true
  error.value = ''
  try {
    const file = previewId.value
      ? await renderArtifact(previewId.value, 'PREVIEW', revisionId.value, lifecycle.signal)
      : download.value
    if (file && !lifecycle.signal.aborted) emit('play', { file })
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  } finally {
    playerBusy.value = false
  }
}

async function loadUsage(event) {
  if (!event.target.open || usage.value || usageRequested.value) return
  usageRequested.value = true
  try {
    const state = await request('/workflow-runs/' + props.turn.run_id, undefined, lifecycle.signal)
    if (!lifecycle.signal.aborted) savedUsage.value = state.usage
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  } finally {
    usageRequested.value = false
  }
}

onUnmounted(() => lifecycle.abort())
</script>

<template>
  <article class="conversation-turn" :data-run-id="turn.run_id">
    <div class="conversation-user">
      <img v-if="screenshot" :src="screenshot" alt="用户附带的截图" @error="renewScreenshot" />
      <p>{{ turn.message }}</p>
    </div>
    <div class="conversation-response">
      <header class="response-header">
        <strong>CutSieve</strong>
        <span v-if="status" class="status-pill" :data-tone="tone(status)">
          <i class="status-dot" aria-hidden="true"></i>{{ label(status) }}
        </span>
      </header>

      <details v-if="steps.length" class="tool-steps" :open="live">
        <summary>执行步骤 <span class="mono">{{ steps.length }}</span></summary>
        <ol>
          <li v-for="step in steps" :key="step.tool_call_id" :data-status="step.status">
            <div>
              <code class="mono">{{ step.name }}</code>
              <span class="status-pill" :data-tone="tone(step.status)">
                <i class="status-dot" aria-hidden="true"></i>{{ label(step.status) }}
              </span>
            </div>
            <p v-if="toolSummary(step)" :title="step.summary">{{ toolSummary(step) }}</p>
          </li>
        </ol>
      </details>
      <div v-for="(item, index) in turn.clarifications || []" :key="index" class="clarified">
        <p>{{ item.question }}</p>
        <p>{{ item.reply }}</p>
      </div>
      <p v-if="answer" class="turn-answer">{{ answer }}</p>
      <p v-if="state?.error" class="inline-error" role="alert">{{ state.error.message || state.error }}</p>

      <ul v-if="citations.length" class="citation-chips" aria-label="原片引用">
        <li v-for="(citation, index) in citations" :key="index">
          <button class="citation-chip" :title="citation.text || citation.label" @click="emit('seek', citation)">
            <span class="mono">{{ interval(citation) }}</span>
            <span v-if="citation.text || citation.label" class="citation-text">{{ citation.text || citation.label }}</span>
          </button>
        </li>
      </ul>
      <p v-for="(warning, index) in result?.warnings || []" :key="index" class="muted">{{ warning.message || warning }}</p>

      <div v-if="previewId || download || editId" class="conversation-actions response-actions">
        <button v-if="previewId" class="teal" :disabled="playerBusy" @click="playPreview">在监视器播放预览</button>
        <button v-else-if="download && download.mime_type !== 'video/x-matroska'" class="teal" :disabled="playerBusy" @click="playPreview">在监视器播放成片</button>
        <ExportDownload v-if="download" :file="download" :revision-id="revisionId" @error="error = $event" />
        <button v-if="editId" @click="emit('timeline', { edit_project_id: editId, revision_id: revisionId })">查看时间线</button>
      </div>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <details v-if="turn.run_id !== 'sending'" class="turn-usage" @toggle="loadUsage">
        <summary>本轮用量</summary>
        <UsageMetrics v-if="usage" :usage="usage" />
        <p v-else class="muted">{{ usageRequested ? '正在读取用量…' : '暂无用量记录' }}</p>
      </details>
    </div>
  </article>
</template>
