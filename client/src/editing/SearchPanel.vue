<script setup>
import { computed, onUnmounted, ref } from 'vue'
import { makeCommand, request } from './editApi.js'
import { interval, label, timecode } from './projectView.js'
import SourceMoment from './SourceMoment.vue'

const props = defineProps({ projectId: String, assets: Array, editId: String })
const emit = defineEmits(['workflow', 'edited'])
const text = ref(''), image = ref(null), imageUrl = ref(''), revision = ref(''), mode = ref('LOCAL')
const result = ref(null), details = ref({}), playing = ref(null), busy = ref(false), error = ref('')
const lastQuery = ref(null), previews = ref({}), crop = ref(false)
const exactDialogue = ref(false)
const roi = ref({ x: 0, y: 0, width: 1, height: 1 })
const input = ref(null), submit = makeCommand()
const scope = computed(() => revision.value ? { project_id: props.projectId, media_revision_ids: [revision.value] } : { project_id: props.projectId })
const filename = id => props.assets.find(a => a.media_revision_id === id)?.filename || '原片'
function choose(file) {
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  image.value = file || null
  crop.value = false
  imageUrl.value = file ? URL.createObjectURL(file) : ''
}
onUnmounted(() => { if (imageUrl.value) URL.revokeObjectURL(imageUrl.value) })
async function action(fn) {
  busy.value = true
  error.value = ''
  try { await fn() } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function search() {
  const body = { scope: scope.value, query_mode: mode.value, top_k: 20 }
  if (text.value.trim()) body.text = exactDialogue.value ? '台词：' + text.value.trim() : text.value.trim()
  if (image.value) {
    const data = new FormData()
    data.append('file', image.value)
    const file = await request('/projects/' + props.projectId + '/query-images', data)
    const cropped = crop.value && await request('/projects/' + props.projectId + '/query-images/' + file.artifact_id + '/roi', roi.value)
    body.image_artifact_id = cropped ? cropped.artifact_id : file.artifact_id
  }
  result.value = null
  previews.value = {}
  details.value = {}
  playing.value = null
  result.value = await request('/search', body)
  lastQuery.value = body
  const ids = result.value.hits.map(hit => hit.evidence_ids[0]).filter(Boolean)
  if (ids.length) previews.value = await request('/projects/' + props.projectId + '/evidence-previews', ids)
}
async function verifySearch() {
  const run = await submit('/projects/' + props.projectId + '/search-workflows', { search: lastQuery.value })
  emit('workflow', { run_id: run.run_id })
}
async function evidence(hit) {
  const rows = await Promise.all(hit.evidence_ids.map(id => request('/evidence/' + id)))
  details.value = { ...details.value, [hit.evidence_ids[0]]: rows }
  return rows
}
async function watchHit(hit) {
  const rows = details.value[hit.evidence_ids[0]] || await evidence(hit)
  playing.value = rows[0] || hit
}
async function selectClip(hit) {
  const path = '/edit-projects/' + props.editId
  const edit = await request(path)
  await submit(path + '/operations', { expected_revision: edit.head_revision, reason: '从检索结果添加',
    operations: [{ op: 'append_clip', clip: {
      media_revision_id: hit.media_revision_id, source_start_us: hit.range.start_us, source_end_us: hit.range.end_us,
      evidence_ids: hit.evidence_ids, audio_policy: 'keep',
    } }] })
  emit('edited', props.editId)
}
function inspect(hit) {
  emit('workflow', { intent: 'INSPECT', scope: { project_id: props.projectId, media_revision_ids: [hit.media_revision_id] },
    message: '复核 ' + interval(hit.range) + '，查找：' + (text.value || '与查询截图一致的画面') })
}
</script>
<template>
  <section class="edit-card">
    <form class="research-query" @submit.prevent="action(search)">
      <label>搜索素材<select v-model="revision"><option value="">项目全部视频</option>
        <option v-for="a in assets" :key="a.asset_id" :value="a.media_revision_id">{{ a.filename }}</option>
      </select></label>
      <label>搜索方式<select v-model="mode"><option value="LOCAL">找相关片段</option><option value="FIRST">找第一次</option>
        <option value="ALL">找全部</option><option value="COUNT">统计次数</option></select></label>
      <label class="wide">台词、动作或场景<input v-model="text" type="search" placeholder="例如，第一次提到车站" /></label>
      <label><input v-model="exactDialogue" type="checkbox" /> 按台词原文核查</label>
      <label>查询截图<input ref="input" type="file" accept="image/jpeg,image/png,image/webp" @change="choose($event.target.files[0])" /></label>
      <div v-if="imageUrl" class="query-image"><img :src="imageUrl" alt="待检索的截图"
        @load="roi = { x: 0, y: 0, width: $event.target.naturalWidth, height: $event.target.naturalHeight }" />
        <button type="button" @click="choose(null); input.value = ''">移除截图</button></div>
      <label v-if="imageUrl"><input v-model="crop" type="checkbox" /> 只搜索截图的一部分</label>
      <div v-if="crop" class="edit-times">
        <label v-for="(name, key) in { x: '左边距', y: '上边距', width: '宽度', height: '高度' }" :key="key">
          {{ name }}（像素）<input v-model.number="roi[key]" type="number" :min="['x','y'].includes(key) ? 0 : 1" required />
        </label>
      </div>
      <button class="edit-primary" :disabled="busy || !assets.length || !(text.trim() || image)">搜索片段</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="result">
      <p role="status">找到 {{ result.candidate_count }} 个候选，当前展示 {{ result.hits.length }} 个。</p>
      <button :disabled="busy" @click="action(verifySearch)">按本次查询核查原片</button>
      <p v-for="w in result.warnings" :key="w">{{ w }}</p>
      <details v-if="result.coverage_summary?.unverified_ranges.length"><summary>尚未核查的区间</summary><ul>
        <li v-for="r in result.coverage_summary.unverified_ranges" :key="r.media_revision_id + ':' + r.range.start_us">
          {{ filename(r.media_revision_id) }} · {{ interval(r.range) }}</li></ul></details>
      <div class="edit-columns research-results">
        <ol class="run-list"><li v-for="hit in result.hits" :key="hit.evidence_ids[0] || hit.segment_id">
          <figure v-if="previews[hit.evidence_ids[0]]?.thumbnail" class="hit-thumb">
            <img :src="previews[hit.evidence_ids[0]].thumbnail" alt="原片采样帧" loading="lazy" />
            <figcaption>{{ timecode(previews[hit.evidence_ids[0]].frame_us) }}</figcaption>
          </figure>
          <strong>{{ filename(hit.media_revision_id) }}</strong> · {{ hit.verified ? '已核查' : '候选' }}
          <button class="edit-link" @click="action(() => watchHit(hit))">{{ interval(hit.range) }} 回看</button>
          <p v-if="previews[hit.evidence_ids[0]]">{{ label(previews[hit.evidence_ids[0]].source) }} · {{ previews[hit.evidence_ids[0]].text }}</p>
          <p>{{ hit.route_ranks.map(r => r.route).join(' · ') }}</p>
          <div class="edit-actions"><button :disabled="busy" @click="action(() => evidence(hit))">查看依据</button>
            <button :disabled="busy" @click="inspect(hit)">局部复核</button>
            <button v-if="editId" :disabled="busy" @click="action(() => selectClip(hit))">添加到当前剪辑</button></div>
          <ul v-if="details[hit.evidence_ids[0]]"><li v-for="e in details[hit.evidence_ids[0]]" :key="e.evidence_id">{{ e.claim }} · {{ label(e.support) }}</li></ul>
        </li></ol>
        <SourceMoment v-if="playing" :assets="assets" :moment="playing" />
      </div>
    </template>
  </section>
</template>
