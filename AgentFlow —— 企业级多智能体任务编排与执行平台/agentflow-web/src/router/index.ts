/**
 * 路由表与全局守卫。
 *
 * 守卫顺序：登录态 → 角色（meta.roles）→ 页面标题。
 * 未登录访问受保护页 → /login?redirect=<原路径>，登录成功后回跳。
 */
import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import { Message } from '@arco-design/web-vue'
import { useAuthStore } from '@/stores/auth'
import { ROLE_META } from '@/utils/status'
import type { UserRole } from '@/types/enums'

declare module 'vue-router' {
  interface RouteMeta {
    /** 页面标题（浏览器标签 + 面包屑） */
    title?: string
    /** 允许访问的角色；缺省表示登录即可 */
    roles?: UserRole[]
    /** 无需登录 */
    public?: boolean
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/login/LoginView.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/',
    component: () => import('@/layouts/BasicLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/dashboard/DashboardView.vue'),
        meta: { title: '工作台' },
      },
      {
        path: 'tasks/new',
        name: 'task-create',
        component: () => import('@/views/task/TaskCreateView.vue'),
        meta: { title: '新建任务', roles: ['admin'] },
      },
      {
        path: 'tasks/:taskId',
        name: 'task-detail',
        component: () => import('@/views/task/TaskDetailView.vue'),
        meta: { title: '任务详情' },
        props: true,
      },
      {
        path: 'approvals',
        name: 'approvals',
        component: () => import('@/views/approval/ApprovalInboxView.vue'),
        meta: { title: '审批收件箱', roles: ['admin', 'approver'] },
      },
      {
        path: 'eval',
        name: 'eval',
        component: () => import('@/views/eval/EvalView.vue'),
        // PRD 旅程三：访客可查看评测看板（汇总指标）；A/R 完整视图（页内再按角色裁剪）
        meta: { title: '评测与实验' },
      },
      {
        path: 'memories',
        name: 'memory',
        component: () => import('@/views/memory/MemoryView.vue'),
        meta: { title: '记忆管理', roles: ['admin'] },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/system/SettingsView.vue'),
        meta: { title: '系统设置', roles: ['admin'] },
      },
      {
        // v1.1：MCP 工具注册表（元信息 + 启停）。读取放开给 approver，写操作仅 admin。
        path: 'tools',
        name: 'tools',
        component: () => import('@/views/tool/ToolRegistryView.vue'),
        meta: { title: '工具注册表', roles: ['admin', 'approver'] },
      },
    ],
  },
  {
    // 兜底：未匹配的路径回工作台
    path: '/:pathMatch(.*)*',
    redirect: '/dashboard',
  },
]

export const router = createRouter({
  // 用 hash 模式：后端只托管静态文件，无需额外配置 history fallback
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const appTitle = import.meta.env.VITE_APP_TITLE || 'AgentFlow'
  document.title = to.meta.title ? `${to.meta.title} · ${appTitle}` : appTitle

  // 1) 公开页：已登录访问 /login 直接回工作台
  if (to.meta.public) {
    if (to.name === 'login' && auth.isAuthenticated) {
      const ok = await auth.ensureProfile()
      if (ok) return { path: '/dashboard' }
    }
    return true
  }

  // 2) 登录态
  const loggedIn = await auth.ensureProfile()
  if (!loggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 3) 角色
  if (to.meta.roles && !auth.hasAnyRole(to.meta.roles)) {
    const allowed = to.meta.roles.map((r) => ROLE_META[r].text).join(' / ')
    Message.warning(`当前角色无权访问该页面（需要：${allowed}）`)
    return { path: '/dashboard' }
  }

  return true
})
