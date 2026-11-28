<script setup>
import { computed, ref, shallowRef, watch } from 'vue'
import { clipDraft, fileExpired, renderArtifact, request, seconds } from './editApi.js'
import { interval, timecode, usableMedia } from './projectView.js'

const props = defineProps({
  asset: Object,
  intent: Object,
  availablePreview: Object,
  trimDisabled: Boolean,
  editId: String,
  saveClip: { type: Function, required: true },
})

const mode = ref('source')
const player = ref(null)
const ready = ref(false)
const file = shallowRef(null)
const chosenPreview = shallowRef(null)
const range = ref(null)
const position = ref(0)
const reload = ref(0)
const loading = ref(false)
const playbackError = ref('')
const missing = ref(false)
const start = ref(0)
const end = ref('')
const audio = ref('keep')
const replacing = ref('')
const saving = ref(false)
const trimError = ref('')
let sourcePosition = 0
let playbackIntent = null
let stopAt = null

const preview = computed(() => chosenPreview.value || props.availablePreview)
const artifactId = computed(() => mode.value === 'source' ? props.asset?.playback_artifact_id : preview.value?.file.artifact_id)
const playbackKey = computed(() => [mode.value, artifactId.value, reload.value].join(':'))
const trimAvailable = computed(() => mode.value === 'source' && usableMedia(props.asset) && !props.trimDisabled)
const duration = computed(() => usableMedia(props.asset) ? seconds(props.asset.duration_us) : undefined)
const unsupported = computed(() => mode.value === 'preview' && file.value?.mime_type === 'video/x-matroska' &&
  !document.createElement('video').canPlayType(file.value.mime_type))
const title = computed(() => {
  if (mode.value === 'source') return props.asset?.filename || '原片'
  return preview.value?.file.kind === 'EXPORT' ? '剪辑成片' : '成片预览'
})
const clock = computed(() => mode.value === 'source' && range.value
  ? interval(range.value)
  : timecode(Math.round(position.value * 1_000_000)))

function requestPlayback(at, autoplay = false, endAt = null) {
  playbackIntent = { at, autoplay, endAt }
  position.value = at
  if (player.value?.error || (file.value && fileExpired(file.value))) {
    reload.value++
  } else if (player.value?.dataset.playbackKey === playbackKey.value && player.value.readyState >= 1) {
    applyPlayback()
  }
}

function applyPlayback() {
  const video = player.value
  if (!video || video.dataset.playbackKey !== playbackKey.value) return
  const intent = playbackIntent || { at: mode.value === 'source' ? sourcePosition : 0 }
  playbackIntent = null
  stopAt = intent.endAt
  video.currentTime = intent.at
  ready.value = true
  if (intent.autoplay) {
    Promise.resolve(video.play()).catch(cause => {
      if (video !== player.value || cause.name === 'AbortError') return
      playbackError.value = cause.name === 'NotAllowedError' ? '点击播放以继续' : '暂时无法播放此视频'
    })
  }
}

function updatePosition() {
  const video = player.value
  if (stopAt != null && video.currentTime >= stopAt) {
    const endpoint = stopAt
    stopAt = null
    video.pause()
    video.currentTime = endpoint
  }
  position.value = video.currentTime
  if (mode.value === 'source') sourcePosition = position.value
}

function switchMode(value) {
  if (value === mode.value) return
  mode.value = value
  playbackError.value = ''
  requestPlayback(value === 'source' ? sourcePosition : 0)
}

watch(
  () => [props.asset?.asset_id, duration.value, props.intent],
  ([id, length, intent], [oldId, oldLength, oldIntent] = []) => {
    if (id !== oldId) {
      sourcePosition = 0
      range.value = null
      missing.value = false
      start.value = 0
      end.value = length ?? ''
      audio.value = 'keep'
      if (mode.value === 'source') requestPlayback(0)
    } else if (length != null && oldLength == null && end.value === '') {
      end.value = length
    }
    if (!intent || intent === oldIntent) return
    playbackError.value = ''
    if (intent.mode === 'preview') {
      chosenPreview.value = intent.preview
      mode.value = 'preview'
      requestPlayback(0, true)
      return
    }
    mode.value = 'source'
    range.value = intent.moment || null
    missing.value = intent.missing || false
    const moment = intent.moment
    if (moment) {
      const at = seconds(moment.start_us)
      const endAt = seconds(moment.end_us)
      start.value = at
      end.value = endAt > at ? endAt : length ?? ''
      audio.value = moment.audio_policy || 'keep'
      if (moment.clip_id) replacing.value = moment.replace ? moment.clip_id : ''
      requestPlayback(at, true, endAt > at ? endAt : null)
    } else {
      requestPlayback(sourcePosition)
    }
  },
  { immediate: true },
)

watch(() => props.editId, () => { replacing.value = '' })

watch(
  playbackKey,
  async (_, __, onCleanup) => {
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    file.value = null
    ready.value = false
    stopAt = null
    playbackError.value = ''
    const id = artifactId.value
    loading.value = Boolean(id)
    if (!id) return
    try {
      const currentPreview = mode.value === 'preview' ? preview.value?.file : null
      const resolved = currentPreview
        ? fileExpired(currentPreview)
          ? await renderArtifact(id, currentPreview.kind, currentPreview.timeline_revision_id, controller.signal)
          : currentPreview
        : await request('/artifacts/' + id, undefined, controller.signal)
      if (controller.signal.aborted) return
      if (!resolved.url) throw new Error('视频播放文件暂不可用')
      file.value = resolved
    } catch (cause) {
      if (!controller.signal.aborted) playbackError.value = cause.message
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  },
  { immediate: true },
)

function markTime(which) {
  const video = player.value
  if (!video || video.readyState < 1) return
  const point = which === 'start' ? start : end
  point.value = Number(video.currentTime.toFixed(6))
}

async function save() {
  if (!trimAvailable.value || saving.value) return
  trimError.value = ''
  saving.value = true
  try {
    await props.saveClip(clipDraft(props.asset, start.value, end.value, audio.value), replacing.value)
    replacing.value = ''
  } catch (cause) {
    trimError.value = cause.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="studio-pane monitor-pane" aria-labelledby="monitor-heading">
    <header class="pane-header monitor-header">
      <h2 id="monitor-heading">监视器</h2>
      <div class="mode-switch" role="group" aria-label="监视器模式">
        <button :aria-pressed="mode === 'source'" @click="switchMode('source')">原片</button>
        <button :aria-pressed="mode === 'preview'" :disabled="!preview" @click="switchMode('preview')">成片预览</button>
      </div>
    </header>
    <div class="monitor-meta">
      <span class="monitor-title" :title="title">{{ title }}</span>
      <span v-if="mode === 'preview' && preview?.revisionNo != null" class="monitor-version mono">V{{ preview.revisionNo }}</span>
      <output class="timecode mono" aria-label="监视器时间码" aria-live="off">{{ clock }}</output>
    </div>
    <div class="monitor-stage">
      <video
        v-if="file && !unsupported"
        :key="playbackKey"
        ref="player"
        :data-playback-key="playbackKey"
        :src="file.url"
        controls
        playsinline
        preload="metadata"
        aria-label="监视器播放"
        @loadedmetadata="applyPlayback"
        @timeupdate="updatePosition"
        @playing="playbackError = ''"
        @error="playbackError = '暂时无法播放此视频'"
      />
      <div v-else class="monitor-empty">
        <span class="monitor-frame-icon" aria-hidden="true"></span>
        <p v-if="loading">正在读取播放文件…</p>
        <p v-else-if="playbackError">{{ playbackError }}</p>
        <p v-else-if="missing">此片段的原片已不可用</p>
        <p v-else-if="unsupported">请下载 MKV 后在本地播放器观看</p>
        <p v-else-if="asset">{{ usableMedia(asset) ? '此素材暂无可播放版本' : '正在等待视频就绪' }}</p>
        <p v-else>添加视频以开始剪辑</p>
      </div>
      <span v-if="file && playbackError" class="monitor-feedback" role="status">{{ playbackError }}</span>
    </div>
    <form class="quick-trim" aria-label="原片快速剪辑" @submit.prevent="save">
      <div class="trim-point">
        <label for="trim-in">入点（秒）</label>
        <div>
          <input id="trim-in" v-model="start" class="mono" type="number" min="0" :max="duration" step="0.000001" :disabled="!trimAvailable || saving" required />
          <button type="button" aria-label="取当前入点" :disabled="!trimAvailable || !ready || saving" @click="markTime('start')">取当前</button>
        </div>
      </div>
      <div class="trim-point">
        <label for="trim-out">出点（秒）</label>
        <div>
          <input id="trim-out" v-model="end" class="mono" type="number" min="0" :max="duration" step="0.000001" :disabled="!trimAvailable || saving" required />
          <button type="button" aria-label="取当前出点" :disabled="!trimAvailable || !ready || saving" @click="markTime('end')">取当前</button>
        </div>
      </div>
      <label class="trim-audio">
        声音
        <select v-model="audio" :disabled="!trimAvailable || saving">
          <option value="keep">原声</option>
          <option value="mute">静音</option>
        </select>
      </label>
      <div class="trim-submit">
        <button class="primary" :disabled="!trimAvailable || saving">{{ replacing ? '保存替换' : '加入时间线' }}</button>
        <button v-if="replacing" type="button" class="bare-button" :disabled="saving" @click="replacing = ''">取消替换</button>
      </div>
      <p v-if="trimError" class="inline-error" role="alert">{{ trimError }}</p>
    </form>
  </section>
</template>
