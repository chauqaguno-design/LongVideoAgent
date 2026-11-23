<script setup>
import { computed } from 'vue'
import { request } from './editApi.js'
import { exportFormat } from './projectView.js'

const props = defineProps({ file: { type: Object, required: true }, revisionId: String,
  caption: { type: String, default: '下载' } })
const emit = defineEmits(['error'])
const format = computed(() => exportFormat(props.file.mime_type))
const filename = computed(() => 'videomind-' + props.revisionId + (format.value ? '.' + format.value.extension : ''))

async function download(event) {
  if (!props.file.url_expires_at || Date.parse(props.file.url_expires_at) > Date.now() + 1000) return
  event.preventDefault()
  try {
    const fresh = await request('/artifacts/' + props.file.artifact_id)
    if (fresh.kind !== 'EXPORT' || fresh.timeline_revision_id !== props.revisionId) {
      throw new Error('导出文件与当前剪辑版本不符')
    }
    const link = document.createElement('a')
    link.href = fresh.url
    link.download = filename.value
    link.click()
  } catch (cause) { emit('error', cause.message) }
}
</script>

<template>
  <a :href="file.url" :download="filename" @click="download">{{ caption }} {{ format?.label || '视频' }}</a>
</template>
