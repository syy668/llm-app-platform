<script setup lang="ts">
// 应用外壳：登录页直接渲染；登录后渲染侧边栏 + 顶栏 + 内容区。
// 普通用户侧边栏隐藏 Trace 链路日志、RAGAS 评测（前端 UI 层控制）。
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChatDotRound,
  DataAnalysis,
  Document,
  Key,
  PieChart,
  Setting,
  SwitchButton,
  TrendCharts,
  User,
  UserFilled,
} from '@element-plus/icons-vue'
import { useUserStore } from '@/store/useUserStore'
import { changePassword } from '@/api/auth'

const route = useRoute()
const router = useRouter()
const store = useUserStore()

const isLoginPage = computed(() => route.path === '/login')

interface NavItem {
  path: string
  title: string
  icon: unknown
}

const navItems = computed<NavItem[]>(() => {
  const items: NavItem[] = [
    { path: '/knowledge', title: '知识库管理', icon: Document },
    { path: '/chat', title: '智能对话', icon: ChatDotRound },
    { path: '/reports', title: '报表中心', icon: TrendCharts },
  ]
  if (store.isAdmin) {
    items.push({ path: '/users', title: '用户管理', icon: UserFilled })
    items.push({ path: '/trace', title: 'Trace 链路日志', icon: DataAnalysis })
    items.push({ path: '/eval', title: 'RAGAS 评测', icon: PieChart })
  }
  items.push({ path: '/config', title: '系统配置', icon: Setting })
  return items
})

const pageTitle = computed(() => (route.meta.title as string) || '')

// 修改密码对话框（对接 POST /auth/change-password）
const pwdDialogVisible = ref(false)
const pwdForm = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })
const pwdSaving = ref(false)

function openPwdDialog() {
  Object.assign(pwdForm, { oldPassword: '', newPassword: '', confirmPassword: '' })
  pwdDialogVisible.value = true
}

async function onSavePassword() {
  if (!pwdForm.oldPassword || !pwdForm.newPassword) return ElMessage.warning('请填写完整')
  if (pwdForm.newPassword.length < 8) return ElMessage.warning('新密码至少 8 位')
  if (pwdForm.newPassword !== pwdForm.confirmPassword) return ElMessage.warning('两次输入的新密码不一致')
  pwdSaving.value = true
  try {
    await changePassword(pwdForm.oldPassword, pwdForm.newPassword)
    ElMessage.success('密码已修改')
    pwdDialogVisible.value = false
  } catch {
    /* 错误已由拦截器提示（旧密码错误等） */
  } finally {
    pwdSaving.value = false
  }
}

async function onLogout() {
  try {
    await ElMessageBox.confirm(
      '确定要退出登录吗？退出后需要重新登录才能访问。',
      '退出登录',
      {
        type: 'warning',
        confirmButtonText: '退出',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return // 用户取消退出
  }
  await store.logout()
  ElMessage.success('已退出登录')
  router.push('/login')
}
</script>

<template>
  <router-view v-if="isLoginPage" />

  <el-container v-else class="app-shell">
    <el-aside width="232px" class="sidebar">
      <div class="brand">
        <div class="brand-icon">🤖</div>
        <div>
          <div class="brand-name">知识库 Agent</div>
          <div class="brand-sub">企业智能后台</div>
        </div>
      </div>

      <div class="nav-group">工作台</div>
      <el-menu :default-active="route.path" router class="nav-menu">
        <el-menu-item v-for="item in navItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-foot">
        <div class="avatar">{{ store.displayName.slice(0, 1) }}</div>
        <div class="user-info">
          <div class="user-name">{{ store.displayName }}</div>
          <div class="user-role" :class="{ admin: store.isAdmin }">{{ store.isAdmin ? '管理员' : '普通用户' }}</div>
        </div>
        <el-button text :icon="SwitchButton" title="退出登录" @click="onLogout" />
      </div>
    </el-aside>

    <el-container>
      <el-header class="topbar" height="56px">
        <div class="topbar-title">
          <span class="crumb">工作台</span><span class="crumb">/</span><span>{{ pageTitle }}</span>
        </div>
        <div class="topbar-user">
          <el-icon><User /></el-icon>
          <span>{{ store.displayName }}</span>
          <span class="ui-tag" :class="store.isAdmin ? 'tag-blue' : 'tag-gray'">
            {{ store.isAdmin ? '管理员' : '普通用户' }}
          </span>
          <el-button text :icon="Key" title="修改密码" class="pwd-btn" @click="openPwdDialog" />
        </div>
      </el-header>

      <el-main class="content">
        <router-view />
      </el-main>
    </el-container>

    <!-- 修改密码对话框 -->
    <el-dialog v-model="pwdDialogVisible" title="修改密码" width="400px">
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="当前密码">
          <el-input v-model="pwdForm.oldPassword" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="pwdForm.newPassword" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="pwdForm.confirmPassword" type="password" show-password @keyup.enter="onSavePassword" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="pwdSaving" @click="onSavePassword">保存</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
.app-shell { height: 100vh; }

.sidebar {
  background: var(--card);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 20px 18px 16px; border-bottom: 1px solid var(--border-light); }
.brand-icon {
  width: 34px; height: 34px; border-radius: 9px; background: var(--primary); color: #fff;
  display: flex; align-items: center; justify-content: center; font-size: 18px; flex: none;
}
.brand-name { font-size: 15px; font-weight: 700; color: var(--text-1); line-height: 1.3; }
.brand-sub { font-size: 11px; color: var(--text-3); }

.nav-group { font-size: 11px; color: var(--text-3); padding: 14px 20px 6px; letter-spacing: 1px; }
.nav-menu { border-right: none; background: transparent; flex: 1; overflow-y: auto; }
.nav-menu :deep(.el-menu-item) { height: 40px; margin: 2px 10px; border-radius: var(--radius-sm); }
.nav-menu :deep(.el-menu-item.is-active) { background: var(--primary-light); color: var(--primary); }

.sidebar-foot { border-top: 1px solid var(--border-light); padding: 12px 14px; display: flex; align-items: center; gap: 10px; }
.avatar {
  width: 32px; height: 32px; border-radius: 50%; background: var(--primary-weak); color: var(--primary);
  display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600; flex: none;
}
.user-info { flex: 1; min-width: 0; }
.user-name { font-size: 13px; font-weight: 500; color: var(--text-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.user-role { font-size: 11px; color: var(--text-3); }
.user-role.admin { color: var(--primary); }

.topbar { background: var(--card); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
.topbar-title { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 600; }
.topbar-title .crumb { color: var(--text-3); font-weight: 400; font-size: 13px; }
.topbar-user { display: flex; align-items: center; gap: 8px; padding: 5px 10px; border: 1px solid var(--border); border-radius: 8px; }
.pwd-btn { padding: 4px; color: var(--text-3); }
.pwd-btn:hover { color: var(--primary); }

.content { background: var(--bg); padding: 20px 24px 40px; }
</style>
