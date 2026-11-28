<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { apiRequest } from '../api.js'
import { label } from './projectView.js'
import UsageMetrics from './UsageMetrics.vue'

defineProps({ latestRun: Object })
const health = ref(null)
const busy = ref(false)
const error = ref('')
const lifecycle = new AbortController()
const queueNames = { processing: '素材索引', workflow: '智能剪辑', render: '视频渲染', outbox: '事件投递' }

async function inspectQueues() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const response = await apiRequest('/v1/readiness', { signal: lifecycle.signal })
    if (!response.ok && response.status !== 503) throw new Error('无法获取工作区服务状态')
    health.value = await response.json()
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  } finally {
    busy.value = false
  }
}

onMounted(inspectQueues)
onUnmounted(() => lifecycle.abort())
</script>

<template>
  <div class="drawer-body system-status">
    <section class="drawer-section">
      <div class="drawer-section-heading">
        <h3>工作队列</h3>
        <button :disabled="busy" @click="inspectQueues">更新服务状态</button>
      </div>
      <p v-if="busy" role="status">正在读取服务状态…</p>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <p v-if="health?.dependency === 'database'" class="inline-error" role="alert">数据库暂时不可用</p>
      <template v-if="health?.queues">
        <p v-if="!health.ready" class="inline-error" role="alert">有后台服务离线，排队任务可能需要等待。</p>
        <ul class="queue-list">
          <li v-for="(queue, key) in health.queues" :key="key">
            <strong>{{ queueNames[key] || key }}</strong>
            <span class="status-pill" :data-tone="queue.online ? 'ready' : 'failed'">
              <i class="status-dot" aria-hidden="true"></i>{{ queue.online ? '在线' : '离线' }}
            </span>
            <span class="muted">排队 <span class="mono">{{ queue.counts?.QUEUED ?? queue.counts?.PENDING ?? 0 }}</span></span>
            <span class="muted">失败 <span class="mono">{{ queue.failed ?? 0 }}</span></span>
          </li>
        </ul>
        <p v-if="health.uncertain_model_requests != null" class="muted">待核账的模型请求 <span class="mono">{{ health.uncertain_model_requests }}</span> 笔</p>
      </template>
    </section>
    <section class="drawer-section">
      <div class="drawer-section-heading">
        <h3>最近一轮用量</h3>
        <span v-if="latestRun" class="muted">{{ label(latestRun.status) }}</span>
      </div>
      <UsageMetrics v-if="latestRun?.usage" :usage="latestRun.usage" />
      <p v-else class="muted">暂无用量记录</p>
    </section>
  </div>
</template>
