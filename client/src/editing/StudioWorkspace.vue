<script setup>
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { fetchRunHarness, listAll, request, uploadAsset } from './editApi.js'
import { assetStatuses, assetTone, usableMedia } from './projectView.js'
import AgentChat from './AgentChat.vue'
import EditTimeline from './EditTimeline.vue'
import HarnessTrace from './HarnessTrace.vue'
import ProcessingPanel from './ProcessingPanel.vue'
import StudioMonitor from './StudioMonitor.vue'
import SystemStatus from './SystemStatus.vue'
import './editing.css'

const projects = ref([])
const projectId = ref('')
const initialEdit = ref('')
const initialRun = ref('')
const navigation = ref(0)
const projectBusy = ref(true)
const projectError = ref('')
const projectForm = ref('')
const projectName = ref('')
const nameField = ref(null)
const assets = ref([])
const processing = ref({})
const assetsLoading = ref(false)
const assetError = ref('')
const selectedAssetId = ref('')
const uploadInput = ref(null)
const uploadPercent = ref(null)
const uploadMessage = ref('')
const drawer = ref('')
const dialog = ref(null)
const latestRun = shallowRef(null)
const activeRunId = ref('')
const runs = shallowRef([])
const traceSelection = ref(null)
const trace = shallowRef(null)
const traceLoading = ref(false)
const traceError = ref(null)
const timeline = ref(null)
const editId = ref('')
const trimDisabled = ref(true)
const monitorIntent = shallowRef(null)
const availablePreview = shallowRef(null)
const monitorPane = ref(null)

const lifecycle = new AbortController()
let projectSession
let assetTimer
let assetLoad = 0
let upload
let traceRequest

const source = computed(() => assets.value.find(asset => asset.asset_id === selectedAssetId.value))
const readyMedia = computed(() => assets.value.filter(usableMedia))
const statusOf = asset => assetTone(asset, processing.value[asset.asset_id])
const drawerTitles = { media: '素材与笔记', trace: '运行轨迹', status: '系统状态' }
const selectedRunId = computed(() => traceSelection.value || activeRunId.value || runs.value[0]?.run_id || null)
const openTraceId = computed(() => drawer.value === 'trace' ? selectedRunId.value : null)
const traceRunStatus = computed(() => runs.value.find(row => row.run_id === openTraceId.value)?.status)
const traceRefreshStatuses = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'WAITING_USER', 'AWAITING_INPUT']

function linkParams() {
  const [route, query] = location.hash.split('?')
  return new URLSearchParams(route === '#edit' ? query : '')
}

function remember(key, id) {
  if (!projectId.value) return
  const params = linkParams()
  if (params.get('project') !== projectId.value) {
    params.delete('edit')
    params.delete('run')
  }
  params.set('project', projectId.value)
  params.delete('view')
  if (key && id) params.set(key, id)
  else if (key) params.delete(key)
  history.replaceState(history.state, '', '#edit?' + params)
}

function selectProject(id, edit = '', run = '') {
  projectSession?.abort()
  clearTimeout(assetTimer)
  upload?.abort()
  upload = null
  uploadPercent.value = null
  uploadMessage.value = ''
  assetError.value = ''
  assetsLoading.value = false
  assets.value = []
  processing.value = {}
  selectedAssetId.value = ''
  monitorIntent.value = null
  availablePreview.value = null
  latestRun.value = null
  activeRunId.value = run
  runs.value = []
  traceSelection.value = null
  editId.value = ''
  trimDisabled.value = true
  drawer.value = ''
  projectForm.value = ''
  projectId.value = id
  initialEdit.value = edit
  initialRun.value = run
  navigation.value++
  remember()
  if (id) {
    projectSession = new AbortController()
    refreshAssets()
  }
}

function selectRun(id) {
  activeRunId.value = id
  remember('run', id)
}

function openTrace(id = null) {
  traceSelection.value = id
  drawer.value = 'trace'
}

async function refreshTrace() {
  const id = openTraceId.value
  if (!id) return
  traceRequest?.abort()
  const controller = new AbortController()
  traceRequest = controller
  traceLoading.value = true
  traceError.value = null
  try {
    const snapshot = await fetchRunHarness(id, controller.signal)
    if (!controller.signal.aborted) trace.value = snapshot
  } catch (cause) {
    if (!controller.signal.aborted) traceError.value = cause.status === 404 ? '此运行暂无轨迹记录' : cause.message
  } finally {
    if (!controller.signal.aborted) traceLoading.value = false
  }
}

watch([openTraceId, traceRunStatus], ([id, status], [previousId]) => {
  if (id !== previousId) {
    traceRequest?.abort()
    trace.value = null
    traceLoading.value = false
    traceError.value = null
    if (id) refreshTrace()
  } else if (id && traceRefreshStatuses.includes(status) && trace.value?.status !== status) {
    refreshTrace()
  }
})

async function loadProjects() {
  projectBusy.value = true
  projectError.value = ''
  try {
    const rows = await listAll('/projects', lifecycle.signal)
    if (lifecycle.signal.aborted) return
    projects.value = rows
    const params = linkParams()
    const linked = rows.find(project => project.project_id === params.get('project'))
    selectProject(
      linked?.project_id || rows[0]?.project_id || '',
      linked ? params.get('edit') || '' : '',
      linked ? params.get('run') || '' : '',
    )
  } catch (cause) {
    if (!lifecycle.signal.aborted) projectError.value = cause.message
  } finally {
    projectBusy.value = false
  }
}

function followLocation() {
  const params = linkParams()
  const id = params.get('project') || projectId.value
  if (projects.value.some(project => project.project_id === id)) {
    selectProject(id, params.get('edit') || '', params.get('run') || '')
  }
}

function toggleProjectForm(kind) {
  projectError.value = ''
  projectForm.value = projectForm.value === kind ? '' : kind
  projectName.value = projectForm.value === 'rename'
    ? projects.value.find(project => project.project_id === projectId.value)?.title || ''
    : ''
}

watch(projectForm, () => nameField.value?.focus(), { flush: 'post' })

async function saveProject() {
  const title = projectName.value.trim()
  if (!title || projectBusy.value) return
  const rename = projectForm.value === 'rename'
  projectBusy.value = true
  projectError.value = ''
  try {
    const project = rename
      ? await request('/projects/' + projectId.value, { title }, lifecycle.signal, 'PATCH')
      : await request('/projects', { title }, lifecycle.signal)
    if (lifecycle.signal.aborted) return
    projects.value = rename
      ? projects.value.map(row => row.project_id === project.project_id ? project : row)
      : [project, ...projects.value]
    if (!rename) selectProject(project.project_id)
    projectForm.value = ''
  } catch (cause) {
    if (!lifecycle.signal.aborted) projectError.value = cause.message
  } finally {
    projectBusy.value = false
  }
}

async function refreshAssets(preferred = '', force = false) {
  const { signal } = projectSession
  const current = ++assetLoad
  clearTimeout(assetTimer)
  assetsLoading.value = !assets.value.length
  const valid = () => !signal.aborted && current === assetLoad
  try {
    const media = await listAll('/projects/' + projectId.value + '/assets', signal)
    if (!valid()) return
    assets.value = media
    assetError.value = ''
    if (media.some(asset => asset.asset_id === preferred)) selectAsset(preferred)
    else if (!source.value) selectedAssetId.value = (media.find(usableMedia) || media[0])?.asset_id || ''
    assetsLoading.value = false

    const checks = media.filter(asset => usableMedia(asset) && !asset.processing_status &&
      (force || !Object.hasOwn(processing.value, asset.asset_id) || statusOf(asset) === 'processing'))
    const latest = await Promise.allSettled(checks.map(async asset => {
      const page = await request('/assets/' + asset.asset_id + '/processing-runs?limit=1', undefined, signal)
      return [asset.asset_id, page.items[0] || null]
    }))
    if (!valid()) return
    processing.value = {
      ...processing.value,
      ...Object.fromEntries(latest.flatMap(result => result.status === 'fulfilled' ? [result.value] : [])),
    }
  } catch (cause) {
    if (valid()) assetError.value = cause.message
  } finally {
    if (valid()) {
      assetsLoading.value = false
      if (assets.value.some(asset => statusOf(asset) === 'processing' && (usableMedia(asset) || asset.status !== 'UPLOADING'))) {
        assetTimer = setTimeout(refreshAssets, 2000)
      }
    }
  }
}

function selectAsset(id) {
  selectedAssetId.value = id
  monitorIntent.value = { mode: 'source' }
}

function seekSource(moment, origin) {
  const asset = moment.media_revision_id
    ? assets.value.find(row => row.media_revision_id === moment.media_revision_id)
    : assets.value.find(row => row.asset_id === moment.asset_id)
  selectedAssetId.value = asset?.asset_id || ''
  monitorIntent.value = { mode: 'source', moment, missing: !asset }
  if (origin === 'notes') drawer.value = ''
  revealMonitor()
}

function openPreview(preview) {
  monitorIntent.value = { mode: 'preview', preview }
  revealMonitor()
}

function revealMonitor() {
  monitorPane.value?.scrollIntoView?.({ block: 'nearest' })
}

function selectTimeline(selection) {
  editId.value = selection.editId
  trimDisabled.value = selection.trimDisabled
  if (selection.editId) remember('edit', selection.editId)
}

async function chooseFile(event) {
  const [file] = event.target.files
  event.target.value = ''
  if (!file || upload || !projectId.value) return
  const session = projectSession
  const controller = new AbortController()
  upload = controller
  uploadMessage.value = ''
  uploadPercent.value = 0
  try {
    const asset = await uploadAsset(projectId.value, file, {
      signal: controller.signal,
      progress: (bytes, total) => {
        if (upload === controller) uploadPercent.value = Math.round(bytes / total * 100)
      },
    })
    if (!session.signal.aborted) await refreshAssets(asset.asset_id, true)
  } catch (cause) {
    if (!session.signal.aborted) {
      uploadMessage.value = controller.signal.aborted
        ? '上传已暂停。重新选择同一文件即可续传。'
        : cause.message
    }
  } finally {
    if (upload === controller) {
      upload = null
      uploadPercent.value = null
    }
  }
}

watch(drawer, value => {
  const pane = dialog.value
  if (value && !pane.open) {
    if (pane.showModal) pane.showModal()
    else pane.setAttribute('open', '')
  } else if (!value && pane.open) {
    if (pane.close) pane.close()
    else pane.removeAttribute('open')
  }
}, { flush: 'post' })

onMounted(() => {
  window.addEventListener('hashchange', followLocation)
  loadProjects()
})
onUnmounted(() => {
  window.removeEventListener('hashchange', followLocation)
  lifecycle.abort()
  projectSession?.abort()
  traceRequest?.abort()
  upload?.abort()
  clearTimeout(assetTimer)
})
</script>

<template>
  <div class="studio-workspace">
    <header class="studio-bar">
      <div class="studio-brand"><slot name="brand" /></div>
      <div class="project-control">
        <select v-if="projects.length" aria-label="素材项目" :value="projectId" :disabled="projectBusy" @change="selectProject($event.target.value)">
          <option v-for="project in projects" :key="project.project_id" :value="project.project_id">{{ project.title }}</option>
        </select>
        <button :class="projects.length ? 'bare-button' : 'primary'" :aria-expanded="projectForm === 'create'" :disabled="projectBusy" @click="toggleProjectForm('create')">+ 新建项目</button>
        <button v-if="projectId" class="bare-button" :aria-expanded="projectForm === 'rename'" :disabled="projectBusy" @click="toggleProjectForm('rename')">重命名项目</button>
        <form v-if="projectForm" class="studio-popover" @submit.prevent="saveProject" @keydown.esc.stop="projectForm = ''">
          <label>
            {{ projectForm === 'rename' ? '新的项目名称' : '新项目名称' }}
            <input ref="nameField" v-model="projectName" maxlength="160" required placeholder="例如，访谈素材" />
          </label>
          <div class="studio-actions">
            <button class="primary" :disabled="projectBusy || !projectName.trim()">{{ projectForm === 'rename' ? '保存名称' : '创建项目' }}</button>
            <button type="button" :disabled="projectBusy" @click="projectForm = ''">取消</button>
          </div>
        </form>
      </div>

      <div class="media-controls" aria-label="项目素材">
        <div class="asset-strip">
          <button
            v-for="asset in assets"
            :key="asset.asset_id"
            class="asset-chip"
            :aria-pressed="asset.asset_id === selectedAssetId"
            :title="asset.filename"
            @click="selectAsset(asset.asset_id)"
          >
            <span class="asset-name">{{ asset.filename }}</span>
            <span class="asset-state" :data-tone="statusOf(asset)"><i class="status-dot" aria-hidden="true"></i>{{ assetStatuses[statusOf(asset)] }}</span>
          </button>
          <span v-if="!assets.length && projectId" class="muted">{{ assetsLoading ? '正在读取素材…' : '暂无素材' }}</span>
        </div>
        <button class="add-video" :disabled="!projectId || uploadPercent != null" @click="uploadInput.click()">+ 添加视频</button>
        <input ref="uploadInput" type="file" class="visually-hidden" aria-label="添加视频" accept="video/*,.mkv,.avi" tabindex="-1" :disabled="!projectId || uploadPercent != null" @change="chooseFile" />
      </div>

      <div class="drawer-triggers">
        <button aria-haspopup="dialog" :disabled="!projectId" @click="drawer = 'media'">素材与笔记</button>
        <button aria-haspopup="dialog" @click="openTrace()">运行轨迹</button>
        <button aria-haspopup="dialog" @click="drawer = 'status'">系统状态</button>
      </div>
    </header>

    <div v-if="uploadPercent != null || uploadMessage || projectError || assetError" class="studio-notices">
      <div v-if="uploadPercent != null" class="upload-meter">
        <label for="studio-upload-progress" class="mono">上传 {{ uploadPercent }}%</label>
        <progress id="studio-upload-progress" :value="uploadPercent" max="100"></progress>
        <button @click="upload?.abort()">暂停上传</button>
      </div>
      <p v-if="uploadMessage" role="status">{{ uploadMessage }}</p>
      <p v-if="projectError" role="alert">{{ projectError }} <button v-if="!projectForm" @click="loadProjects">重试</button></p>
      <p v-if="assetError" role="alert">{{ assetError }} <button @click="refreshAssets()">重试</button></p>
    </div>

    <main :key="navigation" class="studio-grid" aria-label="CutSieve 剪筛工作台">
      <div class="studio-left">
        <div ref="monitorPane" class="studio-monitor-pane">
          <StudioMonitor
            :asset="source"
            :intent="monitorIntent"
            :available-preview="availablePreview"
            :trim-disabled="!projectId || trimDisabled"
            :edit-id="editId"
            :save-clip="(clip, replace) => timeline.saveClip(clip, replace)"
          />
        </div>
        <EditTimeline
          ref="timeline"
          :project-id="projectId"
          :initial-edit="initialEdit"
          :assets="readyMedia"
          @selection="selectTimeline"
          @seek="seekSource"
          @play="openPreview"
          @preview-ready="availablePreview = $event"
        />
      </div>
      <AgentChat
        :project-id="projectId"
        :initial-run="initialRun"
        :assets="readyMedia"
        :edit-id="editId"
        @selected="selectRun"
        @seek="seekSource"
        @play="openPreview"
        @sync="timeline?.sync($event)"
        @timeline="timeline?.showRevision($event)"
        @latest-run="latestRun = $event"
        @runs="runs = $event"
        @inspect-trace="openTrace"
      />
    </main>

    <dialog ref="dialog" class="studio-drawer" aria-labelledby="studio-drawer-heading" @cancel.prevent="drawer = ''" @click.self="drawer = ''">
      <div v-if="drawer" class="drawer-content">
        <header class="pane-header">
          <h2 id="studio-drawer-heading">{{ drawerTitles[drawer] }}</h2>
          <button @click="drawer = ''">关闭</button>
        </header>
        <ProcessingPanel
          v-if="drawer === 'media'"
          :assets="assets"
          :initial-asset="selectedAssetId"
          @changed="refreshAssets('', true)"
          @seek="seekSource($event, 'notes')"
        />
        <HarnessTrace
          v-else-if="drawer === 'trace'"
          :runs="runs"
          :selected-run-id="selectedRunId"
          :trace="trace"
          :loading="traceLoading"
          :error="traceError"
          @select-run="traceSelection = $event"
          @refresh="refreshTrace"
        />
        <SystemStatus v-else-if="drawer === 'status'" :latest-run="latestRun" />
      </div>
    </dialog>
  </div>
</template>
