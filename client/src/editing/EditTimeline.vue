<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { defaultProfile, listAll, makeCommand, request } from './editApi.js'
import { interval, timecode } from './projectView.js'
import RenderJobs from './RenderJobs.vue'

const props = defineProps({ projectId: String, initialEdit: String, assets: { type: Array, default: () => [] } })
const emit = defineEmits(['selection', 'seek', 'play', 'preview-ready'])
const editProjects = ref([])
const editId = ref('')
const revisions = ref([])
const version = ref('')
const selectedClip = ref('')
const highlighted = ref(false)
const timelinePane = ref(null)
const creating = ref(false)
const nameField = ref(null)
const title = ref('')
const loading = ref(Boolean(props.projectId))
const busy = ref(false)
const error = ref('')
const artifactHints = ref({})
const renderRefresh = ref(0)

const head = computed(() => revisions.value[0])
const revision = computed(() => revisions.value.find(row => row.revision_id === version.value))
const current = computed(() => !!revision.value && revision.value === head.value)
const clips = computed(() => revision.value?.clips || [])
const duration = computed(() => revision.value?.total_duration_us || 0)
const canChange = computed(() => !loading.value && !busy.value && current.value)
const submit = makeCommand()
const lifecycle = new AbortController()
let reading

watch(
  [editId, loading, busy, current],
  () => emit('selection', { editId: editId.value, trimDisabled: loading.value || busy.value || (!!editId.value && !current.value) }),
  { immediate: true },
)
watch(() => revision.value?.revision_id, () => emit('preview-ready', null))
watch(creating, value => { if (value) nameField.value?.focus() }, { flush: 'post' })

async function openTimeline(id, pin = '') {
  if (!id || lifecycle.signal.aborted) return
  reading?.abort()
  const controller = new AbortController()
  reading = controller
  if (editId.value !== id) {
    editId.value = id
    revisions.value = []
    version.value = ''
    selectedClip.value = ''
  }
  loading.value = true
  const root = '/edit-projects/' + id
  try {
    const [edit, rows] = await Promise.all([
      request(root, undefined, controller.signal),
      listAll(root + '/revisions', controller.signal),
    ])
    if (controller.signal.aborted) return
    if (!editProjects.value.some(row => row.edit_project_id === id)) editProjects.value.unshift(edit)
    revisions.value = rows.sort((a, b) => b.revision_no - a.revision_no)
    version.value = pin && rows.some(row => row.revision_id === pin) ? pin : head.value?.revision_id || ''
    if (!clips.value.some(clip => clip.clip_id === selectedClip.value)) selectedClip.value = ''
  } catch (cause) {
    if (!controller.signal.aborted) throw cause
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}

async function newestProject(rows) {
  if (rows.length < 2) return rows[0]?.edit_project_id
  // The editing API orders edit projects by ID. V0 records the creation time.
  const dated = await Promise.allSettled(rows.map(async edit => {
    const first = edit.created_at || (await request(
      '/edit-projects/' + edit.edit_project_id + '/revisions?cursor=1&limit=1', undefined, lifecycle.signal,
    )).items[0]?.created_at
    return { id: edit.edit_project_id, created: Date.parse(first) || 0 }
  }))
  return dated.filter(result => result.status === 'fulfilled').map(result => result.value)
    .sort((a, b) => b.created - a.created)[0]?.id || rows[0].edit_project_id
}

async function initialize() {
  loading.value = true
  error.value = ''
  try {
    const rows = await listAll('/projects/' + props.projectId + '/edit-projects', lifecycle.signal)
    if (lifecycle.signal.aborted) return
    const existing = new Set(editProjects.value.map(row => row.edit_project_id))
    editProjects.value.push(...rows.filter(row => !existing.has(row.edit_project_id)))
    if (editId.value) return
    const id = rows.find(row => row.edit_project_id === props.initialEdit)?.edit_project_id || await newestProject(rows)
    if (!editId.value && id) await openTimeline(id)
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  } finally {
    if (!editId.value) loading.value = false
  }
}

async function createProject(name) {
  const edit = await submit('/edit-projects', { project_id: props.projectId, title: name, output_profile: defaultProfile })
  await openTimeline(edit.edit_project_id)
  return edit
}

async function apply(base, operation, reason) {
  if (!base) throw new Error('时间线版本暂不可用')
  const id = base.edit_project_id
  try {
    await submit('/edit-projects/' + id + '/operations', {
      expected_revision: base.revision_no,
      reason,
      operations: [operation],
    })
  } catch (cause) {
    if (cause.status !== 409) throw cause
    if (id === editId.value) await openTimeline(id)
    throw new Error('时间线已更新，请重新确认修改')
  }
  if (id === editId.value) await openTimeline(id)
}

async function saveClip(clip, replacing) {
  if (loading.value || busy.value || (editId.value && !current.value)) throw new Error('请选择时间线的当前版本')
  busy.value = true
  error.value = ''
  try {
    if (!editId.value) {
      const edit = await createProject('剪辑方案 1')
      if (editId.value !== edit.edit_project_id) throw new Error('已切换剪辑方案，请确认后再加入片段')
    }
    const base = head.value
    const previous = base?.clips.find(row => row.clip_id === replacing)
    if (replacing && !previous) throw new Error('当前版本中的片段已更新，请重新选择')
    if (previous?.media_revision_id === clip.media_revision_id) {
      if (previous.source_start_us === clip.source_start_us && previous.source_end_us === clip.source_end_us) {
        clip.evidence_ids = previous.evidence_ids
      }
      if (previous.caption_track_id) clip.caption_track_id = previous.caption_track_id
    }
    const asset = props.assets.find(row => row.media_revision_id === clip.media_revision_id)
    if (!clip.caption_track_id && asset?.capabilities?.some(c => c.name === 'asr' && c.state === 'AVAILABLE')) {
      const track = await request('/assets/' + asset.asset_id + '/caption-track', undefined, lifecycle.signal)
      if (track.caption_track_id) clip.caption_track_id = track.caption_track_id
    }
    if (lifecycle.signal.aborted) return
    const operation = replacing ? { op: 'replace_clip', clip_id: replacing, clip } : { op: 'append_clip', clip }
    await apply(base, operation, replacing ? '替换原片区间' : '追加原片区间')
  } finally {
    busy.value = false
  }
}

async function perform(action) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  } finally {
    busy.value = false
  }
}

function addProject() {
  if (!title.value.trim()) return
  return perform(async () => {
    await createProject(title.value.trim())
    title.value = ''
    creating.value = false
  })
}

function patch(operation, reason) {
  return perform(() => apply(head.value, operation, reason))
}

function retryTimeline() {
  if (editId.value) return perform(() => openTimeline(editId.value, version.value))
  return initialize()
}

function move(clip, index, step) {
  const order = clips.value.map(row => row.clip_id)
  order.splice(index, 1)
  order.splice(index + step, 0, clip.clip_id)
  return patch({ op: 'reorder_clips', clip_ids: order }, '调整片段顺序')
}

function clipRange(clip) {
  return { start_us: clip.source_start_us, end_us: clip.source_end_us }
}

function showClip(clip, replace = false) {
  selectedClip.value = clip.clip_id
  emit('seek', { ...clip, ...clipRange(clip), replace })
}

async function sync(change) {
  const id = change.edit_project_id || editId.value
  if (!id) return
  error.value = ''
  highlighted.value = false
  try {
    await openTimeline(id)
    if (lifecycle.signal.aborted) return
    const rev = change.revision_id || (id === editId.value ? head.value?.revision_id : null)
    if (rev && (change.preview_artifact_id || change.export_artifact_id)) {
      const hints = { ...artifactHints.value[rev] }
      if (change.preview_artifact_id) hints.preview = change.preview_artifact_id
      if (change.export_artifact_id) hints.export = change.export_artifact_id
      artifactHints.value[rev] = hints
    }
    if (id === editId.value) renderRefresh.value++
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  }
}

async function showRevision(change) {
  try {
    await openTimeline(change.edit_project_id || editId.value, change.revision_id)
    if (lifecycle.signal.aborted) return
    highlighted.value = true
    await nextTick()
    timelinePane.value?.focus({ preventScroll: true })
    timelinePane.value?.scrollIntoView?.({ block: 'nearest' })
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  }
}

const filename = id => props.assets.find(asset => asset.media_revision_id === id)?.filename || '已不可用的素材'

onMounted(() => { if (props.projectId) initialize() })
onUnmounted(() => {
  lifecycle.abort()
  reading?.abort()
})
defineExpose({ saveClip, showRevision, sync })
</script>

<template>
  <section
    ref="timelinePane"
    class="studio-pane timeline-pane"
    :class="{ 'timeline-focused': highlighted }"
    aria-labelledby="timeline-heading"
    tabindex="-1"
    @focusout="highlighted = false"
  >
    <header class="pane-header">
      <h2 id="timeline-heading">时间线</h2>
      <span class="timeline-total mono">{{ clips.length }} 段 <i></i> {{ timecode(duration) }}</span>
    </header>
    <div class="timeline-controls">
      <div class="scheme-control">
        <select aria-label="剪辑方案" :value="editId" :disabled="busy || !editProjects.length" @change="perform(() => openTimeline($event.target.value))">
          <option v-if="!editProjects.length" value="">暂无方案</option>
          <option v-for="edit in editProjects" :key="edit.edit_project_id" :value="edit.edit_project_id">{{ edit.title }}</option>
        </select>
        <button class="bare-button" :disabled="!projectId || busy || loading" :aria-expanded="creating" @click="creating = !creating">+ 新方案</button>
        <form v-if="creating" class="studio-popover" @submit.prevent="addProject" @keydown.esc.stop="creating = false">
          <label>新方案名称<input ref="nameField" v-model="title" maxlength="160" required /></label>
          <div class="studio-actions">
            <button class="primary" :disabled="busy || !title.trim()">创建方案</button>
            <button type="button" :disabled="busy" @click="creating = false">取消</button>
          </div>
        </form>
      </div>
      <select v-model="version" class="mono revision-select" aria-label="时间线版本" :disabled="busy || loading || !revisions.length" @change="selectedClip = ''">
        <option v-if="!revisions.length" value="">版本</option>
        <option v-for="row in revisions" :key="row.revision_id" :value="row.revision_id">
          V{{ row.revision_no }}{{ row === head ? '（当前）' : '' }}
        </option>
      </select>
      <button v-if="revision && !current" :disabled="busy || loading" @click="patch({ op: 'restore_revision', source_revision_id: version }, '从历史版本恢复')">
        恢复为新版本
      </button>
      <label class="caption-control">
        输出字幕
        <select :value="revision?.caption_policy || 'none'" :disabled="!canChange" @change="patch({ op: 'set_caption_policy', caption_policy: $event.target.value }, '更新输出字幕')">
          <option value="none">关闭</option>
          <option value="preserve_source">保留字幕轨</option>
          <option value="burn_aligned">烧录到画面</option>
        </select>
      </label>
    </div>

    <div class="timeline-content" :aria-busy="loading">
      <div class="track-panel" aria-label="单轨片段">
        <div class="track-ruler mono" aria-hidden="true">
          <span>{{ timecode(0) }}</span>
          <span v-if="duration">{{ timecode(Math.round(duration / 2)) }}</span>
          <span v-if="duration">{{ timecode(duration) }}</span>
        </div>
        <div v-if="clips.length" class="track-rail">
          <button
            v-for="(clip, index) in clips"
            :key="clip.clip_id"
            class="track-clip"
            :style="{ flexGrow: clip.source_end_us - clip.source_start_us }"
            :aria-label="'第 ' + (index + 1) + ' 段 ' + filename(clip.media_revision_id) + ' ' + interval(clipRange(clip))"
            :aria-pressed="selectedClip === clip.clip_id"
            @click="showClip(clip)"
          >
            <span>{{ filename(clip.media_revision_id) }}</span>
          </button>
        </div>
        <p v-else class="track-empty">{{ loading ? '正在读取时间线…' : '从原片或对话加入片段' }}</p>
      </div>

      <ol v-if="clips.length" class="edit-clips">
        <li v-for="(clip, index) in clips" :key="clip.clip_id" :class="{ 'clip-selected': selectedClip === clip.clip_id }">
          <button class="clip-source bare-button" @click="showClip(clip)">
            <span class="clip-index mono">{{ index + 1 }}</span>
            <span class="clip-filename">{{ filename(clip.media_revision_id) }}</span>
            <span class="timecode mono">{{ interval(clipRange(clip)) }}</span>
          </button>
          <span class="clip-audio">{{ clip.audio_policy === 'mute' ? '静音' : '原声' }}</span>
          <div v-if="current" class="clip-actions">
            <button class="bare-button" :disabled="!canChange || index === 0" @click="move(clip, index, -1)">上移</button>
            <button class="bare-button" :disabled="!canChange || index === clips.length - 1" @click="move(clip, index, 1)">下移</button>
            <button class="bare-button" :disabled="!canChange" @click="showClip(clip, true)">替换</button>
            <button class="bare-button danger" :disabled="!canChange" @click="patch({ op: 'remove_clip', clip_id: clip.clip_id }, '移除片段')">移除</button>
          </div>
        </li>
      </ol>
    </div>
    <p v-if="error" class="timeline-error inline-error" role="alert">
      {{ error }} <button class="bare-button" @click="retryTimeline">刷新时间线</button>
    </p>
    <RenderJobs
      :key="revision?.revision_id || 'empty'"
      :revision="revision"
      :hints="artifactHints[version]"
      :refresh-key="renderRefresh"
      @seek="emit('seek', $event)"
      @play="emit('play', $event)"
      @preview-ready="emit('preview-ready', $event)"
    />
  </section>
</template>
