<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { defaultProfile, listAll, makeCommand, request, uploadAsset } from './editApi.js'
import EditTimeline from './EditTimeline.vue'
import ProcessingPanel from './ProcessingPanel.vue'
import SearchPanel from './SearchPanel.vue'
import WorkflowPanel from './WorkflowPanel.vue'

const props = defineProps({ projectId: String, initialEdit: String, library: Array })
const emit = defineEmits(['selected'])
const assets = ref([])
const edits = ref([])
const editId = ref('')
const title = ref('')
const legacyId = ref('')
const busy = ref(false)
const error = ref('')
const uploadPercent = ref(null)
let uploading
const submit = makeCommand()
const root = '/projects/' + props.projectId
const ready = computed(() => assets.value.filter(a => a.status === 'AVAILABLE'))
const views = { workflow: '对话与任务', processing: '素材处理', search: '检索片段', edit: '时间线' }
const initialParams = new URLSearchParams(location.hash.split('?')[1])
const initialView = initialParams.get('view')
const view = ref(Object.hasOwn(views, initialView) ? initialView :
  (props.initialEdit && !initialParams.has('run') ? 'edit' : 'workflow'))
const workflowDraft = ref(null), pinnedRevision = ref('')
function changeView(next) {
  view.value = next
  if (next !== 'workflow') workflowDraft.value = null
  const [route, query] = location.hash.split('?')
  if (route !== '#edit') return
  const p = new URLSearchParams(query)
  p.set('view', next)
  if (next === 'edit' && editId.value) p.set('edit', editId.value)
  if (next === 'workflow' && workflowDraft.value && !workflowDraft.value.run_id) p.delete('run')
  history.replaceState(history.state, '', route + '?' + p)
}
function inspect(draft) { workflowDraft.value = draft; changeView('workflow') }
async function openResult(id, revision = '') {
  await load()
  select(id)
  pinnedRevision.value = revision
  changeView('edit')
}

function select(id) {
  editId.value = id
  if (view.value === 'edit') emit('selected', id)
}
async function run(fn) {
  busy.value = true
  error.value = ''
  try { await fn() } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function load() {
  const [media, timelines] = await Promise.all([listAll(root + '/assets'), listAll(root + '/edit-projects')])
  assets.value = media
  edits.value = timelines
  const wanted = editId.value || props.initialEdit
  select(timelines.find(t => t.edit_project_id === wanted)?.edit_project_id || timelines[0]?.edit_project_id || '')
}
async function createEdit() {
  const edit = await submit('/edit-projects', { project_id: props.projectId, title: title.value, output_profile: defaultProfile })
  edits.value = [...edits.value, edit]
  select(edit.edit_project_id)
  title.value = ''
}
async function importMedia(file) {
  if (file) {
    uploading = new AbortController()
    uploadPercent.value = 0
    try {
      await uploadAsset(props.projectId, file, { signal: uploading.signal,
        progress: (bytes, total) => { uploadPercent.value = Math.round(bytes / total * 100) } })
    } catch (cause) {
      if (uploading.signal.aborted) throw new Error('上传已暂停。重新选择同一文件即可续传。')
      throw cause
    } finally { uploading = null; uploadPercent.value = null }
  }
  else await request(root + '/imports/' + encodeURIComponent(legacyId.value), {})
  assets.value = await listAll(root + '/assets')
  legacyId.value = ''
}
function chooseFile(event) {
  const [file] = event.target.files
  event.target.value = ''
  if (file) run(() => importMedia(file))
}
const status = { UPLOADING: '上传中', PROBING: '读取媒体中', REJECTED: '读取失败', DELETING: '移除中', DELETED: '已移除' }
onMounted(() => run(load))
onUnmounted(() => uploading?.abort())
</script>

<template>
  <section class="edit-project">
    <nav aria-label="项目工具" class="project-tabs">
      <button v-for="(name, key) in views" :key="key" :aria-pressed="key === view" @click="changeView(key)">{{ name }}</button>
    </nav>
    <div v-if="view === 'processing'" class="edit-toolbar">
      <label>添加视频
        <input type="file" accept="video/*,.mkv" :disabled="busy" @change="chooseFile" />
      </label>
      <form v-if="library.length" class="edit-inline" @submit.prevent="run(() => importMedia())">
        <label>已有素材
          <select v-model="legacyId" :disabled="busy"><option value="">选择媒体库视频</option>
            <option v-for="m in library" :key="m.id" :value="m.id">{{ m.filename }}</option>
          </select>
        </label>
        <button :disabled="busy || !legacyId">导入素材</button>
      </form>
      <button :disabled="busy" @click="run(load)">刷新项目</button>
      <span v-if="uploadPercent != null" role="status">上传 {{ uploadPercent }}%</span>
      <button v-if="uploadPercent != null" @click="uploading.abort()">暂停上传</button>
    </div>
    <p v-for="asset in assets.filter(a => a.status !== 'AVAILABLE')" :key="asset.asset_id">
      {{ asset.filename }} · {{ status[asset.status] || asset.status }}
    </p>
    <div v-if="view === 'edit'" class="edit-toolbar">
      <label v-if="edits.length">剪辑方案
        <select :value="editId" :disabled="busy" @change="select($event.target.value)">
          <option v-for="e in edits" :key="e.edit_project_id" :value="e.edit_project_id">{{ e.title }}</option>
        </select>
      </label>
      <form class="edit-inline" @submit.prevent="run(createEdit)">
        <label>新方案名称 <input v-model.trim="title" maxlength="160" required placeholder="例如，访谈精剪" /></label>
        <button :disabled="busy || !title">新建剪辑</button>
      </form>
    </div>
    <p v-if="busy" role="status">正在处理项目…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <EditTimeline v-if="view === 'edit' && editId" :key="editId" :edit-id="editId" :initial-revision="pinnedRevision" :assets="ready" />
    <ProcessingPanel v-if="view === 'processing'" :assets="ready" @removed="run(load)" />
    <SearchPanel v-if="view === 'search'" :assets="ready" :project-id="projectId" :edit-id="editId"
      @workflow="inspect" @edited="openResult" />
    <WorkflowPanel v-if="view === 'workflow'" :key="projectId" :assets="ready" :project-id="projectId"
      :draft="workflowDraft" @edited="openResult" @media-library="changeView('processing')" />
  </section>
</template>
