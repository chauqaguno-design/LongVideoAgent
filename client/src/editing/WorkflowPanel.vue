<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { apiRequest } from '../api.js'
import { consumeStream } from '../taskEvents.js'
import { listAll, makeCommand, request } from './editApi.js'
import { label, pending } from './projectView.js'
import WorkflowTurn from './WorkflowTurn.vue'

const props = defineProps({ projectId: String, assets: Array, draft: Object })
const emit = defineEmits(['edited', 'media-library'])
const root = '/projects/' + props.projectId
const history = ref([])
const selected = ref('')
const turns = ref([])
const titles = ref({})
const run = ref(null)
const result = ref(null)
const events = ref([])
const media = ref(props.draft?.scope?.media_revision_ids?.[0] || '')
const message = ref(props.draft?.message || '')
const limits = ref({})
const reason = ref('')
const error = ref('')
const channel = ref('')
const busy = ref(false)
const optimistic = ref(null)
const submit = makeCommand()
let controller
let timer
let cursor
let mounted = false

const last = computed(() => turns.value.at(-1))
const shownTurns = computed(() => (optimistic.value ? [...turns.value, optimistic.value] : turns.value))
const file = computed(() => props.assets.find(a => a.media_revision_id === media.value))
const waiting = computed(() => run.value?.status === 'AWAITING_INPUT')
const canContinue = computed(() => run.value?.status === 'SUCCEEDED' && last.value?.media_revision_id === media.value)
const canSend = computed(() => file.value && message.value.trim() && !busy.value && (!selected.value || waiting.value || canContinue.value))
const budgetFields = {
  max_model_calls: '模型调用上限',
  max_tool_calls: '工具调用上限',
  max_images: '图片上限',
  max_cost_microunits: '费用上限（百万分之一货币单位）',
  deadline_seconds: '时限（秒）',
}

watch(
  () => props.assets,
  rows => {
    if (!media.value && !selected.value && rows.length === 1) media.value = rows[0].media_revision_id
  },
  { immediate: true }
)
watch(
  () => run.value?.budget_usage?.limits,
  value => {
    if (value && !reason.value) limits.value = { ...value }
  }
)

async function historyList() {
  const items = await listAll(root + '/workflow-runs')
  if (mounted) history.value = items
}

function remember(id) {
  const params = new URLSearchParams(location.hash.split('?')[1])
  if (params.get('project') !== props.projectId) return
  if (id) params.set('run', id)
  else params.delete('run')
  params.set('view', 'workflow')
  window.history.replaceState(window.history.state, '', '#edit?' + params)
}

function stop() {
  controller?.abort()
  clearTimeout(timer)
}

async function snapshot(id, signal) {
  const path = '/workflow-runs/' + id
  const [state, thread] = await Promise.all([request(path, undefined, signal), request(path + '/conversation', undefined, signal)])
  const output = state.result_artifact_id ? await request(path + '/result', undefined, signal) : null
  if (signal.aborted || selected.value !== id) return
  turns.value = thread.turns
  for (const turn of thread.turns) titles.value[turn.run_id] = turn.message
  media.value = thread.turns.at(-1)?.media_revision_id || ''
  run.value = state
  result.value = output
  optimistic.value = null
}

async function follow(id, signal) {
  try {
    await snapshot(id, signal)
    if (signal.aborted) return
    channel.value = ''
    if (!pending(run.value)) return
    const headers = { Accept: 'text/event-stream' }
    if (cursor) headers['Last-Event-ID'] = cursor
    const response = await apiRequest('/v1/workflow-runs/' + id + '/events', { headers, signal })
    if (!response.ok) throw new Error('事件流暂不可用')
    channel.value = ''
    await consumeStream(
      response.body,
      async event => {
        if (event.run_id !== id || event.sequence <= (events.value.at(-1)?.sequence || 0)) return
        cursor = event.event_id
        events.value.push(event)
        await snapshot(id, signal)
      },
      signal
    )
  } catch (cause) {
    if (signal.aborted) return
    if ([401, 403, 404].includes(cause.status)) {
      error.value = cause.message
      return
    }
    channel.value = cause.message + '。正在重新读取任务状态。'
  }
  if (!signal.aborted && (!run.value || pending(run.value))) timer = setTimeout(() => follow(id, signal), 1200)
}

function select(id, keepTurns = false) {
  stop()
  selected.value = id
  remember(id)
  run.value = null
  result.value = null
  events.value = []
  cursor = ''
  channel.value = ''
  error.value = ''
  reason.value = ''
  if (!keepTurns) {
    turns.value = []
    optimistic.value = null
    message.value = ''
  }
  if (!id) return
  controller = new AbortController()
  follow(id, controller.signal)
}

function newChat() {
  select('')
  if (!file.value && props.assets.length === 1) media.value = props.assets[0].media_revision_id
}

function changeMedia(id) {
  newChat()
  media.value = id
}

async function command(fn) {
  if (busy.value) return
  error.value = ''
  busy.value = true
  try {
    await fn()
  } catch (cause) {
    if (mounted) error.value = cause.message
  } finally {
    busy.value = false
  }
}

async function send() {
  if (!canSend.value) return
  const text = message.value.trim()
  const parent = canContinue.value ? selected.value : null
  await command(async () => {
    if (waiting.value) {
      await submit('/workflow-runs/' + selected.value + '/resume', {
        expected_state_version: run.value.state_version,
        message: text,
      })
      if (!mounted) return
      message.value = ''
      select(selected.value, true)
    } else {
      const body = { message: text, media_revision_id: media.value }
      if (parent) body.parent_run_id = parent
      optimistic.value = { run_id: 'sending', message: text, status: 'QUEUED' }
      try {
        const next = await submit(root + '/conversation-runs', body)
        if (!mounted) return
        message.value = ''
        select(next.run_id, true)
      } catch (cause) {
        optimistic.value = null
        throw cause
      }
    }
    await historyList()
  })
}

async function changeRun(kind) {
  const path = '/workflow-runs/' + selected.value
  if (kind === 'cancel') await submit(path + '/cancel', { reason: '用户取消任务' })
  else if (kind === 'retry') {
    await submit(path + '/resume', {
      expected_state_version: run.value.state_version,
      message: '重试本轮任务',
    })
  } else {
    await submit(
      path + '/budget',
      { expected_state_version: run.value.state_version, new_limits: limits.value, reason: reason.value },
      'PATCH'
    )
  }
  if (!mounted) return
  select(selected.value, true)
  await historyList()
}

onMounted(() => {
  mounted = true
  const deepLink = new URLSearchParams(location.hash.split('?')[1]).get('run')
  const id = props.draft?.run_id || (!props.draft && deepLink)
  if (id) select(id)
  historyList().catch(cause => {
    if (mounted) error.value = cause.message
  })
})
onUnmounted(() => {
  mounted = false
  stop()
})
</script>

<template>
  <section class="conversation" aria-label="视频对话">
    <header class="conversation-header">
      <div>
        <h2>视频对话</h2>
        <p v-if="file">{{ file.filename }}</p>
      </div>
      <div class="conversation-actions">
        <label v-if="history.length">
          对话历史
          <select :value="selected" :disabled="busy" @change="select($event.target.value)">
            <option value="">新对话</option>
            <option v-for="r in history" :key="r.run_id" :value="r.run_id">
              {{ titles[r.run_id] || r.message || label(r.status) + ' · ' + r.run_id.slice(0, 8) }}
            </option>
          </select>
        </label>
        <button :disabled="busy" @click="newChat">新对话</button>
      </div>
    </header>

    <div v-if="!assets.length && !selected" class="conversation-empty">
      <h3>先添加一部视频</h3>
      <p>上传完成后，可以查台词、定位画面，或直接描述想剪的片段。</p>
      <button class="edit-primary" @click="emit('media-library')">去添加视频</button>
    </div>

    <div v-else-if="!shownTurns.length && !selected" class="conversation-empty">
      <h3>你想找哪一段？</h3>
      <p>可以从一句台词、一个动作，或一段明确的剪辑时间开始。</p>
      <div class="prompt-hints">
        <span class="prompt-hint-chip">哪一段提到了关键台词？</span>
        <span class="prompt-hint-chip">剪出第 1 到 3 秒并导出</span>
        <span class="prompt-hint-chip">前面再留两秒，生成预览</span>
      </div>
    </div>

    <div class="conversation-log" aria-label="对话记录">
      <WorkflowTurn
        v-for="turn in shownTurns"
        :key="turn.run_id"
        :turn="turn"
        :assets="assets"
        :project-id="projectId"
        :active="turn.run_id === selected"
        :result="turn.run_id === selected ? result : null"
        @edited="(edit, rev) => emit('edited', edit, rev)"
      />
    </div>

    <div v-if="run" class="conversation-progress">
      <p role="status">
        <strong>{{ label(run.status) }}</strong> · {{ label(run.phase) }}
        <span v-if="run.cancel_requested_at">已请求取消</span>
        <button class="edit-link" :disabled="busy" @click="select(selected, true)">刷新任务</button>
        <button v-if="pending(run) || waiting" class="edit-link" :disabled="busy" @click="command(() => changeRun('cancel'))">
          取消任务
        </button>
      </p>
      <p v-if="run.error" role="alert">{{ run.error.message }}</p>
      <button v-if="run.status === 'FAILED' && run.error?.retryable" class="edit-primary" :disabled="busy" @click="command(() => changeRun('retry'))">
        重试本轮
      </button>

      <details v-if="events.length">
        <summary>任务记录</summary>
        <ol>
          <li v-for="e in events" :key="e.event_id">
            {{ e.payload.summary || e.payload.message || label(e.payload.phase || e.payload.status || e.type) }}
          </li>
        </ol>
      </details>

      <details v-if="run.budget_usage">
        <summary>用量与预算</summary>
        <p>模型 {{ run.budget_usage.model_calls }} 次 · 工具 {{ run.budget_usage.tool_calls }} 次 · 图片 {{ run.budget_usage.images }} 张</p>
        <p v-if="!run.budget_usage.model_calls && !run.budget_usage.committed_microunits">本轮未调用模型</p>
        <p v-else-if="run.budget_usage.committed_microunits == null">模型费用待核算</p>
        <p v-else>按配置估算费用 {{ run.budget_usage.committed_microunits / 1000000 }} {{ run.budget_usage.currency }}</p>
        <p v-if="run.budget_usage.uncertain_microunits">
          另有 {{ run.budget_usage.uncertain_microunits / 1000000 }} {{ run.budget_usage.currency }} 预占费用待核账。
        </p>
        <p v-if="run.budget_usage.reserved_microunits">
          进行中的请求已预留 {{ run.budget_usage.reserved_microunits / 1000000 }} {{ run.budget_usage.currency }}。
        </p>
        <p v-if="run.budget_usage.model_calls">
          输入 {{ run.budget_usage.input_tokens }} tokens · 输出 {{ run.budget_usage.output_tokens }} tokens · 价格版本
          {{ run.budget_usage.price_version || '待核对' }}
        </p>
        <form v-if="pending(run) || waiting" class="research-query" @submit.prevent="command(() => changeRun('budget'))">
          <label v-for="(name, key) in budgetFields" :key="key">
            {{ name }}
            <input v-model.number="limits[key]" type="number" min="0" required />
          </label>
          <label>调整原因<input v-model.trim="reason" required /></label>
          <button :disabled="busy || !reason">保存预算</button>
        </form>
      </details>
    </div>

    <p v-if="channel" role="status">{{ channel }}</p>
    <p v-if="error" role="alert">{{ error }}</p>

    <form v-if="assets.length || selected" class="conversation-composer" @submit.prevent="send">
      <div v-if="waiting && run.awaiting_input" class="conversation-clarification">
        <strong>需要你确认</strong>
        <p>{{ run.awaiting_input.question }}</p>
        <button v-for="opt in run.awaiting_input.options" :key="opt" type="button" @click="message = opt">{{ opt }}</button>
      </div>
      <label for="conversation-message">{{ waiting ? '补充说明' : '问题或剪辑要求' }}</label>
      <textarea
        id="conversation-message"
        v-model="message"
        rows="3"
        maxlength="4000"
        :placeholder="waiting ? '在这里补充要求…' : '例如，找到提到车钥匙的片段，再剪出第二段。（Ctrl+Enter 发送）'"
        @keydown.ctrl.enter.prevent="send"
        @keydown.meta.enter.prevent="send"
      />
      <div class="composer-controls">
        <label>
          对话视频
          <select :value="media" :disabled="busy" @change="changeMedia($event.target.value)">
            <option value="">选择一部视频</option>
            <option v-for="a in assets" :key="a.asset_id" :value="a.media_revision_id">{{ a.filename }}</option>
          </select>
        </label>
        <button class="edit-primary" :disabled="!canSend">{{ waiting ? '继续任务' : '发送' }}</button>
      </div>
    </form>
  </section>
</template>
