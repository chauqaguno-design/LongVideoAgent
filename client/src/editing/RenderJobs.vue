<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { listAll, makeCommand, request } from './editApi.js'
import { exportFormat, exportFormats, interval } from './projectView.js'
import SourceMoment from './SourceMoment.vue'
import ExportDownload from './ExportDownload.vue'

const props = defineProps({ revision: Object, assets: Array })
const mapping = ref(null), moment = ref(null)
const jobs = ref([])
const format = ref('mp4'), exportTypes = ref({})
const artifact = ref(null)
const error = ref('')
const busy = ref(false)
const submit = makeCommand()
const lifecycle = new AbortController()
const labels = { QUEUED: '排队中', RUNNING: '生成中', VALIDATING: '校验中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' }
const pending = job => ['QUEUED', 'RUNNING', 'VALIDATING'].includes(job.status)
let timer, generation = 0

async function refresh() {
  const requestNo = ++generation
  clearTimeout(timer)
  try {
    const all = await listAll('/edit-projects/' + props.revision.edit_project_id + '/render-jobs', lifecycle.signal)
    if (requestNo !== generation || lifecycle.signal.aborted) return
    jobs.value = all.filter(j => j.revision_id === props.revision.revision_id)
    if (jobs.value.some(pending)) timer = setTimeout(refresh, 1500)
    await Promise.all(jobs.value.filter(job => job.profile === 'EXPORT' && job.artifact_id &&
      !exportTypes.value[job.render_job_id]).map(async job => {
      const file = await request('/artifacts/' + job.artifact_id, undefined, lifecycle.signal)
      exportTypes.value[job.render_job_id] = exportFormat(file.mime_type)?.label
    }))
  } catch (cause) { if (!lifecycle.signal.aborted) error.value = cause.message }
}
async function render(profile, cancelJob) {
  error.value = ''
  busy.value = true
  try {
    if (cancelJob) await submit('/render-jobs/' + cancelJob.render_job_id + '/cancel', {})
    else {
      const exporting = profile === 'EXPORT'
      const query = exporting && format.value !== 'mp4' ? '?format=' + format.value : ''
      const job = await submit('/render-jobs' + query, { revision_id: props.revision.revision_id, profile })
      if (exporting) exportTypes.value[job.render_job_id] = exportFormats.find(f => f.extension === format.value).label
    }
    if (!lifecycle.signal.aborted) await refresh()
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function openResult(job) {
  busy.value = true
  error.value = ''
  try {
    const result = await request('/artifacts/' + job.artifact_id, undefined, lifecycle.signal)
    if (!result.url) throw new Error('视频暂时不可用，请刷新后重试。')
    if (result.timeline_revision_id !== props.revision.revision_id || result.kind !== job.profile) {
      throw new Error('成片与当前剪辑版本不符')
    }
    artifact.value = { ...result, profile: job.profile }
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function showMapping(job) {
  busy.value = true
  try {
    const file = await request('/artifacts/' + job.compiled_manifest_artifact_id, undefined, lifecycle.signal)
    const response = await fetch(file.url, { credentials: 'omit', signal: lifecycle.signal })
    if (!response.ok) throw new Error('时间映射暂不可用')
    mapping.value = await response.json()
    moment.value = null
  } catch (cause) { if (!lifecycle.signal.aborted) error.value = cause.message }
  finally { busy.value = false }
}
onMounted(refresh)
onUnmounted(() => { lifecycle.abort(); clearTimeout(timer) })
</script>

<template>
  <section class="edit-card edit-results">
    <h2>预览与导出 <small>V{{ revision.revision_no }}</small></h2>
    <div class="edit-actions">
      <button :disabled="busy || !revision.clips.length" @click="render('PREVIEW')">生成预览</button>
      <label>导出格式
        <select v-model="format" :disabled="busy">
          <option v-for="f in exportFormats" :key="f.extension" :value="f.extension">{{ f.label }}</option>
        </select>
      </label>
      <button :disabled="busy || !revision.clips.length" @click="render('EXPORT')">导出视频</button>
      <button :disabled="busy" @click="refresh">刷新任务</button>
    </div>
    <ul v-if="jobs.length" class="edit-job-list" aria-live="polite">
      <li v-for="job in jobs" :key="job.render_job_id">
        <span>{{ job.profile === 'PREVIEW' ? '预览' : '导出' }} · {{ labels[job.status] }}
          <template v-if="pending(job)"> · {{ job.cancel_requested_at ? '正在取消' : Math.round(job.progress * 100) + '%' }}</template>
        </span>
        <small v-if="exportTypes[job.render_job_id]">{{ exportTypes[job.render_job_id] }}</small>
        <button v-if="pending(job)" :disabled="busy || !!job.cancel_requested_at" @click="render(null, job)">取消任务</button>
        <span v-if="job.error" role="alert">{{ job.error.message }}</span>
        <button v-if="job.status === 'SUCCEEDED' && job.artifact_id" :disabled="busy" @click="openResult(job)">打开成片</button>
        <button v-if="job.compiled_manifest_artifact_id" :disabled="busy" @click="showMapping(job)">查看源片对应</button>
      </li>
    </ul>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="artifact">
      <video v-if="artifact.mime_type !== 'video/x-matroska'" :key="artifact.url" :src="artifact.url" controls preload="metadata" aria-label="剪辑成片" />
      <p v-else>MKV 可下载到本地播放器观看。</p>
      <ExportDownload v-if="artifact.profile === 'EXPORT'" :file="artifact" :revision-id="revision.revision_id" @error="error = $event" />
    </template>
    <ol v-if="mapping"><li v-for="clip in mapping.clips" :key="clip.clip_id">
      成片 {{ interval({ start_us: clip.output_start_us, end_us: clip.output_end_us }) }}
      <button class="edit-link" @click="moment = { media_revision_id: clip.media_revision_id, range: clip.realized_source_range }">
        原片 {{ interval(clip.realized_source_range) }}</button>
    </li></ol>
    <SourceMoment v-if="moment && assets" :assets="assets" :moment="moment" />
  </section>
</template>
