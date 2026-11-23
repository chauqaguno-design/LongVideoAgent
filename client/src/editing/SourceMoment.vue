<script setup>
import { computed, ref, watch } from 'vue'
import { request } from './editApi.js'
import { interval } from './projectView.js'

const props = defineProps({ assets: Array, moment: Object })
const media = computed(() => props.assets.find(a => a.media_revision_id === props.moment.media_revision_id))
const url = ref(''), error = ref(''), video = ref(null)
function seek() { if (video.value) video.value.currentTime = props.moment.range.start_us / 1e6 }
watch(() => [props.moment, media.value?.playback_artifact_id], async (_, __, cleanup) => {
  const controller = new AbortController()
  cleanup(() => controller.abort())
  error.value = ''
  const id = media.value?.playback_artifact_id
  if (!id) { url.value = ''; error.value = '此素材的播放文件尚未就绪。'; return }
  try {
    const file = await request('/artifacts/' + id, undefined, controller.signal)
    if (controller.signal.aborted) return
    if (url.value === file.url) seek()
    else url.value = file.url
  } catch (cause) { if (!controller.signal.aborted) { url.value = ''; error.value = cause.message } }
}, { immediate: true })
</script>
<template>
  <section class="edit-card moment">
    <h3>{{ media?.filename || '原片' }}</h3>
    <p>原片时间 {{ interval(moment.range) }}</p>
    <video v-if="url" :key="url" ref="video" :src="url" controls preload="metadata" aria-label="证据原片回看" @loadedmetadata="seek" />
    <p v-if="error" role="status">{{ error }}</p>
  </section>
</template>
