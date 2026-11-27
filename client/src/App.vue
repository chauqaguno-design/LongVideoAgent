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

function openAuth() {
  sessionError.value = ''
  mode.value = 'login'
  fields.value = { username: '', password: '', nickname: '' }
  message.value = ''
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
      method: 'POST', headers: { 'Content-Type': 'application/json' },
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
  } finally { busy.value = false }
}
function logout() {
  apiRequest('/user/logout', { method: 'POST' }).catch(() => {})
  clearAuthToken()
}
</script>

<template>
  <div class="shell">
    <header class="navbar">
      <a href="/" aria-label="ReelCue 镜寻剪辑台首页"><strong>ReelCue</strong> 镜寻剪辑台</a>
      <div v-if="user">
        <span>{{ user.nickname }}</span>
        <button @click="logout">退出登录</button>
      </div>
      <button v-else @click="openAuth">登录 / 注册</button>
    </header>
    <main>
      <EditStudio v-if="user" :key="user.id" />
      <section v-else class="welcome">
        <h1>视频工作台</h1>
        <p>登录后上传视频，即可对话、回看原片和导出剪辑。</p>
        <button @click="openAuth">登录工作台</button>
        <p v-if="sessionError" role="alert">{{ sessionError }}</p>
      </section>
    </main>
    <dialog ref="auth" aria-labelledby="auth-title" @cancel.prevent="!busy && auth.close()">
      <header>
        <h2 id="auth-title">{{ mode === 'login' ? '用户登录' : '新用户注册' }}</h2>
        <button aria-label="关闭登录窗口" :disabled="busy" @click="auth.close()">×</button>
      </header>
      <form @submit.prevent="submit">
        <label for="auth-username">用户名</label>
        <input id="auth-username" v-model="fields.username" autocomplete="username" required autofocus>
        <label for="auth-password">密码</label>
        <input id="auth-password" v-model="fields.password" type="password" required
          :autocomplete="mode === 'login' ? 'current-password' : 'new-password'">
        <template v-if="mode === 'register'">
          <label for="auth-nickname">昵称</label>
          <input id="auth-nickname" v-model="fields.nickname" autocomplete="nickname">
        </template>
        <button type="submit" :disabled="busy">{{ mode === 'login' ? '立即登录' : '提交注册' }}</button>
        <button type="button" :disabled="busy" @click="mode = mode === 'login' ? 'register' : 'login'; message = ''">
          {{ mode === 'login' ? '去注册' : '去登录' }}
        </button>
        <p v-if="message" :role="error ? 'alert' : 'status'">{{ message }}</p>
      </form>
    </dialog>
  </div>
</template>

<style scoped>
.shell { color: #e4edf4; font-family: system-ui, sans-serif; }
.navbar, dialog header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.navbar { padding: .75rem clamp(1rem, 4vw, 3rem); border-bottom: 1px solid #26313e; }
.navbar a { color: inherit; text-decoration: none; }
.navbar strong { color: #54e5db; }
.navbar span { margin-right: .75rem; }
.welcome { padding: 2.5rem clamp(1rem, 4vw, 3rem); }
button, input { padding: .5rem .75rem; color: inherit; background: #181e28; border: 1px solid #354454; border-radius: .35rem; }
dialog { width: min(22rem, 85vw); color: inherit; background: #121923; border: 1px solid #354454; border-radius: .5rem; }
dialog::backdrop { background: #000a; }
form { display: grid; gap: .5rem; }
</style>
