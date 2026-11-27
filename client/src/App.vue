<script setup>
import EditStudio from './editing/EditStudio.vue'
</script>

<template>
  <div class="cs-shell">
    <header class="navbar">
      <a href="/" class="brand-mark" aria-label="CutSieve 剪筛工作台首页">
        <svg class="brand-icon" viewBox="0 0 32 32" fill="none" aria-hidden="true">
          <rect x="2" y="2" width="28" height="28" rx="7" fill="#10151f" stroke="#263244" stroke-width="1.5" />
          <rect x="7" y="9" width="7.5" height="14" rx="2" fill="#132629" stroke="#2dd4bf" stroke-width="1.6" />
          <rect x="17.5" y="9" width="7.5" height="14" rx="2" fill="#271d0f" stroke="#f59e0b" stroke-width="1.6" />
          <path d="M16 5.5v21" stroke="#fbbf24" stroke-width="1.6" stroke-linecap="round" />
        </svg>
        <span class="brand-text"><strong>CutSieve</strong><span class="brand-sub">剪筛工作台</span></span>
      </a>

      <span class="workspace-pill">
        <span class="workspace-dot" aria-hidden="true"></span>
        本地工作区
      </span>
    </header>

    <main class="cs-main">
      <EditStudio :library="[]" />
    </main>
  </div>
</template>

<style scoped>
.cs-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  color: var(--cs-text);
}

.navbar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.72rem clamp(0.9rem, 3.5vw, 2.4rem);
  background: rgba(9, 11, 15, 0.84);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--cs-border);
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  gap: 0.65rem;
  color: var(--cs-text);
  text-decoration: none;
}

.brand-icon {
  width: 30px;
  height: 30px;
  flex-shrink: 0;
}

.brand-text {
  display: inline-flex;
  align-items: baseline;
  gap: 0.5rem;
}

.brand-text strong {
  font-size: 1.05rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: #fff;
}

.brand-sub {
  font-size: 0.82rem;
  color: var(--cs-text-secondary);
}

.workspace-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.34rem 0.72rem;
  border-radius: 999px;
  background: var(--cs-surface);
  border: 1px solid var(--cs-border);
  font-size: 0.84rem;
  color: var(--cs-text);
}

.workspace-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--cs-success);
  box-shadow: 0 0 8px rgba(52, 211, 153, 0.7);
}

.cs-main {
  flex: 1;
}

@media (max-width: 560px) {
  .brand-sub {
    display: none;
  }
}
</style>
