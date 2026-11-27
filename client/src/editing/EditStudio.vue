<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { listAll, request } from './editApi.js'
import { apiRequest } from '../api.js'
import EditProject from './EditProject.vue'
import './editing.css'

defineProps({ library: { type: Array, default: () => [] } })
const projects = ref([])
const projectId = ref('')
const title = ref('')
const busy = ref(false)
const error = ref('')
const rename = ref(null)
const health = ref(null)
const healthError = ref('')
const queueNames = { processing: '素材处理', workflow: '对话与任务', render: '渲染', outbox: '事件投递' }

async function inspectQueues() {
  healthError.value = ''
  try {
    const response = await apiRequest('/v1/readiness')
    if (!response.ok && response.status !== 503) throw new Error('无法获取工作区服务状态')
    health.value = await response.json()
  } catch (cause) {
    health.value = null
    healthError.value = cause.message
  }
}

const routeParams = () => new URLSearchParams(window.location.hash.split('?')[1])
const initialEdit = ref(routeParams().get('edit') || '')
const navigation = ref(0)

function selectProject(id, editId = '') {
  projectId.value = id
  rename.value = null
  initialEdit.value = editId
  rememberEdit(editId)
}

function rememberEdit(id) {
  if (!['', '#edit'].includes(window.location.hash.split('?')[0])) return
  const query = new URLSearchParams(window.location.hash.split('?')[1])
  if (query.has('project') && query.get('project') !== projectId.value) {
    query.delete('run')
    query.delete('view')
  }
  query.set('project', projectId.value)
  if (id) query.set('edit', id)
  else query.delete('edit')
  history.replaceState(history.state, '', '#edit?' + query)
}

async function load(create = false) {
  busy.value = true
  error.value = ''
  try {
    const created = create ? await request('/projects', { title: title.value.trim() }) : null
    projects.value = await listAll('/projects')
    const remembered = projects.value.find(p => p.project_id === routeParams().get('project'))
    const selected = created || remembered || projects.value[0]
    if (selected) selectProject(selected.project_id, created ? '' : initialEdit.value)
    if (created) title.value = ''
  } catch (cause) {
    error.value = cause.message
  } finally {
    busy.value = false
  }
}

function followLocation() {
  if (window.location.hash.split('?')[0] !== '#edit') return
  const p = routeParams()
  if (!p.has('project') || projects.value.some(item => item.project_id === p.get('project'))) {
    selectProject(p.get('project') || projectId.value, p.get('edit') || '')
    navigation.value++
  }
}

onMounted(() => {
  window.addEventListener('hashchange', followLocation)
  load()
})
onUnmounted(() => window.removeEventListener('hashchange', followLocation))

async function saveTitle() {
  busy.value = true
  error.value = ''
  try {
    const changed = await request('/projects/' + projectId.value, { title: rename.value.trim() }, undefined, 'PATCH')
    projects.value = projects.value.map(row => (row.project_id === changed.project_id ? changed : row))
    rename.value = null
  } catch (cause) {
    error.value = cause.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="edit-studio" aria-label="视频工作台">
    <header class="studio-titlebar">
      <div class="studio-heading">
        <h1>视频工作台</h1>
        <span class="studio-badge">Local WAL</span>
      </div>
      <div class="studio-titlebar-controls">
        <label v-if="projects.length">
          素材项目
          <select :value="projectId" :disabled="busy" @change="selectProject($event.target.value)">
            <option v-for="p in projects" :key="p.project_id" :value="p.project_id">{{ p.title }}</option>
          </select>
        </label>
      </div>
    </header>

    <details class="project-settings" :open="!projects.length">
      <summary>{{ projects.length ? '管理素材项目' : '创建第一个项目' }}</summary>
      <div class="edit-toolbar">
        <button
          v-if="projectId && rename === null"
          :disabled="busy"
          @click="rename = projects.find(p => p.project_id === projectId).title"
        >
          重命名项目
        </button>
        <form v-if="rename !== null" class="edit-inline" @submit.prevent="saveTitle">
          <label>新的项目名称<input v-model="rename" maxlength="160" required /></label>
          <button class="edit-primary" :disabled="busy || !rename.trim()">保存名称</button>
          <button type="button" @click="rename = null">保留原名</button>
        </form>
        <form class="edit-inline" @submit.prevent="load(true)">
          <label>新项目名称 <input v-model.trim="title" maxlength="160" required placeholder="例如，访谈素材" /></label>
          <button class="edit-primary" :disabled="busy || !title">创建项目</button>
        </form>
      </div>
    </details>

    <p v-if="error" role="alert">
      {{ error }}
      <button @click="load()">重试</button>
    </p>

    <EditProject
      v-if="projectId"
      :key="projectId + ':' + navigation"
      :project-id="projectId"
      :initial-edit="initialEdit"
      @selected="rememberEdit"
    />
    <section v-else-if="!busy && !error" class="conversation-empty">
      <h3>从创建素材项目开始</h3>
      <p>创建项目后，在素材处理中添加第一部视频。</p>
    </section>

    <details class="studio-telemetry" @toggle="$event.target.open && inspectQueues()">
      <summary>工作区服务状态</summary>
      <p v-if="healthError" role="alert">{{ healthError }}</p>
      <p v-if="health?.dependency === 'database'" role="alert">数据库暂时不可用，任务计数稍后可重查。</p>
      <template v-if="health?.queues">
        <p v-if="!health.ready" role="alert">有后台服务离线，排队任务可能需要等待。</p>
        <ul class="telemetry-list">
          <li v-for="(queue, key) in health.queues" :key="key" :class="{ 'queue-offline': !queue.online }">
            {{ queueNames[key] }} · {{ queue.online ? '在线' : '离线' }} · 排队 {{ queue.counts.QUEUED || queue.counts.PENDING || 0 }}
            · 累计失败 {{ queue.failed }}<span v-if="queue.failure_rate != null">（{{ (queue.failure_rate * 100).toFixed(1) }}%）</span>
          </li>
        </ul>
        <p>待核账的模型请求 {{ health.uncertain_model_requests }} 笔</p>
      </template>
      <button @click="inspectQueues">更新服务状态</button>
    </details>
  </section>
</template>
