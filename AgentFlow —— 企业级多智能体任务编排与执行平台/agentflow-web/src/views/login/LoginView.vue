<script setup lang="ts">
/**
 * P01 登录页。
 *
 * 设计约束（重要）：**没有任何角色选择控件**。
 * 依据：后端 `LoginRequest` 只接受 username / password，角色存在 `users.role`，
 * 登录响应 `TokenPair` 里没有 role，必须再调 `GET /auth/me`。
 *
 * 错误分支（与后端 ErrorCode 一一对应）：
 *  - AUTH_INVALID_CREDENTIALS(401) → "用户名或密码错误"（**不能**提示"用户不存在"，
 *    后端已用 dummy_verify 抹平账号存在性的时序差异）
 *  - AUTH_USER_DISABLED(403)       → "账号已被禁用"
 *  - NETWORK_ERROR / 5xx           → "无法连接后端服务"，回显 API 地址
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Message } from '@arco-design/web-vue'
import { useAuthStore } from '@/stores/auth'
import { API_BASE_URL, API_PREFIX, ApiError, isApiError } from '@/api/http'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const appTitle = import.meta.env.VITE_APP_TITLE || 'AgentFlow'
const appVersion = import.meta.env.VITE_APP_VERSION || '1.0.0'

const form = reactive({
  username: '',
  password: '',
  remember: true,
})

const errors = reactive({ username: '', password: '' })
const alert = reactive<{ type: 'error' | 'warning' | null; title: string; detail: string; code: string }>({
  type: null,
  title: '',
  detail: '',
  code: '',
})

const submitting = ref(false)
const usernameRef = ref<{ focus: () => void } | null>(null)

const disabledSubmit = computed(() => submitting.value)

function validate(): boolean {
  errors.username = form.username.trim() ? '' : '请输入用户名'
  errors.password = form.password ? '' : '请输入密码'
  return !errors.username && !errors.password
}

function clearAlert(): void {
  alert.type = null
  alert.title = ''
  alert.detail = ''
  alert.code = ''
}

/** 把后端错误码翻译成用户能看懂的文案（绝不直接抛 detail 原文） */
function applyError(error: unknown): void {
  if (isApiError(error)) {
    switch (error.code) {
      case 'AUTH_INVALID_CREDENTIALS':
        alert.type = 'error'
        alert.title = '用户名或密码错误'
        alert.detail = '请确认后重试，连续失败请联系管理员重置密码。'
        break
      case 'AUTH_USER_DISABLED':
        alert.type = 'error'
        alert.title = '账号已被禁用'
        alert.detail = '请联系管理员恢复账号，或更换账号登录。'
        break
      case 'NETWORK_ERROR':
        alert.type = 'warning'
        alert.title = '无法连接后端服务'
        alert.detail = `当前 API：${API_BASE_URL}${API_PREFIX}`
        break
      default:
        alert.type = 'warning'
        alert.title = error.message || '登录失败'
        alert.detail = error.httpStatus && error.httpStatus >= 500 ? '服务端异常，请稍后重试或联系管理员。' : ''
    }
    alert.code = error.code
    return
  }
  alert.type = 'warning'
  alert.title = '无法连接后端服务'
  alert.detail = `当前 API：${API_BASE_URL}${API_PREFIX}`
  alert.code = 'UNKNOWN'
}

async function handleSubmit(): Promise<void> {
  clearAlert()
  if (!validate()) return
  submitting.value = true
  try {
    await auth.login(form.username.trim(), form.password, form.remember)
    Message.success(`欢迎回来，${auth.displayName}`)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
    await router.replace(redirect)
  } catch (error) {
    applyError(error)
    // 凭证错误：保留用户名、清空密码并聚焦（原型规范 P01 状态表）
    if (isApiError(error) && error.code === 'AUTH_INVALID_CREDENTIALS') {
      form.password = ''
    }
    if (error instanceof ApiError && error.code === 'AUTH_INVALID_CREDENTIALS') {
      const input = document.querySelector<HTMLInputElement>('input[type="password"]')
      input?.focus()
    }
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  usernameRef.value?.focus()
})
</script>

<template>
  <div class="login">
    <!-- 左：品牌区（深色，降低营销感，不加插画） -->
    <aside class="brand-side">
      <div class="grid-bg" />

      <div class="brand-inner">
        <div class="brand-top">
          <div class="brand-mark">AF</div>
          <div class="brand-name">{{ appTitle }}</div>
          <div class="brand-env">内部系统</div>
        </div>

        <div class="brand-mid">
          <h1 class="brand-title">
            把一句业务问题<br />
            编排成<span>可追溯的 Agent 任务</span>
          </h1>
          <p class="brand-desc">企业级多智能体任务编排与执行平台 · 电商经营分析场景</p>

          <div class="caps">
            <div class="cap">
              <div class="cap-k">01</div>
              <div>
                <b>自然语言发起编排</b>
                <p>目标 + 业务时间范围，Planner 自动生成 DAG 子任务计划</p>
              </div>
            </div>
            <div class="cap">
              <div class="cap-k">02</div>
              <div>
                <b>LangGraph 多智能体执行</b>
                <p>Executor 调用 MCP 工具：SQL / 图表 / 知识库 / 通知</p>
              </div>
            </div>
            <div class="cap">
              <div class="cap-k">03</div>
              <div>
                <b>人工审批断点与全链路 Trace</b>
                <p>高危工具触发 human_gate，审批后断点续跑，全程可回放</p>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="brand-foot">
        <span>v{{ appVersion }} · 仅限授权账号访问</span>
        <span>© 2026 电商数据中台</span>
      </div>
    </aside>

    <!-- 右：表单区 -->
    <main class="panel">
      <form class="form" @submit.prevent="handleSubmit">
        <h2>登录</h2>
        <p class="form-sub">使用企业账号登录，角色与数据范围由账号权限自动匹配</p>

        <a-alert
          v-if="alert.type"
          :type="alert.type"
          class="form-alert"
          :title="alert.title"
          closable
          @close="clearAlert"
        >
          <div class="form-alert-body">
            <div v-if="alert.detail">{{ alert.detail }}</div>
            <div v-if="alert.code" class="af-mono form-alert-code">code: {{ alert.code }}</div>
          </div>
        </a-alert>

        <label class="field-label" for="username">用户名</label>
        <a-input
          id="username"
          ref="usernameRef"
          v-model="form.username"
          size="large"
          placeholder="请输入用户名"
          autocomplete="username"
          :error="Boolean(errors.username)"
          :max-length="64"
          allow-clear
          @press-enter="handleSubmit"
        >
          <template #prefix><icon-user /></template>
        </a-input>
        <div v-if="errors.username" class="field-error">{{ errors.username }}</div>

        <label class="field-label" for="password">密码</label>
        <a-input-password
          id="password"
          v-model="form.password"
          size="large"
          placeholder="请输入密码"
          autocomplete="current-password"
          :error="Boolean(errors.password)"
          :max-length="128"
          @press-enter="handleSubmit"
        >
          <template #prefix><icon-lock /></template>
        </a-input-password>
        <div v-if="errors.password" class="field-error">{{ errors.password }}</div>

        <div class="form-row">
          <a-checkbox v-model="form.remember">在此设备保持登录 7 天</a-checkbox>
          <a-tooltip content="系统未开放自助重置，请通过管理员在「用户与权限」中重置密码">
            <span class="form-link">忘记密码？联系管理员</span>
          </a-tooltip>
        </div>

        <a-button
          type="primary"
          size="large"
          long
          html-type="submit"
          :loading="submitting"
          :disabled="disabledSubmit"
        >
          {{ submitting ? '登录中…' : '登 录' }}
        </a-button>

        <p class="form-note">
          账号由管理员在 <b>用户与权限</b> 中开通，角色（管理员 / 审批人 / 访客）随账号下发，
          登录后自动匹配菜单与数据范围 —— <b>登录页不提供角色选择</b>。
        </p>

        <div class="form-foot">
          <span class="af-mono">{{ API_PREFIX }}</span>
          <span>v{{ appVersion }}</span>
        </div>
      </form>
    </main>
  </div>
</template>

<style scoped>
.login {
  display: grid;
  grid-template-columns: 56% 44%;
  min-height: 100vh;
  background: #fff;
}

/* ---------- 左侧品牌区 ---------- */
.brand-side {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 52px 60px;
  background: #1d2129;
  color: #fff;
  overflow: hidden;
}

.grid-bg {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.045) 1px, transparent 1px);
  background-size: 48px 48px;
}

.brand-inner {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1;
}

.brand-top {
  display: flex;
  align-items: center;
  gap: 10px;
}

.brand-mark {
  width: 30px;
  height: 30px;
  border-radius: 7px;
  background: #165dff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 500;
}

.brand-name {
  font-size: 18px;
  font-weight: 500;
  letter-spacing: -0.3px;
}

.brand-env {
  margin-left: 8px;
  padding: 1px 6px;
  font-size: 11px;
  line-height: 15px;
  color: #a9aeb8;
  border: 1px solid #4e5969;
  border-radius: 2px;
}

.brand-mid {
  margin: auto 0;
}

.brand-title {
  margin: 0;
  font-size: 31px;
  line-height: 43px;
  font-weight: 500;
  letter-spacing: -0.6px;
}

.brand-title span {
  color: #94bfff;
}

.brand-desc {
  margin: 16px 0 0;
  max-width: 430px;
  font-size: 13px;
  line-height: 23px;
  color: #9ba3ae;
}

.caps {
  margin-top: 52px;
  border-top: 1px solid #2e333b;
}

.cap {
  display: flex;
  gap: 14px;
  padding: 16px 0;
  border-bottom: 1px solid #2e333b;
}

.cap:last-child {
  border-bottom: 0;
}

.cap-k {
  width: 16px;
  padding-top: 2px;
  flex: none;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11px;
  color: #94bfff;
}

.cap b {
  display: block;
  margin-bottom: 4px;
  font-size: 13px;
  font-weight: 400;
  color: #e5e6eb;
}

.cap p {
  margin: 0;
  font-size: 12px;
  line-height: 19px;
  color: #9ba3ae;
}

.brand-foot {
  position: relative;
  display: flex;
  justify-content: space-between;
  padding-top: 28px;
  font-size: 11px;
  color: #7a8494;
}

/* ---------- 右侧表单 ---------- */
.panel {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  background: #fff;
}

.form {
  width: 352px;
}

.form h2 {
  margin: 0;
  font-size: 23px;
  font-weight: 500;
  letter-spacing: -0.4px;
}

.form-sub {
  margin: 8px 0 28px;
  font-size: 12.5px;
  color: var(--color-text-3);
}

.form-alert {
  margin-bottom: 18px;
}

.form-alert-body {
  font-size: 12px;
  line-height: 19px;
}

.form-alert-code {
  margin-top: 2px;
  opacity: 0.75;
}

.field-label {
  display: block;
  margin-bottom: 7px;
  font-size: 12px;
  color: var(--color-text-2);
}

.field-error {
  margin: -12px 0 14px;
  font-size: 12px;
  color: rgb(var(--danger-6));
}

.form :deep(.arco-input-wrapper) {
  margin-bottom: 18px;
}

.form-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 2px 0 22px;
}

.form-link {
  font-size: 12px;
  color: var(--color-text-3);
  cursor: help;
}

.form-note {
  margin: 20px 0 0;
  padding-top: 16px;
  border-top: 1px solid var(--color-border-1, #f2f3f5);
  font-size: 11.5px;
  line-height: 19px;
  color: var(--color-text-3);
}

.form-note b {
  font-weight: 400;
  color: var(--color-text-2);
}

.form-foot {
  display: flex;
  justify-content: space-between;
  margin-top: 26px;
  font-size: 11px;
  color: var(--color-text-4, #c9cdd4);
}
</style>
