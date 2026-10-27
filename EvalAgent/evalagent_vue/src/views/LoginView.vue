<script setup lang="ts">
/**
 * 内嵌登录页（轻量）：
 * 需求约定登录复用项目一登录页，但两站点不同源无法共享 localStorage，
 * 因此提供本页直接调用项目一已有 POST /auth/login 接口签发 token（不新增后端）。
 * token 过期时 axios 仍会按约束跳转项目一登录页（VITE_LOGIN_URL）。
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { login } from '@/api/auth'
import { useAuthStore } from '@/store/auth'

const route = useRoute()
const router = useRouter()
const { setAuth } = useAuthStore()

const form = ref({ username: '', password: '' })
const loading = ref(false)

async function handleLogin() {
  if (!form.value.username || !form.value.password) {
    ElMessage.warning('请输入用户名与密码')
    return
  }
  loading.value = true
  try {
    const result = await login(form.value.username, form.value.password)
    // 【双Token改造】同时存储 access/refresh 双 token，供 401 无感刷新使用
    setAuth(result.access_token, result.user, result.refresh_token)
    ElMessage.success(`欢迎，${result.user.display_name || result.user.username}`)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    void router.push(redirect)
  } finally {
    loading.value = false
  }
}

function goExternalLogin() {
  window.open(import.meta.env.VITE_LOGIN_URL, '_blank')
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="brand">
        <div class="brand-logo">EA</div>
        <div>
          <div class="brand-name">EvalAgent</div>
          <div class="brand-sub">RAG 自动化评测与 A/B 调优平台</div>
        </div>
      </div>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="账号" autocomplete="username" @keyup.enter="handleLogin" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            show-password
            autocomplete="current-password"
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-button type="primary" class="login-btn" :loading="loading" @click="handleLogin">登录</el-button>
        <el-button class="login-btn" @click="goExternalLogin">打开知识库问答系统登录页</el-button>
      </el-form>

      <div class="login-tip">调用知识库问答系统 POST /auth/login 接口签发 token，本平台不实现用户注册</div>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #eef4ff 0%, #f7f8fa 55%);
}

.login-card {
  width: 400px;
  background: #fff;
  border: 1px solid var(--ea-border);
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(29, 33, 41, 0.08);
  padding: 32px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 24px;
}

.brand-logo {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  background: var(--ea-primary);
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand-name {
  font-size: 20px;
  font-weight: 700;
}

.brand-sub {
  font-size: 12px;
  color: var(--ea-text-3);
}

.login-btn {
  width: 100%;
  margin: 6px 0 0;
}

.login-tip {
  margin-top: 16px;
  font-size: 12px;
  color: var(--ea-text-3);
  text-align: center;
}
</style>
