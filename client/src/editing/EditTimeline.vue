<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { clipDraft, listAll, makeCommand, request, seconds } from './editApi.js'
import { timecode } from './projectView.js'
import RenderJobs from './RenderJobs.vue'

const props = defineProps({ editId: String, assets: Array, initialRevision: String })
const root = '/edit-projects/' + props.editId
const submit = makeCommand()
const revisions = ref([])
const head = ref('')
const version = ref('')
const revision = computed(() => revisions.value.find(r => r.revision_id === version.value))
const current = computed(() => head.value === version.value && !!revision.value)
const sourceId = ref('')
const asset = computed(() => props.assets.find(a => a.media_revision_id === sourceId.value))
const sourceUrl = ref('')
const sourceError = ref('')
const player = ref(null)
const start = ref(0)
const end = ref('')
const audio = ref('keep')
const replacing = ref('')
const useAligned = ref(false)
const busy = ref(false)
const error = ref('')

function selectSource(id) {
  sourceId.value = id
  useAligned.value = false
  start.value = 0
  end.value = asset.value ? seconds(asset.value.duration_us) : ''
}

watch(
  () => props.assets,
  assets => {
    if (!assets.some(a => a.media_revision_id === sourceId.value)) selectSource(assets[0]?.media_revision_id || '')
  },
  { immediate: true }
)

watch(
  asset,
  async (source, _, onCleanup) => {
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    sourceUrl.value = sourceError.value = ''
    const id = source?.playback_artifact_id
    if (!id) return
    try {
      const artifact = await request('/artifacts/' + id, undefined, controller.signal)
      if (!controller.signal.aborted) sourceUrl.value = artifact.url
    } catch (cause) {
      if (!controller.signal.aborted) sourceError.value = cause.message
    }
  },
  { immediate: true }
)

async function load() {
  const [edit, history] = await Promise.all([request(root), listAll(root + '/revisions')])
  revisions.value = history.sort((a, b) => b.revision_no - a.revision_no)
  head.value = edit.head_revision_id
  version.value = head.value
  replacing.value = ''
}

async function refresh() {
  busy.value = true
  error.value = ''
  try {
    await load()
  } catch (cause) {
    error.value = cause.message
  } finally {
    busy.value = false
  }
}

async function patch(operation, reason) {
  busy.value = true
  error.value = ''
  try {
    await submit(root + '/operations', {
      expected_revision: revisions.value.find(r => r.revision_id === head.value).revision_no,
      reason,
      operations: [operation],
    })
    await load()
  } catch (cause) {
    error.value = cause.status === 409 ? '版本发生冲突。请刷新时间线后重新确认修改。' : cause.message
  } finally {
    busy.value = false
  }
}

async function saveClip() {
  let clip
  const previous = revision.value.clips.find(c => c.clip_id === replacing.value)
  try {
    clip = clipDraft(asset.value, start.value, end.value, audio.value)
    if (
      previous?.media_revision_id === clip.media_revision_id &&
      previous.source_start_us === clip.source_start_us &&
      previous.source_end_us === clip.source_end_us
    ) {
      clip.evidence_ids = previous.evidence_ids
    }
    if (useAligned.value) {
      busy.value = true
      const pinned = previous?.media_revision_id === clip.media_revision_id && previous.caption_track_id
      const id = pinned || (await request('/assets/' + asset.value.asset_id + '/caption-track')).caption_track_id
      if (!id) throw new Error('此素材尚无对齐字幕。请先在素材处理页提取内嵌字幕或语音。')
      clip.caption_track_id = id
    }
  } catch (cause) {
    error.value = cause.message
    busy.value = false
    return
  }
  return replacing.value
    ? patch({ op: 'replace_clip', clip_id: replacing.value, clip }, '替换原片区间')
    : patch({ op: 'append_clip', clip }, '追加原片区间')
}

function showClip(clip, replace = false) {
  selectSource(clip.media_revision_id)
  start.value = seconds(clip.source_start_us)
  end.value = seconds(clip.source_end_us)
  audio.value = clip.audio_policy
  useAligned.value = !!clip.caption_track_id
  replacing.value = replace ? clip.clip_id : ''
  if (player.value?.readyState) player.value.currentTime = start.value
}

function move(index, step) {
  const order = revision.value.clips.map(clip => clip.clip_id)
  const destination = index + step
  ;[order[index], order[destination]] = [order[destination], order[index]]
  return patch({ op: 'reorder_clips', clip_ids: order }, '调整片段顺序')
}

const filename = id => props.assets.find(a => a.media_revision_id === id)?.filename || '已不可用的素材'
const clipWeight = clip => Math.max(1, clip.source_end_us - clip.source_start_us)

onMounted(async () => {
  await refresh()
  if (revisions.value.some(r => r.revision_id === props.initialRevision)) version.value = props.initialRevision
})
</script>

<template>
  <section class="edit-timeline">
    <div class="studio-subbar">
      <div class="edit-toolbar">
        <label v-if="revisions.length">
          时间线版本
          <select v-model="version" :disabled="busy" @change="replacing = ''">
            <option v-for="r in revisions" :key="r.revision_id" :value="r.revision_id">
              V{{ r.revision_no }}{{ r.revision_id === head ? '（当前）' : '' }}
            </option>
          </select>
        </label>
        <button :disabled="busy" @click="refresh">刷新时间线</button>
        <button
          v-if="revision && !current"
          class="edit-primary"
          :disabled="busy"
          @click="patch({ op: 'restore_revision', source_revision_id: version }, '从历史版本恢复')"
        >
          恢复为新版本
        </button>
      </div>
    </div>

    <div v-if="revision" class="edit-columns">
      <section class="edit-card">
        <div class="edit-card-header">
          <h2>原片监视器</h2>
          <small v-if="asset">总长 {{ timecode(asset.duration_us) }}</small>
        </div>

        <label v-if="assets.length">
          视频
          <select :value="sourceId" @change="selectSource($event.target.value)">
            <option v-for="a in assets" :key="a.asset_id" :value="a.media_revision_id">{{ a.filename }}</option>
          </select>
        </label>
        <p v-else>请先为项目添加视频。</p>

        <video
          v-if="sourceUrl"
          ref="player"
          :key="sourceUrl"
          :src="sourceUrl"
          controls
          preload="metadata"
          aria-label="原片回看"
          @loadedmetadata="player.currentTime = Number(start) || 0"
        />
        <p v-else-if="asset && !asset.playback_artifact_id">此素材暂无浏览器可播放的版本，可按原片时间剪辑。</p>
        <p v-if="sourceError" role="alert">{{ sourceError }}</p>

        <form v-if="asset && current" @submit.prevent="saveClip">
          <div class="edit-times">
            <label>
              入点（秒）
              <input v-model="start" type="number" min="0" :max="seconds(asset.duration_us)" step="0.000001" required />
            </label>
            <button v-if="sourceUrl" type="button" @click="start = Number(player.currentTime.toFixed(6))">取当前入点</button>
            <label>
              出点（秒）
              <input v-model="end" type="number" min="0" :max="seconds(asset.duration_us)" step="0.000001" required />
            </label>
            <button v-if="sourceUrl" type="button" @click="end = Number(player.currentTime.toFixed(6))">取当前出点</button>
          </div>
          <div class="edit-toolbar">
            <label>
              声音
              <select v-model="audio">
                <option value="keep">保留原声</option>
                <option value="mute">静音</option>
              </select>
            </label>
            <label><input v-model="useAligned" type="checkbox" /> 使用素材的对齐字幕</label>
            <button class="edit-primary" :disabled="busy">{{ replacing ? '保存替换' : '添加到时间线' }}</button>
            <button v-if="replacing" type="button" @click="replacing = ''">取消替换</button>
          </div>
        </form>
      </section>

      <section class="edit-card">
        <div class="edit-card-header">
          <h2>时间线 <small>{{ revision.clips.length }} 段 · {{ seconds(revision.total_duration_us) }} 秒</small></h2>
          <label>
            输出字幕
            <select
              :value="revision.caption_policy"
              :disabled="busy || !current"
              @change="patch({ op: 'set_caption_policy', caption_policy: $event.target.value }, '更新输出字幕')"
            >
              <option value="none">关闭</option>
              <option value="preserve_source">保留为字幕轨</option>
              <option value="burn_aligned">烧录到画面</option>
            </select>
          </label>
        </div>

        <div v-if="revision.clips.length" class="nle-track-panel" aria-hidden="true">
          <div class="nle-track-meta">
            <span>V1 SEQUENCE · V{{ revision.revision_no }}</span>
            <span>0:00 → {{ timecode(revision.total_duration_us) }}</span>
          </div>
          <div class="nle-track-rail">
            <div
              v-for="(clip, idx) in revision.clips"
              :key="clip.clip_id"
              class="nle-track-seg"
              :style="{ flex: clipWeight(clip) + ' 1 0%' }"
              @click="showClip(clip)"
            >
              <strong>#{{ idx + 1 }}</strong>
              <span>{{ seconds(clip.source_start_us) }}–{{ seconds(clip.source_end_us) }}s</span>
            </div>
          </div>
        </div>

        <p v-if="!revision.clips.length">选择原片的入点和出点，添加第一个片段。</p>
        <ol class="edit-clips">
          <li v-for="(clip, index) in revision.clips" :key="clip.clip_id">
            <div class="clip-row-head">
              <div>
                <span class="clip-index">{{ index + 1 }}</span>
                <button class="edit-link" @click="showClip(clip)">{{ filename(clip.media_revision_id) }}</button>
              </div>
              <div class="clip-meta">
                {{ seconds(clip.source_start_us) }} – {{ seconds(clip.source_end_us) }} 秒 ·
                {{ clip.audio_policy === 'mute' ? '静音' : '原声' }}
              </div>
            </div>
            <div v-if="current" class="edit-actions">
              <button :disabled="busy || !index" @click="move(index, -1)">上移</button>
              <button :disabled="busy || index === revision.clips.length - 1" @click="move(index, 1)">下移</button>
              <button :disabled="busy" @click="showClip(clip, true)">替换区间</button>
              <button class="edit-danger" :disabled="busy" @click="patch({ op: 'remove_clip', clip_id: clip.clip_id }, '移除片段')">移除</button>
            </div>
          </li>
        </ol>
      </section>
    </div>

    <p v-if="error" role="alert">{{ error }}</p>
    <RenderJobs v-if="revision" :key="revision.revision_id" :revision="revision" :assets="assets" />
  </section>
</template>
