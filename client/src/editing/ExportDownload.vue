<script setup>
import { computed } from 'vue'
import { fileExpired, renderArtifact } from './editApi.js'
import { exportFormat } from './projectView.js'

const props = defineProps({ file: { type: Object, required: true }, revisionId: String,
  caption: { type: String, default: '下载' } })
const emit = defineEmits(['error'])
const format = computed(() => exportFormat(props.file.mime_type))
const revision = computed(() => props.revisionId || props.file.timeline_revision_id)
const filename = computed(() => 'cutsieve-' + revision.value + (format.value ? '.' + format.value.extension : ''))

async function download(event) {
  if (!fileExpired(props.file)) return
  event.preventDefault()
  try {
    const fresh = await renderArtifact(props.file.artifact_id, 'EXPORT', revision.value)
    const link = document.createElement('a')
    link.href = fresh.url
    link.download = filename.value
    link.click()
  } catch (cause) { emit('error', cause.message) }
}
</script>

<template>
  <a class="download-link" :href="file.url" :download="filename" @click="download">{{ caption }} {{ format?.label || '视频' }}</a>
</template>
