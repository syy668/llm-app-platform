<script setup lang="ts">
// 登录页
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login as loginApi } from '@/api/auth'
import { useUserStore } from '@/store/useUserStore'

const router = useRouter()
const route = useRoute()
const store = useUserStore()

const form = reactive({ username: '', password: '' })
const loading = ref(false)

async function onSubmit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const data = await loginApi(form.username, form.password)
    store.setAuth(data)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/knowledge'
    router.push(redirect)
  } catch {
    // 错误提示已由 request 拦截器统一处理
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-logo">🤖</div>
      <div class="login-title">企业垂直知识库 Agent</div>
      <div class="login-desc">智能后台 · 知识库 · 智能问答 · 链路追踪</div>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            show-password
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" style="width: 100%" :loading="loading" @click="onSubmit">登 录</el-button>
      </el-form>
    </div>
    <div class="login-footer">企业垂直知识库 Agent 后台 · Vue v1.1</div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  background: radial-gradient(1200px 600px at 50% -10%, #eaf1ff 0%, transparent 60%), var(--bg);
}
.login-card {
  width: 400px;
  max-width: 100%;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  box-shadow: 0 12px 32px rgba(29, 33, 41, 0.1);
  padding: 40px 40px 32px;
}
.login-logo {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  background: linear-gradient(135deg, #165dff, #4080ff);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  margin-bottom: 20px;
  box-shadow: 0 8px 20px rgba(22, 93, 255, 0.28);
}
.login-title { font-size: 22px; font-weight: 700; color: var(--text-1); }
.login-desc { font-size: 13px; color: var(--text-3); margin: 6px 0 24px; }
.login-footer { margin-top: 24px; font-size: 12px; color: #c9cdd4; }
</style>
