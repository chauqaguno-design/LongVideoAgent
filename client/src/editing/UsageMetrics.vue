<script setup>
defineProps({ usage: { type: Object, required: true } })

const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 })
const count = value => value == null ? '—' : number.format(value)
</script>

<template>
  <dl class="usage-metrics">
    <div><dt>模型</dt><dd class="mono">{{ count(usage.model_calls) }} 次</dd></div>
    <div><dt>工具</dt><dd class="mono">{{ count(usage.tool_calls) }} 次</dd></div>
    <div><dt>抽帧</dt><dd class="mono">{{ count(usage.images) }} 张</dd></div>
    <div><dt>Tokens（入 / 出）</dt><dd class="mono">{{ count(usage.input_tokens) }} / {{ count(usage.output_tokens) }}</dd></div>
    <div><dt>估算费用</dt><dd class="mono">{{ usage.estimated_cost_cny == null ? '—' : '¥' + count(usage.estimated_cost_cny) }}</dd></div>
  </dl>
</template>
