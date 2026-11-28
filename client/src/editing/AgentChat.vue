<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { apiRequest } from '../api.js'
import { consumeStream } from '../taskEvents.js'
import { listAll, makeCommand, request } from './editApi.js'
import { label, pending } from './projectView.js'
import AgentTurn from './AgentTurn.vue'

const props = defineProps({ projectId: String, editId: String, initialRun: String, assets: { type: Array, default: () => [] } })
const emit = defineEmits(['selected', 'latest-run', 'seek', 'play', 'timeline', 'sync'])
const history = ref([])
const selected = ref('')
const turns = ref([])
const optimistic = ref(null)
const run = ref(null)
const states = ref({})
const outputs = ref({})
const liveSteps = ref({})
const images = ref({})
const scope = ref('')
const message = ref('')
const screenshot = ref(null)
const screenshotInput = ref(null)
const uploading = ref(false)
const composer = ref(null)
const log = ref(null)
const busy = ref(false)
const error = ref('')
const connection = ref('')
const lifecycle = new AbortController()
const submit = makeCommand()
const latestCreated = ref('')
let following
let reconnect
let imageUpload

const root = '/projects/' + props.projectId
const recentRunKey = 'cutsieve-latest-run:' + props.projectId
const waiting = computed(() => run.value?.status === 'AWAITING_INPUT')
const running = computed(() => pending(run.value))
const shownTurns = computed(() => optimistic.value ? [...turns.value, optimistic.value] : turns.value)
const latestId = computed(() => latestCreated.value || history.value[0]?.run_id)
const canCompose = computed(() => !!props.projectId && !busy.value && !running.value && (!selected.value || !!run.value))
const canSend = computed(() => canCompose.value && !uploading.value && message.value.trim() &&
  (!waiting.value || run.value.state_version != null))
const syncKeys = ['edit_project_id', 'revision_id', 'preview_artifact_id', 'export_artifact_id']
const starters = [
  { label: '查找关键台词片段', message: '找出推动情节的关键台词，列出每段的原片时间码。' },
  { label: '定位特定颜色/画面片段', message: '定位出现蓝色物体的画面，并给出原片时间码。' },
  { label: '剪出片段并导出 MP4', message: '剪出最关键的片段，生成预览并导出 MP4。' },
]

function scrollLatest(force = false) {
  const panel = log.value
  if (!panel || (!force && panel.scrollHeight - panel.scrollTop - panel.clientHeight > 96)) return
  nextTick(() => { if (log.value === panel) panel.scrollTop = panel.scrollHeight })
}

function fill(text) {
  message.value = text
  composer.value?.focus()
}

function rememberRun(state, id) {
  const snapshot = { ...state, run_id: id }
  states.value[id] = snapshot
  if (selected.value === id) run.value = snapshot
  if (latestId.value === id) emit('latest-run', snapshot)
}

async function loadHistory() {
  const { signal } = lifecycle
  try {
    const rows = await listAll(root + '/workflow-runs', signal)
    if (signal.aborted) return
    rows.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    const ids = new Set(rows.map(row => row.run_id))
    history.value = [...history.value.filter(row => !ids.has(row.run_id)), ...rows]
    if (latestCreated.value && !history.value.some(row => row.run_id === latestCreated.value)) latestCreated.value = ''
    const id = latestId.value
    if (!id) return
    const known = states.value[id] || history.value.find(row => row.run_id === id)
    if (known?.usage) emit('latest-run', known)
    else if (id !== selected.value) {
      const state = await request('/workflow-runs/' + id, undefined, signal)
      if (!signal.aborted && id === latestId.value) rememberRun(state, id)
    }
  } catch (cause) {
    if (!signal.aborted && cause.status !== 404) error.value = cause.message
  }
}

function trackOutput(context, ...sources) {
  const next = { ...context.change }
  for (const source of sources) {
    if (!source) continue
    if (source.revision_id && next.revision_id && source.revision_id !== next.revision_id) {
      delete next.preview_artifact_id
      delete next.export_artifact_id
    }
    for (const key of syncKeys) {
      if (source[key]) next[key] = source[key]
    }
  }
  context.change = next
  if (!context.track || !Object.keys(next).length) return
  const signature = JSON.stringify(next)
  if (signature === context.synced) return
  context.synced = signature
  emit('sync', next)
}

async function readRun(context) {
  const { id, signal } = context
  const path = '/workflow-runs/' + id
  const [state, thread] = await Promise.all([
    request(path, undefined, signal),
    request(path + '/conversation', undefined, signal),
  ])
  if (signal.aborted) return
  rememberRun(state, id)
  context.track ||= pending(state) || state.status === 'AWAITING_INPUT'
  const tail = thread.turns.find(turn => turn.run_id === id)
  if (tail) {
    turns.value = thread.turns
    if (context.restoreScope) {
      scope.value = tail.media_revision_id || ''
      context.restoreScope = false
    }
  }
  const hasOutput = [tail, state].some(value => value?.revision_id || value?.preview_artifact_id || value?.export_artifact_id)
  if (hasOutput || (!pending(state) && state.status !== 'AWAITING_INPUT')) trackOutput(context, tail, state)
  if (state.result_artifact_id || state.status === 'SUCCEEDED') {
    try {
      const result = await request(path + '/result', undefined, signal)
      if (!signal.aborted) {
        outputs.value[id] = result
        trackOutput(context, result)
      }
    } catch (cause) {
      if (!signal.aborted && !(cause.status === 409 && !state.result_artifact_id)) throw cause
    }
  }
  scrollLatest()
}

function recordStep(id, step, started) {
  if (!step.tool_call_id) return
  const steps = [...(liveSteps.value[id] || [])]
  const index = steps.findIndex(row => row.tool_call_id === step.tool_call_id)
  const next = { ...steps[index], ...step, status: step.status || (started ? 'RUNNING' : step.ok === false ? 'FAILED' : 'SUCCEEDED') }
  if (index === -1) steps.push(next)
  else steps[index] = next
  liveSteps.value[id] = steps
}

function receive(context, event) {
  if (event.run_id !== context.id || event.sequence <= context.sequence) return
  context.sequence = event.sequence
  context.cursor = event.event_id
  const payload = event.payload || {}
  switch (event.type) {
    case 'TOOL_STARTED':
    case 'TOOL_FINISHED':
      recordStep(context.id, payload.tool_step || payload, event.type === 'TOOL_STARTED')
      break
    case 'STATUS':
      if (pending(payload)) {
        rememberRun({ ...run.value, status: payload.status, phase: payload.phase || run.value.phase }, context.id)
      }
      break
    case 'TIMELINE_UPDATED':
      trackOutput(context, payload)
      break
    case 'ARTIFACT_READY': {
      const kind = payload.kind?.toLowerCase()
      trackOutput(context, {
        ...payload,
        preview_artifact_id: kind === 'preview' ? payload.artifact_id : null,
        export_artifact_id: kind === 'export' ? payload.artifact_id : null,
      })
      break
    }
    case 'RUN_COMPLETED':
      trackOutput(context, payload)
      break
  }
  if (Object.keys(context.change).length && !outputs.value[context.id]?.answer) {
    outputs.value[context.id] = { ...context.change }
  }
  scrollLatest()
}

async function follow(context) {
  const { id, signal } = context
  clearTimeout(reconnect)
  try {
    await readRun(context)
    if (signal.aborted) return
    connection.value = ''
    if (!running.value) return
    const headers = { Accept: 'text/event-stream' }
    if (context.cursor) headers['Last-Event-ID'] = context.cursor
    const stream = await apiRequest('/v1/workflow-runs/' + id + '/events', { headers, signal })
    if (!stream.ok || !stream.body) throw new Error('实时事件连接暂不可用')
    await consumeStream(stream.body, event => receive(context, event), signal)
    if (!signal.aborted) await readRun(context)
    if (!running.value) return
  } catch (cause) {
    if (signal.aborted) return
    if ([401, 403, 404].includes(cause.status)) {
      error.value = cause.message
      return
    }
    connection.value = '正在重新读取任务状态…'
  }
  if (!signal.aborted) reconnect = setTimeout(() => follow(context), 1200)
}

function select(id, options = {}) {
  const previous = following
  previous?.controller.abort()
  clearTimeout(reconnect)
  selected.value = id
  emit('selected', id)
  run.value = options.state || null
  connection.value = ''
  error.value = ''
  if (!options.keepThread) {
    turns.value = []
    optimistic.value = null
    scope.value = ''
    message.value = ''
    imageUpload?.abort()
    screenshot.value = null
  }
  if (!id) {
    following = null
    return
  }
  const same = previous?.id === id
  const controller = new AbortController()
  following = {
    id, controller, signal: controller.signal,
    sequence: same ? previous.sequence : 0,
    cursor: same ? previous.cursor : '',
    change: same ? previous.change : {},
    synced: same ? previous.synced : '',
    track: options.track || false,
    restoreScope: !options.keepThread,
  }
  scrollLatest(true)
  follow(following)
}

async function chooseScreenshot(event) {
  const [file] = event.target.files
  event.target.value = ''
  if (!file || uploading.value || !canCompose.value || waiting.value) return
  const controller = new AbortController()
  imageUpload = controller
  uploading.value = true
  error.value = ''
  try {
    const body = new FormData()
    body.append('file', file)
    const artifact = await request(root + '/query-images', body, controller.signal)
    if (controller.signal.aborted) return
    screenshot.value = { id: artifact.artifact_id, url: artifact.url, name: file.name }
    images.value[artifact.artifact_id] = screenshot.value
  } catch (cause) {
    if (!controller.signal.aborted) error.value = cause.message
  } finally {
    uploading.value = false
  }
}

async function command(action) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (cause) {
    if (lifecycle.signal.aborted) return
    error.value = cause.message
    if (cause.status === 409 && following && waiting.value) follow(following)
  } finally {
    busy.value = false
  }
}

function send(reply = message.value) {
  const text = reply.trim()
  if (!text || !canCompose.value || uploading.value) return
  if (waiting.value && run.value.state_version == null) return
  return command(async () => {
    if (waiting.value) {
      const id = selected.value
      const state = await submit('/workflow-runs/' + id + '/resume', {
        expected_state_version: run.value.state_version,
        message: text,
      })
      if (lifecycle.signal.aborted) return
      message.value = ''
      select(id, { keepThread: true, track: true, state: { status: 'QUEUED', ...state } })
      return
    }

    const mediaId = props.assets.some(asset => asset.media_revision_id === scope.value) ? scope.value : null
    const parent = turns.value.at(-1)?.run_id === selected.value ? selected.value : null
    const body = {
      message: text,
      media_revision_id: mediaId,
      edit_project_id: props.editId || null,
      image_artifact_id: screenshot.value?.id || null,
      parent_run_id: parent || null,
    }
    const draft = { ...body, run_id: 'sending', status: 'QUEUED' }
    optimistic.value = draft
    scrollLatest(true)
    try {
      const state = await submit(root + '/conversation-runs', body)
      if (lifecycle.signal.aborted) return
      const id = state.run_id
      const turn = { ...draft, status: state.status || 'QUEUED', run_id: id }
      turns.value = parent ? [...turns.value, turn] : [turn]
      history.value = [turn, ...history.value.filter(row => row.run_id !== id)]
      latestCreated.value = id
      localStorage.setItem(recentRunKey, id)
      message.value = ''
      screenshot.value = null
      select(id, { keepThread: true, track: true, state: { ...state, status: turn.status } })
    } finally {
      optimistic.value = null
    }
  })
}

function cancel() {
  return command(async () => {
    const id = selected.value
    const state = await submit('/workflow-runs/' + id + '/cancel', { reason: '用户取消任务' })
    if (!lifecycle.signal.aborted) select(id, { keepThread: true, track: true, state: { ...run.value, ...state } })
  })
}

const optionText = option => typeof option === 'string' ? option : option.label || option.value
const optionReply = option => typeof option === 'string' ? option : option.value || option.label
const historyTitle = row => (turns.value.find(turn => turn.run_id === row.run_id)?.message || row.message ||
  (label(row.status) || '任务') + ' · ' + row.run_id.slice(0, 8)).slice(0, 80)

watch(() => props.assets, rows => {
  if (scope.value && !rows.some(row => row.media_revision_id === scope.value)) scope.value = ''
})

onMounted(() => {
  if (!props.projectId) return
  latestCreated.value = localStorage.getItem(recentRunKey) || ''
  if (props.initialRun) select(props.initialRun)
  loadHistory()
})
onUnmounted(() => {
  lifecycle.abort()
  following?.controller.abort()
  imageUpload?.abort()
  clearTimeout(reconnect)
})
</script>

<template>
  <section class="studio-pane agent-pane" aria-labelledby="agent-heading">
    <header class="pane-header agent-header">
      <h2 id="agent-heading">智能剪辑助手</h2>
      <div class="conversation-actions">
        <select aria-label="对话历史" :value="selected" :disabled="!projectId || busy || !history.length" @change="select($event.target.value)">
          <option value="">对话历史</option>
          <option v-if="selected && !history.some(row => row.run_id === selected)" :value="selected">当前对话</option>
          <option v-for="row in history" :key="row.run_id" :value="row.run_id">{{ historyTitle(row) }}</option>
        </select>
        <button :disabled="!projectId || busy" @click="select('')">新对话</button>
      </div>
    </header>

    <div ref="log" class="conversation-log" role="log" aria-label="对话记录">
      <div v-if="!shownTurns.length && !selected" class="conversation-empty">
        <h3>你想剪出哪一段？</h3>
        <p>{{ projectId ? '用台词、画面或时间描述你想找的内容。' : '新建项目后，添加视频并开始对话。' }}</p>
        <div class="prompt-hints" aria-label="示例提示词">
          <button v-for="starter in starters" :key="starter.label" class="prompt-hint" :disabled="!projectId" @click="fill(starter.message)">
            {{ starter.label }}<span aria-hidden="true">↗</span>
          </button>
        </div>
      </div>
      <p v-else-if="!shownTurns.length && !error" class="agent-loading muted" role="status">正在读取对话…</p>

      <AgentTurn
        v-for="turn in shownTurns"
        :key="turn.run_id"
        :turn="turn"
        :state="states[turn.run_id]"
        :result="outputs[turn.run_id]"
        :live-steps="liveSteps[turn.run_id]"
        :image="images[turn.image_artifact_id]"
        @seek="emit('seek', $event)"
        @play="emit('play', $event)"
        @timeline="emit('timeline', $event)"
      />

      <section v-if="waiting && run.awaiting_input" class="conversation-clarification" aria-label="需要确认">
        <strong>需要你确认</strong>
        <p>{{ run.awaiting_input.question }}</p>
        <div v-if="run.awaiting_input.options?.length" class="studio-actions">
          <button
            v-for="(option, index) in run.awaiting_input.options"
            :key="index"
            :disabled="busy || run.state_version == null"
            @click="send(optionReply(option))"
          >{{ optionText(option) }}</button>
        </div>
      </section>
    </div>

    <form class="conversation-composer" @submit.prevent="send()">
      <p v-if="connection" class="muted composer-notice" role="status">{{ connection }}</p>
      <p v-if="error" class="inline-error composer-notice" role="alert">{{ error }}</p>
      <div v-if="screenshot" class="attachment-chip">
        <img v-if="screenshot.url" :src="screenshot.url" alt="" />
        <span :title="screenshot.name">{{ screenshot.name }}</span>
        <button type="button" class="bare-button" :disabled="busy" aria-label="移除截图" @click="screenshot = null">×</button>
      </div>
      <label for="conversation-message" class="visually-hidden">问题或剪辑要求</label>
      <textarea
        id="conversation-message"
        ref="composer"
        v-model="message"
        rows="2"
        maxlength="12000"
        :disabled="!projectId || busy"
        :placeholder="waiting ? '补充剪辑要求…' : '描述想找的画面或想剪的片段…'"
        @keydown.ctrl.enter.prevent="send()"
        @keydown.meta.enter.prevent="send()"
      />
      <div class="composer-controls">
        <div class="composer-scope">
          <button type="button" class="bare-button attach-button" :disabled="!canCompose || waiting || uploading" @click="screenshotInput.click()">
            <span aria-hidden="true">📎</span> {{ uploading ? '上传截图中…' : '附带截图' }}
          </button>
          <input
            ref="screenshotInput"
            class="visually-hidden"
            type="file"
            accept="image/png,image/jpeg,image/webp"
            aria-label="附带截图"
            tabindex="-1"
            :disabled="!canCompose || waiting || uploading"
            @change="chooseScreenshot"
          />
          <select v-model="scope" aria-label="对话素材范围" :disabled="!projectId || busy || waiting || running">
            <option value="">全部素材</option>
            <option v-for="asset in assets" :key="asset.asset_id" :value="asset.media_revision_id">{{ asset.filename }}</option>
          </select>
        </div>
        <div class="composer-send">
          <kbd v-if="!running && !waiting" class="mono">Ctrl / ⌘ ↵</kbd>
          <button v-if="running || waiting" type="button" :disabled="busy || !!run.cancel_requested_at" @click="cancel">取消</button>
          <button v-if="!running" class="primary" :disabled="!canSend">{{ waiting ? '继续任务' : '发送' }}</button>
        </div>
      </div>
    </form>
  </section>
</template>
