<script setup>
import { computed, ref, watch } from 'vue'
import { request } from './editApi.js'
import { interval, label, timecode } from './projectView.js'
import SourceMoment from './SourceMoment.vue'
import ExportDownload from './ExportDownload.vue'

const props = defineProps({ turn: Object, result: Object, assets: Array, projectId: String, active: Boolean })
const emit = defineEmits(['edited'])
const playing = ref(null), saved = ref(null), preview = ref(null), download = ref(null)
const error = ref(''), busy = ref(false), occurrences = ref([]), next = ref(null), opened = ref(false)
const timeline = ref(null)
const output = computed(() => props.result || saved.value || {})
const edit = computed(() => output.value.edit_project_id || props.turn.edit_project_id)
const revision = computed(() => output.value.revision_id || props.turn.revision_id)
const planLabels = { SEARCH: '查找片段', INSPECT: '复核画面', ANSWER: '整理回答',
  EDIT: '编排时间线', PREVIEW: '生成预览', EXPORT: '导出成片' }
const plan = computed(() => props.turn.route?.plan || [])
const answer = computed(() => props.turn.answer || output.value.answer)
watch(() => props.result, value => { if (value) saved.value = value }, { immediate: true })
watch(() => [output.value.edit_project_id, output.value.revision_id], async ([id, rev], _, cleanup) => {
  timeline.value = null
  if (!id || !rev) return
  const controller = new AbortController()
  cleanup(() => controller.abort())
  try {
    const data = await request('/edit-projects/' + id + '/revisions/' + rev, undefined, controller.signal)
    if (!controller.signal.aborted && data.revision_id === rev) timeline.value = data
  } catch (err) { if (!controller.signal.aborted) error.value = err.message }
}, { immediate: true })
async function action(fn) {
  busy.value = true; error.value = ''
  try { await fn() } catch (err) { error.value = err.message } finally { busy.value = false }
}
async function resolveFile(id, kind, signal) {
  const file = await request('/artifacts/' + id, undefined, signal)
  if (file.kind !== kind || file.timeline_revision_id !== revision.value) throw new Error('此成片与当前轮次的时间线版本不符')
  return file
}
watch(() => output.value.export_artifact_id, async (id, _, cleanup) => {
  download.value = null
  if (!id) return
  const controller = new AbortController()
  cleanup(() => controller.abort())
  try {
    const file = await resolveFile(id, 'EXPORT', controller.signal)
    if (!controller.signal.aborted) download.value = file
  } catch (err) { if (!controller.signal.aborted) error.value = err.message }
}, { immediate: true })
async function more() {
  const path = '/projects/' + props.projectId + '/query-results/' + output.value.complete_event_list_artifact_id + '/occurrences'
  const page = await request(path + (next.value ? '?cursor=' + encodeURIComponent(next.value) : ''))
  occurrences.value.push(...page.items); next.value = page.next_cursor; opened.value = true
}
async function showEvidence(id) { playing.value = await request('/evidence/' + id) }
</script>

<template>
  <article class="conversation-turn" :data-run-id="turn.run_id">
    <p class="conversation-user">{{ turn.message }}</p>
    <div class="conversation-response">
      <header><strong>ReelCue</strong><small>{{ label(turn.status) }}</small></header>
      <ol v-if="plan.length" class="conversation-plan" aria-label="任务计划">
        <li v-for="(step, i) in plan" :key="i">{{ planLabels[step] || step }}</li>
      </ol>
      <div v-for="(item, i) in turn.clarifications || []" :key="i" class="clarified">
        <p>{{ item.question }}</p><p class="conversation-user">{{ item.reply }}</p>
      </div>
      <div class="workflow-output">
        <p v-if="answer" class="turn-answer">{{ answer }}</p>
        <p v-if="output.brief?.goal && output.brief.goal !== turn.message">{{ output.brief.goal }}</p>
        <p v-if="timeline" class="turn-answer">成片 {{ timecode(timeline.total_duration_us) }}，共 {{ timeline.clips.length }} 段。</p>
        <ul v-if="timeline" class="output-clips"><li v-for="(clip, n) in timeline.clips" :key="clip.clip_id">
          第 {{ n + 1 }} 段 · 原片
          <button class="edit-link" @click="playing = { media_revision_id: clip.media_revision_id, range: { start_us: clip.source_start_us, end_us: clip.source_end_us } }">
            {{ interval({ start_us: clip.source_start_us, end_us: clip.source_end_us }) }}
          </button>
        </li></ul>
        <ol v-if="turn.candidates?.length" class="turn-candidates" aria-label="候选片段">
          <li v-for="(c, i) in turn.candidates" :key="i">
            <strong>候选 {{ i + 1 }}</strong>
            <button class="edit-link" @click="playing = c">{{ interval(c.range) }}</button>
            <span>{{ c.text }}</span><small>{{ label(c.support) }}</small>
          </li>
        </ol>
        <template v-if="output.complete_event_list_artifact_id">
          <button v-if="!opened || next" :disabled="busy" @click="action(more)">{{ opened ? '继续读取清单' : '查看全部匹配清单' }}</button>
          <ol v-if="opened"><li v-for="event in occurrences" :key="event.occurrence_id">
            <button class="edit-link" @click="playing = event">{{ interval(event.range) }}</button> {{ event.text }}</li></ol>
          <p v-if="opened && !occurrences.length">本次核查范围内没有匹配项。</p>
        </template>
        <ul v-if="output.claims?.length"><li v-for="(claim, i) in output.claims" :key="i">{{ claim.text }} · {{ label(claim.support) }}
          <button v-for="(e, n) in claim.evidence_ids" :key="e" class="edit-link" @click="action(() => showEvidence(e))">回看依据 {{ n + 1 }}</button>
        </li></ul>
        <p v-for="warning in output.warnings || output.gaps || []" :key="warning">{{ warning }}</p>
        <div class="conversation-actions">
          <button v-if="!active && !saved && turn.status === 'SUCCEEDED'" :disabled="busy"
            @click="action(async () => { saved = await request('/workflow-runs/' + turn.run_id + '/result') })">查看本轮产物</button>
          <button v-if="edit" @click="emit('edited', edit, revision)">打开本次剪辑</button>
          <button v-if="output.preview_artifact_id" :disabled="busy"
            @click="action(async () => { preview = await resolveFile(output.preview_artifact_id, 'PREVIEW') })">播放本次预览</button>
          <ExportDownload v-if="download" :file="download" :revision-id="revision" caption="下载本次导出的" @error="error = $event" />
        </div>
        <video v-if="preview" :src="preview.url" controls preload="metadata" aria-label="本次剪辑预览" />
        <SourceMoment v-if="playing" :assets="assets" :moment="playing" />
        <p v-if="error" role="alert">{{ error }}</p>
      </div>
      <details v-if="turn.route?.reason" class="route-details"><summary>任务判断依据</summary>{{ turn.route.reason }}</details>
    </div>
  </article>
</template>
