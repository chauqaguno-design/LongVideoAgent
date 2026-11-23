<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { listAll, makeCommand, request } from './editApi.js'
import { interval, label, pending } from './projectView.js'
import SourceMoment from './SourceMoment.vue'

const props = defineProps({ assets: Array })
const emit = defineEmits(['removed'])
const selected = ref(''), runs = ref([]), coverage = ref([]), observations = ref([])
const info = ref(null), error = ref(''), busy = ref(false)
const notes = ref(null), moment = ref(null), next = ref(null)
const removing = ref(false)
const charges = ref({})
async function showCharges(id) {
  try { charges.value = { ...charges.value, [id]: await request('/processing-runs/' + id + '/usage') } }
  catch (cause) { error.value = cause.message }
}
const submit = makeCommand()
const active = computed(() => runs.value.find(pending))
let timer, controller
watch(() => props.assets, list => {
  if (!list.some(a => a.asset_id === selected.value)) selected.value = list[0]?.asset_id || ''
}, { immediate: true })

async function load() {
  clearTimeout(timer)
  controller?.abort()
  const current = controller = new AbortController()
  const id = selected.value
  if (!id) return
  const root = '/assets/' + id
  try {
    const [asset, history, ledger, page] = await Promise.all([
      request(root, undefined, current.signal), listAll(root + '/processing-runs', current.signal),
      request(root + '/coverage?limit=100', undefined, current.signal),
      request(root + '/observations?limit=30', undefined, current.signal),
    ])
    if (current.signal.aborted) return
    info.value = asset
    runs.value = history
    coverage.value = ledger.items
    next.value = ledger.next_cursor
    observations.value = page.items
    error.value = ''
    if (active.value) timer = setTimeout(load, 1500)
  } catch (cause) { if (!current.signal.aborted) error.value = cause.message }
}
watch(selected, () => {
  info.value = null; notes.value = null; moment.value = null; next.value = null; removing.value = false
  runs.value = []; coverage.value = []; observations.value = []; load()
}, { immediate: true })
onUnmounted(() => { clearTimeout(timer); controller?.abort() })

async function change(cancel) {
  busy.value = true
  try {
    if (cancel) await submit('/processing-runs/' + active.value.processing_run_id + '/cancel', { reason: '用户取消处理' })
    else await submit('/assets/' + selected.value + '/processing-runs', {
      requested_capabilities: ['asr', 'ocr', 'visual_observation', 'text_embedding', 'image_embedding'],
      force_rebuild: false,
    })
    await load()
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function more() {
  busy.value = true
  try {
    const page = await request('/assets/' + selected.value + '/coverage?' + new URLSearchParams({ cursor: next.value, limit: 100 }))
    coverage.value.push(...page.items)
    next.value = page.next_cursor
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function readNotes() {
  const id = selected.value
  busy.value = true
  error.value = ''
  try {
    const ref = await request('/assets/' + id + '/notes')
    const file = await request('/artifacts/' + ref.artifact_id)
    const response = await fetch(file.url, { credentials: 'omit' })
    if (!response.ok) throw new Error('笔记文件暂不可用')
    const data = await response.json()
    if (id === selected.value) notes.value = data
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function removeAsset() {
  busy.value = true
  try {
    await request('/assets/' + selected.value, undefined, undefined, 'DELETE')
    removing.value = false
    emit('removed')
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>
<template>
  <section class="edit-card">
    <div class="edit-toolbar">
      <label>查看处理的视频<select v-model="selected"><option v-for="a in assets" :key="a.asset_id" :value="a.asset_id">{{ a.filename }}</option></select></label>
      <button :disabled="!selected || busy || !!active" @click="change(false)">{{ runs.length ? '重试处理' : '开始处理' }}</button>
      <button v-if="active" :disabled="busy || !!active.cancel_requested_at" @click="change(true)">取消处理</button>
      <button :disabled="!selected" @click="load">刷新进度</button>
    </div>
    <p v-if="!assets.length">请先上传视频。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul v-if="info?.capabilities?.length" class="cap-list">
      <li v-for="c in info.capabilities" :key="c.name">{{ label(c.name) }} · {{ label(c.state) }} <small>{{ c.reason }}</small></li>
    </ul>
    <ol class="run-list">
      <li v-for="r in runs" :key="r.processing_run_id"><strong>{{ label(r.status) }}</strong> · {{ label(r.stage) }}
        <span>已处理 {{ r.completed_units }}{{ r.total_units == null ? ' 个单元，总量待确认' : ' / ' + r.total_units }}</span>
        <span v-if="r.cancel_requested_at">已请求取消</span><p v-if="r.error">{{ r.error.message }}</p>
        <button class="edit-link" @click="showCharges(r.processing_run_id)">查看画面分析用量</button>
        <p v-if="charges[r.processing_run_id]">
          模型 {{ charges[r.processing_run_id].model_calls }} 次 · 图片 {{ charges[r.processing_run_id].images }} 张 ·
          已确认 {{ charges[r.processing_run_id].committed_microunits / 1000000 }} 元
          <span v-if="charges[r.processing_run_id].uncertain_microunits"> · 另有请求待核账，预留 {{ charges[r.processing_run_id].uncertain_microunits / 1000000 }} 元</span>
        </p>
      </li>
    </ol>
    <div v-if="runs.some(r => ['PARTIAL', 'SUCCEEDED'].includes(r.status))" class="edit-toolbar">
      <button :disabled="busy" @click="readNotes">打开观影笔记</button>
    </div>
    <div v-if="notes" class="edit-columns">
      <div><p>{{ notes.coverage_note }}</p>
        <details v-if="notes.overview?.highlights.length"><summary>全片速览</summary>
          <ul><li v-for="highlight in notes.overview.highlights" :key="highlight.evidence_id">
            <button class="edit-link" @click="moment = { media_revision_id: info.media_revision_id, range: highlight.range }">{{ interval(highlight.range) }}</button>
            {{ highlight.text }} · {{ label(highlight.support) }}</li></ul>
        </details>
        <details v-for="chapter in notes.chapters" :key="chapter.start_us" open>
        <summary>{{ interval(chapter) }}</summary><ul><li v-for="claim in chapter.claims" :key="claim.evidence_id">
          <button class="edit-link" @click="moment = { media_revision_id: info.media_revision_id, range: claim.range }">{{ interval(claim.range) }}</button>
          {{ claim.text }} · {{ label(claim.source) }} · {{ label(claim.support) }}</li></ul></details></div>
      <SourceMoment v-if="moment" :assets="assets" :moment="moment" />
    </div>
    <details v-if="coverage.length"><summary>查看时间覆盖</summary>
      <ul><li v-for="c in coverage" :key="c.coverage_id">{{ interval(c.range) }} · {{ label(c.modality) }} · {{ label(c.stage) }} {{ c.failure_reason }}</li></ul>
      <button v-if="next" :disabled="busy" @click="more">查看更多区间</button>
    </details>
    <details v-if="observations.length"><summary>查看原始观察</summary>
      <ul><li v-for="o in observations" :key="o.observation_id">{{ interval(o.range) }} · {{ label(o.modality) }} · {{ o.text }}</li></ul>
    </details>
    <details v-if="info"><summary>移除素材</summary>
      <p>移除《{{ info.filename }}》会取消相关处理任务，停止提供原片、检索证据及依赖它的剪辑成片。</p>
      <button v-if="!removing" @click="removing = true">移除此素材</button>
      <template v-else><button :disabled="busy" @click="removeAsset">确认移除《{{ info.filename }}》</button>
      <button @click="removing = false">保留素材</button></template>
    </details>
  </section>
</template>
