<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { listAll, makeCommand, renderArtifact, request } from './editApi.js'
import { exportFormat, exportFormats, interval } from './projectView.js'
import ExportDownload from './ExportDownload.vue'

const props = defineProps({ revision: Object, hints: Object, refreshKey: Number })
const emit = defineEmits(['seek', 'play', 'preview-ready'])
const jobs = ref([])
const files = ref({})
const formats = ref({})
const format = ref('mp4')
const mapping = ref(null)
const error = ref('')
const busy = ref(false)
const labels = { QUEUED: '排队中', RUNNING: '生成中', VALIDATING: '校验中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' }
const active = job => ['QUEUED', 'RUNNING', 'VALIDATING'].includes(job.status)
const submit = makeCommand()
const lifecycle = new AbortController()
const resolving = new Set()
let timer
let generation = 0

const renderRows = computed(() => {
  const rows = [...jobs.value]
  for (const [profile, id] of [['PREVIEW', props.hints?.preview], ['EXPORT', props.hints?.export]]) {
    if (!id || rows.some(job => job.artifact_id === id)) continue
    rows.unshift({ render_job_id: id, profile, status: 'SUCCEEDED', artifact_id: id })
  }
  return rows
})

const latestPreview = computed(() => {
  const ready = renderRows.value.filter(row => row.status === 'SUCCEEDED' && files.value[row.artifact_id])
  const job = ready.find(row => row.profile === 'PREVIEW') || ready.find(row => files.value[row.artifact_id]?.mime_type !== 'video/x-matroska')
  return job ? { file: files.value[job.artifact_id], revisionNo: props.revision.revision_no } : null
})

watch(latestPreview, preview => emit('preview-ready', preview))

async function hydrate() {
  await Promise.all(renderRows.value.map(async job => {
    const id = job.artifact_id
    if (!id || job.status !== 'SUCCEEDED' || files.value[id] || resolving.has(id)) return
    resolving.add(id)
    try {
      const file = await renderArtifact(id, job.profile, props.revision.revision_id, lifecycle.signal)
      if (!lifecycle.signal.aborted) files.value[id] = file
    } catch (cause) {
      if (!lifecycle.signal.aborted) error.value = cause.message
    } finally {
      resolving.delete(id)
    }
  }))
}

async function refresh() {
  if (!props.revision) return
  const requestNo = ++generation
  clearTimeout(timer)
  const valid = () => requestNo === generation && !lifecycle.signal.aborted
  try {
    const all = await listAll('/edit-projects/' + props.revision.edit_project_id + '/render-jobs', lifecycle.signal)
    if (!valid()) return
    jobs.value = all.filter(job => job.revision_id === props.revision.revision_id)
    await hydrate()
  } catch (cause) {
    if (valid()) error.value = cause.message
  } finally {
    if (valid() && jobs.value.some(active)) timer = setTimeout(refresh, 1500)
  }
}

async function act(action) {
  if (busy.value) return
  error.value = ''
  busy.value = true
  try {
    await action()
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  } finally {
    busy.value = false
  }
}

function render(profile) {
  return act(async () => {
    const output = format.value
    const path = profile === 'EXPORT' && output !== 'mp4' ? '/render-jobs?format=' + output : '/render-jobs'
    const job = await submit(path, { revision_id: props.revision.revision_id, profile })
    if (lifecycle.signal.aborted) return
    if (profile === 'EXPORT') formats.value[job.render_job_id] = exportFormats.find(row => row.extension === output).label
    jobs.value = [job, ...jobs.value.filter(row => row.render_job_id !== job.render_job_id)]
    await refresh()
  })
}

function cancel(job) {
  return act(async () => {
    await submit('/render-jobs/' + job.render_job_id + '/cancel', { reason: '用户取消渲染' })
    if (!lifecycle.signal.aborted) await refresh()
  })
}

function play(file) {
  emit('play', { file, revisionNo: props.revision.revision_no })
}

function toggleMapping(job) {
  if (mapping.value?.id === job.render_job_id) {
    mapping.value = null
    return
  }
  return act(async () => {
    const { signal } = lifecycle
    const file = await request('/artifacts/' + job.compiled_manifest_artifact_id, undefined, signal)
    const response = await fetch(file.url, { credentials: 'omit', signal })
    if (!response.ok) throw new Error('源片映射暂不可用')
    const data = await response.json()
    if (!signal.aborted) mapping.value = { id: job.render_job_id, clips: data.clips }
  })
}

function retry() {
  error.value = ''
  return refresh()
}

watch(() => props.refreshKey, refresh, { immediate: true })
watch(() => props.hints, hydrate, { immediate: true })
onUnmounted(() => {
  lifecycle.abort()
  clearTimeout(timer)
})
</script>

<template>
  <section class="render-bar" aria-label="预览与导出">
    <div class="render-controls">
      <button :disabled="busy || !revision?.clips.length" @click="render('PREVIEW')">生成预览</button>
      <div class="export-controls">
        <select v-model="format" aria-label="导出格式" :disabled="busy || !revision?.clips.length">
          <option v-for="item in exportFormats" :key="item.extension" :value="item.extension">{{ item.label }}</option>
        </select>
        <button class="primary" :disabled="busy || !revision?.clips.length" @click="render('EXPORT')">导出视频</button>
      </div>
    </div>

    <ul v-if="renderRows.length" class="edit-job-list" aria-live="polite">
      <li v-for="job in renderRows" :key="job.render_job_id">
        <span
          class="status-pill"
          :data-tone="active(job) ? 'running' : job.status === 'SUCCEEDED' ? 'ready' : job.status === 'FAILED' ? 'failed' : ''"
        >
          <i class="status-dot" aria-hidden="true"></i>
          {{ job.profile === 'PREVIEW' ? '预览' : '导出' }} · {{ labels[job.status] || job.status }}
          <span v-if="active(job)" class="mono">{{ job.cancel_requested_at ? '正在取消' : Math.round((job.progress || 0) * 100) + '%' }}</span>
          <span v-if="job.profile === 'EXPORT'">{{ exportFormat(files[job.artifact_id]?.mime_type)?.label || formats[job.render_job_id] }}</span>
        </span>
        <button v-if="active(job)" class="bare-button" :disabled="busy || !!job.cancel_requested_at" @click="cancel(job)">取消渲染</button>
        <span v-if="job.error" class="inline-error" role="alert">{{ job.error.message || job.error }}</span>
        <template v-if="files[job.artifact_id]">
          <button
            v-if="files[job.artifact_id].mime_type !== 'video/x-matroska'"
            class="bare-button teal"
            @click="play(files[job.artifact_id])"
          >{{ job.profile === 'EXPORT' ? '播放成片' : '播放预览' }}</button>
          <ExportDownload v-if="job.profile === 'EXPORT'" :file="files[job.artifact_id]" :revision-id="revision.revision_id" @error="error = $event" />
        </template>
        <button v-if="job.compiled_manifest_artifact_id" class="bare-button" :disabled="busy" :aria-expanded="mapping?.id === job.render_job_id" @click="toggleMapping(job)">源片映射</button>
      </li>
    </ul>
    <p v-if="error" class="inline-error" role="alert">{{ error }} <button class="bare-button" @click="retry">刷新渲染状态</button></p>

    <ol v-if="mapping" class="mapping-list">
      <li v-for="clip in mapping.clips" :key="clip.clip_id">
        <span>成片 <span class="mono">{{ interval({ start_us: clip.output_start_us, end_us: clip.output_end_us }) }}</span></span>
        <button class="bare-button timecode" @click="emit('seek', { media_revision_id: clip.media_revision_id, ...clip.realized_source_range })">
          原片 <span class="mono">{{ interval(clip.realized_source_range) }}</span>
        </button>
      </li>
    </ol>
  </section>
</template>
