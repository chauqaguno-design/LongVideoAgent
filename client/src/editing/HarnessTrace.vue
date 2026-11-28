<script setup>
import { computed } from 'vue'
import { label } from './projectView.js'

const props = defineProps({
  runs: { type: Array, default: () => [] },
  selectedRunId: { type: String, default: null },
  trace: { type: Object, default: null },
  loading: Boolean,
  error: { type: String, default: null },
})
const emit = defineEmits(['select-run', 'refresh'])

const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 })
const count = value => Number.isFinite(value) ? number.format(value) : '—'
const tokens = item => count(item?.input_tokens) + ' / ' + count(item?.output_tokens)
const elapsed = ms => ms < 1000 ? count(ms) + ' ms' : (ms / 1000).toFixed(1) + ' s'
const hasNumber = Number.isFinite
const eventTypes = {
  MODEL_TURN: { title: '模型轮次', icon: '◈', tone: 'ready' },
  IMAGE_UNLOAD: { title: '帧卸载', icon: '↓', tone: 'ready' },
  COMPACTION: { title: '上下文压缩', icon: '⇥', tone: 'partial' },
  SELF_REPAIR: { title: '自修复', icon: '↻', tone: 'partial' },
  RENDER_WAIT: { title: '等待渲染', icon: '◷', tone: '' },
}
const stepStatuses = {
  SUCCEEDED: { text: '成功', tone: 'ready' },
  FAILED: { text: '失败', tone: 'failed' },
  RUNNING: { text: '执行中', tone: 'running' },
  WAITING: { text: '等待中', tone: 'partial' },
  CANCELLED: { text: '已取消', tone: '' },
}

const options = computed(() => (props.runs || []).filter(run => run?.run_id))
const selected = computed(() => options.value.find(run => run.run_id === props.selectedRunId))
const runStatus = computed(() => props.trace?.status || selected.value?.status)
const runLabel = status => status === 'WAITING_USER' ? '等待补充信息' : label(status)
const steps = computed(() => (props.trace?.tool_steps || []).filter(Boolean).map(step => ({
  ...step,
  badge: stepStatuses[step.status] || (typeof step.ok === 'boolean'
    ? stepStatuses[step.ok ? 'SUCCEEDED' : 'FAILED']
    : { text: label(step.status) || '状态未知', tone: '' }),
})))
const events = computed(() => (props.trace?.trace_events || []).filter(Boolean).map(event => ({
  ...event,
  tag: eventTypes[event.kind] || { title: '轨迹事件', icon: '·', tone: '' },
})))
const usage = computed(() => props.trace?.usage)
const metrics = computed(() => [
  { title: '模型轮次', value: count(usage.value?.model_calls) },
  { title: '工具调用', value: count(usage.value?.tool_calls ?? (props.trace?.tool_steps ? steps.value.length : null)) },
  { title: '自修复', value: count(steps.value.filter(step => step.repaired === true).length) },
  { title: '帧卸载', value: count(usage.value?.images_unloaded) },
  { title: '上下文压缩', value: count(usage.value?.compactions) },
])
const warnings = computed(() => (props.trace?.warnings || [])
  .map(warning => typeof warning === 'string' ? warning : warning?.message || warning?.code)
  .filter(Boolean))

function runTitle(run) {
  const prompt = run.message || run.prompt
  return prompt?.trim().replace(/\s+/g, ' ').slice(0, 80) ||
    [runLabel(run.status) || '运行', run.run_id.slice(0, 8)].join(' · ')
}
</script>

<template>
  <div class="drawer-body harness-trace">
    <div class="trace-controls">
      <select
        aria-label="选择运行"
        :value="selectedRunId || ''"
        :disabled="!options.length"
        @change="emit('select-run', $event.target.value)"
      >
        <option v-if="!selected" :value="selectedRunId || ''">{{ selectedRunId ? '当前运行' : '选择运行' }}</option>
        <option v-for="run in options" :key="run.run_id" :value="run.run_id">{{ runTitle(run) }}</option>
      </select>
      <button type="button" :disabled="!selectedRunId || loading" @click="emit('refresh')">刷新轨迹</button>
    </div>

    <p v-if="!selectedRunId" class="trace-empty muted">暂无运行记录。在智能剪辑助手发送消息后，可在这里查看轨迹。</p>
    <p v-if="loading" class="muted" role="status">正在读取运行轨迹…</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="selectedRunId && !trace && !loading && !error" class="trace-empty muted">暂无轨迹记录</p>

    <template v-if="selectedRunId && trace">
      <p v-if="runStatus" class="trace-run-status muted">运行状态 <strong>{{ runLabel(runStatus) }}</strong></p>
      <dl class="trace-kpis" aria-label="Harness 用量">
        <div v-for="metric in metrics" :key="metric.title">
          <dt>{{ metric.title }}</dt>
          <dd class="mono">{{ metric.value }}</dd>
        </div>
        <div class="trace-token-kpi">
          <dt>Token（入 / 出）与费用</dt>
          <dd class="mono">
            <span>{{ tokens(usage) }}</span>
            <span>{{ hasNumber(usage?.estimated_cost_cny) ? '¥' + count(usage.estimated_cost_cny) : '—' }}</span>
          </dd>
        </div>
      </dl>
      <ul v-if="warnings.length" class="trace-warnings" aria-label="运行警告">
        <li v-for="(warning, index) in warnings" :key="index">{{ warning }}</li>
      </ul>

      <section class="drawer-section" aria-labelledby="trace-steps-heading">
        <h3 id="trace-steps-heading">执行时间线</h3>
        <ol v-if="steps.length" class="trace-steps">
          <li v-for="(step, index) in steps" :key="step.tool_call_id || index" class="trace-step">
            <div class="trace-badges">
              <span v-if="hasNumber(step.step_index)" class="trace-index mono">#{{ step.step_index }}</span>
              <code class="trace-tool mono">{{ step.name || '工具' }}</code>
              <span class="status-pill" :data-tone="step.badge.tone">{{ step.badge.text }}</span>
              <span v-if="step.repaired === true" class="status-pill trace-repair" data-tone="partial">已自修复</span>
              <span v-if="hasNumber(step.duration_ms)" class="status-pill mono">{{ elapsed(step.duration_ms) }}</span>
              <span v-if="step.images_added > 0" class="status-pill" data-tone="ready">+{{ count(step.images_added) }} 帧</span>
              <code v-if="step.error_code" class="status-pill mono" data-tone="failed">{{ step.error_code }}</code>
            </div>
            <p v-if="step.summary">{{ step.summary }}</p>
            <code v-if="step.args_preview?.trim()" class="trace-args mono">{{ step.args_preview }}</code>
          </li>
        </ol>
        <p v-else class="trace-empty muted">暂无工具调用记录</p>
      </section>

      <section class="drawer-section" aria-labelledby="trace-events-heading">
        <h3 id="trace-events-heading">上下文与生命周期事件</h3>
        <ol v-if="events.length" class="trace-events">
          <li v-for="(event, index) in events" :key="index" class="trace-event" :data-kind="event.kind">
            <div class="trace-badges">
              <span v-if="hasNumber(event.step)" class="trace-index mono">#{{ event.step }}</span>
              <span class="status-pill" :data-tone="event.tag.tone"><span aria-hidden="true">{{ event.tag.icon }}</span>{{ event.tag.title }}</span>
              <code v-if="event.kind" class="trace-kind mono">{{ event.kind }}</code>
            </div>
            <p v-if="event.summary">{{ event.summary }}</p>
            <div class="trace-event-meta mono">
              <span v-if="hasNumber(event.input_tokens) || hasNumber(event.output_tokens)">Token {{ tokens(event) }}</span>
              <span v-if="hasNumber(event.duration_ms)">{{ elapsed(event.duration_ms) }}</span>
            </div>
          </li>
        </ol>
        <p v-else class="trace-empty muted">暂无生命周期事件记录</p>
      </section>
    </template>
  </div>
</template>
