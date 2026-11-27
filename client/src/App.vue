<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { apiRequest, captureAuthSession, clearAuthToken, onAuthSessionChange, setAuthToken } from './api.js'
import EditStudio from './editing/EditStudio.vue'

const user = ref(null)
const sessionError = ref('')
const auth = ref(null)
const mode = ref('login')
const fields = ref({ username: '', password: '', nickname: '' })
const busy = ref(false)
const message = ref('')
const error = ref(false)
let unsubscribe

async function loadUser() {
  const session = captureAuthSession()
  user.value = null
  if (!session.token) return
  try {
    const response = await apiRequest('/user/me')
    const current = response.ok ? await response.json() : null
    if (!session.isCurrent()) return
    if (response.status === 401) clearAuthToken()
    else if (response.ok) user.value = current
    else sessionError.value = await response.text()
  } catch (cause) {
    if (session.isCurrent()) sessionError.value = cause.message
  }
}

onMounted(() => {
  unsubscribe = onAuthSessionChange(loadUser)
  void loadUser()
})
onUnmounted(() => unsubscribe?.())

function openAuth(initialMode = 'login') {
  sessionError.value = ''
  mode.value = initialMode
  fields.value = { username: '', password: '', nickname: '' }
  message.value = ''
  error.value = false
  auth.value.showModal()
}

async function submit() {
  if (busy.value) return
  const session = captureAuthSession()
  const action = mode.value
  busy.value = true
  error.value = false
  message.value = ''
  try {
    const response = await apiRequest('/user/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(fields.value),
    })
    if (!response.ok) throw new Error(await response.text())
    const result = await response.json()
    if (!session.isCurrent() || !auth.value.open) return
    if (action === 'register') {
      mode.value = 'login'
      message.value = '注册成功，请登录'
    } else {
      setAuthToken(result.token)
      user.value = result.userInfo
      auth.value.close()
    }
  } catch (cause) {
    if (session.isCurrent()) {
      error.value = true
      message.value = cause.message
    }
  } finally {
    busy.value = false
  }
}

function logout() {
  apiRequest('/user/logout', { method: 'POST' }).catch(() => {})
  clearAuthToken()
}
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

      <div v-if="user" class="nav-user">
        <span class="user-pill">
          <span class="user-dot" aria-hidden="true"></span>
          <span>{{ user.nickname || user.username }}</span>
        </span>
        <button class="nav-btn" @click="logout">退出登录</button>
      </div>
      <div v-else class="nav-actions">
        <button class="nav-btn nav-btn-primary" @click="openAuth('login')">登录 / 注册</button>
      </div>
    </header>

    <main class="cs-main">
      <EditStudio v-if="user" :key="user.id" :library="[]" />

      <section v-else class="welcome-stage">
        <div class="welcome-copy">
          <p class="hero-eyebrow">LOCAL-FIRST VIDEO RETRIEVAL &amp; ROUGH-CUT STUDIO</p>
          <h1>把长视频筛成<span class="accent-word">有据可查</span>的粗剪时间线</h1>
          <p class="hero-desc">
            本地单进程运行，自动提取时间对齐的字幕、语音、画面文字与关键帧。用自然语言查找片段，核实原片时间码后直接在版本化时间线上编排、预览并导出成片。
          </p>
          <div class="hero-cta">
            <button class="hero-primary-btn" @click="openAuth('login')">登录工作台</button>
            <button class="hero-secondary-btn" @click="openAuth('register')">创建本地账号</button>
          </div>
          <p v-if="sessionError" class="hero-alert" role="alert">{{ sessionError }}</p>

          <dl class="hero-pillars">
            <div>
              <dt>多模态索引</dt>
              <dd>字幕 · 语音 · OCR · 采样帧</dd>
            </div>
            <div>
              <dt>时间戳 Grounding</dt>
              <dd>候选片段点击直跳原片核实</dd>
            </div>
            <div>
              <dt>版本化时间线</dt>
              <dd>乐观锁防并发覆盖 · 一键回滚</dd>
            </div>
          </dl>
        </div>

        <aside class="hero-preview" aria-hidden="true">
          <div class="preview-chrome">
            <span class="chrome-dots"><i></i><i></i><i></i></span>
            <span class="chrome-title">interview_raw_04.mp4 · V3</span>
            <span class="chrome-pill">FFmpeg Ready</span>
          </div>
          <div class="preview-body">
            <div class="preview-prompt">
              <span>“找到第二次提到车钥匙的片段，前面留两秒并导出”</span>
            </div>
            <div class="preview-steps">
              <span>查找片段</span>
              <span>核实原片</span>
              <span>编排时间线</span>
              <span class="step-active">导出 MP4</span>
            </div>
            <div class="preview-evidence">
              <div class="ev-row">
                <span class="ev-tc">00:14.2 – 00:19.8</span>
                <span class="ev-txt">“把车钥匙放在玄关柜子上”</span>
                <span class="ev-tag">有直接依据</span>
              </div>
              <div class="ev-row ev-row-picked">
                <span class="ev-tc">01:02.0 – 01:08.5</span>
                <span class="ev-txt">“车钥匙刚才还在你口袋里”</span>
                <span class="ev-tag">已入轨</span>
              </div>
            </div>
            <div class="preview-timeline">
              <div class="pt-ruler">
                <span>00:00</span><span>00:03</span><span>00:06</span><span>00:08.5</span>
              </div>
              <div class="pt-clips">
                <div class="pt-clip pt-clip-a">V1 · 01:00.0 – 01:08.5</div>
                <div class="pt-clip pt-clip-b">保留原声 · 对齐字幕</div>
              </div>
            </div>
          </div>
        </aside>
      </section>
    </main>

    <dialog ref="auth" class="auth-dialog" aria-labelledby="auth-title" @cancel.prevent="!busy && auth.close()">
      <header class="dialog-header">
        <div>
          <h2 id="auth-title">{{ mode === 'login' ? '用户登录' : '新用户注册' }}</h2>
          <p class="dialog-sub">{{ mode === 'login' ? '登录本地剪筛工作区' : '账号仅保存在本机 SQLite 数据库' }}</p>
        </div>
        <button class="dialog-close" aria-label="关闭登录窗口" :disabled="busy" @click="auth.close()">×</button>
      </header>
      <form class="auth-form" @submit.prevent="submit">
        <label for="auth-username">用户名</label>
        <input id="auth-username" v-model="fields.username" autocomplete="username" placeholder="输入用户名" required autofocus />
        <label for="auth-password">密码</label>
        <input
          id="auth-password"
          v-model="fields.password"
          type="password"
          placeholder="输入密码"
          required
          :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
        />
        <template v-if="mode === 'register'">
          <label for="auth-nickname">昵称</label>
          <input id="auth-nickname" v-model="fields.nickname" autocomplete="nickname" placeholder="工作台显示名称（可选）" />
        </template>
        <div class="dialog-actions">
          <button type="submit" class="dialog-submit" :disabled="busy">{{ mode === 'login' ? '立即登录' : '提交注册' }}</button>
          <button
            type="button"
            class="dialog-switch"
            :disabled="busy"
            @click="mode = mode === 'login' ? 'register' : 'login'; message = ''; error = false"
          >
            {{ mode === 'login' ? '去注册' : '去登录' }}
          </button>
        </div>
        <p v-if="message" class="dialog-msg" :class="{ 'is-error': error }" :role="error ? 'alert' : 'status'">{{ message }}</p>
      </form>
    </dialog>
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

.nav-user,
.nav-actions {
  display: flex;
  align-items: center;
  gap: 0.65rem;
}

.user-pill {
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

.user-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--cs-success);
  box-shadow: 0 0 8px rgba(52, 211, 153, 0.7);
}

.nav-btn {
  padding: 0.44rem 0.88rem;
  border-radius: var(--cs-radius-sm);
  background: var(--cs-surface-elevated);
  border: 1px solid var(--cs-border-strong);
  color: var(--cs-text);
  font-size: 0.84rem;
  font-weight: 550;
  cursor: pointer;
  transition: border-color 140ms ease, background-color 140ms ease;
}

.nav-btn:hover {
  background: var(--cs-surface-hover);
  border-color: var(--cs-accent-border);
}

.nav-btn-primary {
  background: linear-gradient(180deg, #fbbf24 0%, #f59e0b 100%);
  border-color: #f59e0b;
  color: var(--cs-accent-ink);
  font-weight: 650;
}

.nav-btn-primary:hover {
  background: linear-gradient(180deg, #fcd34d 0%, #fbbf24 100%);
  border-color: #fbbf24;
  color: #140e02;
}

.cs-main {
  flex: 1;
}

/* Welcome Hero */
.welcome-stage {
  width: min(100%, 1200px);
  margin: 0 auto;
  padding: clamp(2.2rem, 6vw, 4.8rem) clamp(1rem, 3.5vw, 2.4rem) 3.5rem;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 0.95fr);
  gap: clamp(1.8rem, 4vw, 3.5rem);
  align-items: center;
}

.hero-eyebrow {
  margin: 0 0 0.9rem;
  font-family: var(--cs-font-mono);
  font-size: 0.74rem;
  letter-spacing: 0.1em;
  color: var(--cs-teal);
}

.welcome-copy h1 {
  margin: 0 0 1.05rem;
  font-size: clamp(2rem, 4.2vw, 3.1rem);
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: -0.03em;
  color: #fff;
}

.accent-word {
  color: var(--cs-accent-bright);
}

.hero-desc {
  margin: 0 0 1.6rem;
  max-width: 35rem;
  font-size: 0.98rem;
  line-height: 1.7;
  color: var(--cs-text-secondary);
}

.hero-cta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
}

.hero-primary-btn,
.hero-secondary-btn {
  padding: 0.68rem 1.35rem;
  border-radius: var(--cs-radius-sm);
  font-size: 0.92rem;
  font-weight: 620;
  cursor: pointer;
  transition: transform 120ms ease, border-color 140ms ease, background-color 140ms ease;
}

.hero-primary-btn {
  background: linear-gradient(180deg, #fbbf24 0%, #f59e0b 100%);
  border: 1px solid #f59e0b;
  color: var(--cs-accent-ink);
  box-shadow: 0 6px 22px rgba(245, 158, 11, 0.25);
}

.hero-primary-btn:hover {
  transform: translateY(-1px);
  background: linear-gradient(180deg, #fcd34d 0%, #fbbf24 100%);
}

.hero-secondary-btn {
  background: var(--cs-surface);
  border: 1px solid var(--cs-border-strong);
  color: var(--cs-text);
}

.hero-secondary-btn:hover {
  border-color: var(--cs-teal-border);
  background: var(--cs-surface-elevated);
}

.hero-alert {
  margin: 1rem 0 0;
  padding: 0.65rem 0.85rem;
  border-radius: var(--cs-radius-sm);
  background: var(--cs-danger-soft);
  border: 1px solid var(--cs-danger-border);
  color: #fecaca;
  font-size: 0.86rem;
}

.hero-pillars {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.85rem;
  margin: 2.1rem 0 0;
  padding-top: 1.4rem;
  border-top: 1px solid var(--cs-border);
}

.hero-pillars dt {
  font-size: 0.82rem;
  font-weight: 650;
  color: var(--cs-text);
}

.hero-pillars dd {
  margin: 0.25rem 0 0;
  font-size: 0.78rem;
  color: var(--cs-text-muted);
}

/* Studio Mock Preview Card */
.hero-preview {
  background: var(--cs-surface);
  border: 1px solid var(--cs-border-strong);
  border-radius: var(--cs-radius-lg);
  box-shadow: var(--cs-shadow-md);
  overflow: hidden;
}

.preview-chrome {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.7rem 1rem;
  background: var(--cs-bg-subtle);
  border-bottom: 1px solid var(--cs-border);
}

.chrome-dots {
  display: inline-flex;
  gap: 6px;
}

.chrome-dots i {
  display: block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #2c3748;
}

.chrome-title {
  font-family: var(--cs-font-mono);
  font-size: 0.76rem;
  color: var(--cs-text-secondary);
}

.chrome-pill {
  padding: 0.14rem 0.48rem;
  border-radius: 999px;
  font-family: var(--cs-font-mono);
  font-size: 0.7rem;
  color: var(--cs-success);
  background: rgba(52, 211, 153, 0.12);
  border: 1px solid rgba(52, 211, 153, 0.3);
}

.preview-body {
  padding: 1.15rem;
  display: grid;
  gap: 0.95rem;
}

.preview-prompt {
  margin-left: auto;
  padding: 0.6rem 0.85rem;
  border-radius: 12px 12px 3px 12px;
  background: var(--cs-accent-soft);
  border: 1px solid var(--cs-accent-border);
  color: #fef3c7;
  font-size: 0.84rem;
}

.preview-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  font-size: 0.74rem;
  color: var(--cs-text-muted);
}

.preview-steps span {
  padding: 0.2rem 0.55rem;
  border-radius: 999px;
  background: var(--cs-bg-subtle);
  border: 1px solid var(--cs-border);
}

.preview-steps .step-active {
  color: var(--cs-teal);
  border-color: var(--cs-teal-border);
  background: var(--cs-teal-soft);
}

.preview-evidence {
  display: grid;
  gap: 0.5rem;
}

.ev-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 0.6rem;
  padding: 0.58rem 0.72rem;
  border-radius: 8px;
  background: var(--cs-bg-subtle);
  border: 1px solid var(--cs-border);
  font-size: 0.8rem;
}

.ev-row-picked {
  border-color: var(--cs-teal-border);
  background: rgba(45, 212, 191, 0.06);
}

.ev-tc {
  font-family: var(--cs-font-mono);
  font-size: 0.74rem;
  color: var(--cs-teal);
}

.ev-txt {
  color: var(--cs-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ev-tag {
  font-size: 0.7rem;
  padding: 0.12rem 0.42rem;
  border-radius: 4px;
  background: var(--cs-surface-elevated);
  color: var(--cs-text-secondary);
}

.preview-timeline {
  padding: 0.7rem 0.8rem;
  border-radius: 8px;
  background: #07090d;
  border: 1px solid var(--cs-border);
}

.pt-ruler {
  display: flex;
  justify-content: space-between;
  font-family: var(--cs-font-mono);
  font-size: 0.68rem;
  color: var(--cs-text-muted);
  margin-bottom: 0.45rem;
}

.pt-clips {
  display: grid;
  gap: 0.35rem;
}

.pt-clip {
  padding: 0.36rem 0.65rem;
  border-radius: 5px;
  font-family: var(--cs-font-mono);
  font-size: 0.74rem;
}

.pt-clip-a {
  background: linear-gradient(90deg, rgba(245, 158, 11, 0.24), rgba(245, 158, 11, 0.1));
  border: 1px solid var(--cs-accent-border);
  color: #fef3c7;
}

.pt-clip-b {
  width: 72%;
  background: rgba(45, 212, 191, 0.12);
  border: 1px solid var(--cs-teal-border);
  color: #99f6e4;
}

/* Auth Dialog */
.auth-dialog {
  width: min(24rem, 90vw);
  padding: 1.35rem;
  color: var(--cs-text);
  background: var(--cs-surface);
  border: 1px solid var(--cs-border-strong);
  border-radius: var(--cs-radius-lg);
  box-shadow: var(--cs-shadow-md);
}

.auth-dialog::backdrop {
  background: rgba(4, 6, 10, 0.76);
  backdrop-filter: blur(4px);
}

.dialog-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1rem;
}

.dialog-header h2 {
  margin: 0;
  font-size: 1.12rem;
  font-weight: 650;
}

.dialog-sub {
  margin: 0.2rem 0 0;
  font-size: 0.8rem;
  color: var(--cs-text-muted);
}

.dialog-close {
  width: 2rem;
  height: 2rem;
  border-radius: 6px;
  background: var(--cs-bg-subtle);
  border: 1px solid var(--cs-border);
  color: var(--cs-text-secondary);
  font-size: 1.15rem;
  line-height: 1;
  cursor: pointer;
}

.dialog-close:hover {
  color: #fff;
  border-color: var(--cs-border-strong);
}

.auth-form {
  display: grid;
  gap: 0.55rem;
}

.auth-form label {
  font-size: 0.82rem;
  font-weight: 500;
  color: var(--cs-text-secondary);
  margin-top: 0.2rem;
}

.auth-form input {
  width: 100%;
  padding: 0.56rem 0.78rem;
  border-radius: var(--cs-radius-sm);
  background: var(--cs-bg-subtle);
  border: 1px solid var(--cs-border-strong);
  color: var(--cs-text);
}

.auth-form input:focus {
  border-color: var(--cs-accent);
}

.dialog-actions {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.55rem;
  margin-top: 0.65rem;
}

.dialog-submit {
  padding: 0.58rem 1rem;
  border-radius: var(--cs-radius-sm);
  background: linear-gradient(180deg, #fbbf24 0%, #f59e0b 100%);
  border: 1px solid #f59e0b;
  color: var(--cs-accent-ink);
  font-weight: 650;
  cursor: pointer;
}

.dialog-switch {
  padding: 0.58rem 0.9rem;
  border-radius: var(--cs-radius-sm);
  background: var(--cs-surface-elevated);
  border: 1px solid var(--cs-border-strong);
  color: var(--cs-text-secondary);
  cursor: pointer;
}

.dialog-switch:hover {
  color: #fff;
}

.dialog-msg {
  margin: 0.35rem 0 0;
  padding: 0.5rem 0.7rem;
  border-radius: var(--cs-radius-sm);
  font-size: 0.82rem;
  background: var(--cs-teal-soft);
  border: 1px solid var(--cs-teal-border);
  color: var(--cs-teal);
}

.dialog-msg.is-error {
  background: var(--cs-danger-soft);
  border-color: var(--cs-danger-border);
  color: #fecaca;
}

@media (max-width: 900px) {
  .welcome-stage {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 560px) {
  .hero-pillars {
    grid-template-columns: minmax(0, 1fr);
  }

  .brand-sub {
    display: none;
  }
}
</style>
