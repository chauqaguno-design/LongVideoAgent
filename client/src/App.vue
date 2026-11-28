<script setup>
import StudioWorkspace from './editing/StudioWorkspace.vue'
</script>

<template>
  <StudioWorkspace>
    <template #brand>
      <a href="/" class="brand-mark" aria-label="CutSieve 剪筛工作台">
        <svg class="brand-icon" viewBox="0 0 32 32" fill="none" aria-hidden="true">
          <rect x="2" y="2" width="28" height="28" rx="7" fill="#10151f" stroke="#263244" stroke-width="1.5" />
          <rect x="7" y="9" width="7.5" height="14" rx="2" fill="#132629" stroke="#2dd4bf" stroke-width="1.6" />
          <rect x="17.5" y="9" width="7.5" height="14" rx="2" fill="#271d0f" stroke="#f59e0b" stroke-width="1.6" />
          <path d="M16 5.5v21" stroke="#fbbf24" stroke-width="1.6" stroke-linecap="round" />
        </svg>
        <span class="brand-text"><strong>CutSieve</strong><span class="brand-sub">剪筛工作台</span></span>
      </a>

    </template>
  </StudioWorkspace>
</template>
