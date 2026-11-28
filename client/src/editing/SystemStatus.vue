<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiRequest } from '../api.js'
import { fetchCapabilities } from './editApi.js'
import { label } from './projectView.js'
import UsageMetrics from './UsageMetrics.vue'

defineProps({ latestRun: Object })
const health = ref(null)
const busy = ref(false)
const error = ref('')
const capabilities = ref(null)
const capabilityError = ref('')
const lifecycle = new AbortController()
const queueNames = { processing: '素材索引', workflow: '智能剪辑', render: '视频渲染', outbox: '事件投递' }
const serviceLabels = { ASR: 'ASR 语音转写', OCR: 'OCR 画面文字', embedding: '向量检索', FFmpeg: 'FFmpeg' }
const services = computed(() => Object.entries(serviceLabels).map(([name, title]) => ({
  title,
  ...capabilities.value?.dedicated_services?.find(service => service.name === name),
})))

async function inspectQueues() {
  try {
    const response = await apiRequest('/v1/readiness', { signal: lifecycle.signal })
    if (!response.ok && response.status !== 503) throw new Error('无法获取工作区服务状态')
    health.value = await response.json()
  } catch (cause) {
    if (!lifecycle.signal.aborted) error.value = cause.message
  }
}

async function inspectCapabilities() {
  try {
    capabilities.value = await fetchCapabilities(lifecycle.signal)
  } catch {
    if (!lifecycle.signal.aborted) capabilityError.value = '无法读取模型与本地能力'
  }
}

async function refresh() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  capabilityError.value = ''
  try {
    await Promise.all([inspectQueues(), inspectCapabilities()])
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
onUnmounted(() => lifecycle.abort())
</script>

<template>
  <div class="drawer-body system-status">
    <section class="drawer-section">
      <div class="drawer-section-heading">
        <h3>工作队列</h3>
        <button :disabled="busy" @click="refresh">更新服务状态</button>
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
      <h3>模型与本地能力</h3>
      <p v-if="capabilityError" class="inline-error" role="alert">{{ capabilityError }}</p>
      <dl v-if="capabilities" class="capability-details">
        <div>
          <dt>对话模型</dt>
          <dd>
            <span class="status-pill" :data-tone="capabilities.text ? 'ready' : undefined">
              {{ capabilities.text ? '已配置' : '未配置' }}
            </span>
            <code v-if="capabilities.text" class="capability-version mono">{{ capabilities.general_model }}</code>
            <code v-if="capabilities.text && capabilities.resolved_model_version !== capabilities.general_model"
                  class="capability-version mono">{{ capabilities.resolved_model_version }}</code>
            <span class="status-pill" :data-tone="capabilities.native_tools ? 'ready' : undefined">
              {{ capabilities.native_tools ? '支持工具' : '工具未启用' }}
            </span>
            <span class="status-pill" :data-tone="capabilities.vision ? 'ready' : undefined">
              {{ capabilities.vision ? '支持视觉' : '视觉未启用' }}
            </span>
          </dd>
        </div>
        <div v-for="service in services" :key="service.title">
          <dt>{{ service.title }}</dt>
          <dd>
            <span class="status-pill" :data-tone="service.ready ? 'ready' : service.configured ? 'partial' : undefined">
              {{ service.ready ? '已就绪' : service.configured ? '不可用' : '未配置' }}
            </span>
            <template v-if="service.versions">
              <code v-for="(version, space) in service.versions" :key="space"
                    class="capability-version mono" :title="version">{{ space === 'text' ? '文字' : '图像' }} {{ version }}</code>
            </template>
            <code v-else-if="service.version" class="capability-version mono">{{ service.version }}</code>
          </dd>
        </div>
      </dl>
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
