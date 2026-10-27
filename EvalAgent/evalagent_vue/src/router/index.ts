/**
 * 路由配置 + 守卫：
 * - 全部业务页需登录（token），无 token 跳本项目内嵌登录页（复用上游 /auth/login 接口）
 * - 后端已放开 eval GET 查询为登录可读：普通用户可浏览数据集/任务/A-B 看板；
 *   写操作（创建任务、删除）按 isAdmin 显隐按钮，后端 require_admin 二次校验
 * - 【RBAC 新增】meta.requiresAdmin：仅限管理员页面，普通用户访问被守卫拦截并提示
 *   （前端拦截仅为体验优化，真正的权限校验以后端 get_current_admin 403 为准）
 * - 【双Token改造】token 过期由 axios 拦截器无感刷新，路由守卫只做「有无凭据」判断
 */
import { createRouter, createWebHistory } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/store/auth'

/** RouteMeta 类型扩展：requiresAuth 是否需要登录 / requiresAdmin 是否仅限管理员 */
declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    requiresAuth?: boolean
    requiresAdmin?: boolean
  }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录' },
    },
    { path: '/', redirect: '/dataset' },
    {
      path: '/dataset',
      name: 'dataset',
      component: () => import('@/views/DatasetView.vue'),
      meta: { title: '数据集管理', requiresAuth: true },
    },
    {
      path: '/task',
      name: 'task',
      component: () => import('@/views/TaskView.vue'),
      meta: { title: '评测任务', requiresAuth: true },
    },
    {
      path: '/ab-compare',
      name: 'ab-compare',
      component: () => import('@/views/AbCompareView.vue'),
      meta: { title: 'A/B 实验看板', requiresAuth: true },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
      meta: { title: '系统设置', requiresAuth: true, requiresAdmin: true },
    },
    { path: '/:pathMatch(.*)*', redirect: '/dataset' },
  ],
})

router.beforeEach((to) => {
  const { isLoggedIn, isAdmin } = useAuthStore()

  // 1) 未登录访问受保护页面 → 跳登录（记录回跳地址）
  if (to.meta.requiresAuth && !isLoggedIn.value) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  // 2) 普通用户访问 requiresAdmin 管理员页面 → 拦截并提示（后端接口另有 403 兜底）
  if (to.meta.requiresAdmin && isLoggedIn.value && !isAdmin.value) {
    ElMessage.warning('权限不足：该页面仅管理员可访问')
    return { path: '/dataset' }
  }
  // 3) 登录用户访问登录页 → 自动跳回首页
  if (to.name === 'login' && isLoggedIn.value) {
    return { path: '/' }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · EvalAgent` : 'EvalAgent'
})

export default router
