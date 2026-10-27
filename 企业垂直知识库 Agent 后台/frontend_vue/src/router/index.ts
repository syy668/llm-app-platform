// 路由配置 + 路由守卫权限拦截。
// 普通用户禁止访问 Trace 链路日志、RAGAS 评测页面（前端 UI 层控制，后端 require_admin 二次校验）。
import { createRouter, createWebHistory } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/store/useUserStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/Login.vue'), meta: { title: '登录' } },
    { path: '/', redirect: '/knowledge' },
    {
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('@/views/KnowledgeBase.vue'),
      meta: { title: '知识库管理', requiresAuth: true },
    },
    {
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/Chat.vue'),
      meta: { title: '智能对话', requiresAuth: true },
    },
    {
      path: '/reports',
      name: 'reports',
      component: () => import('@/views/Reports.vue'),
      meta: { title: '报表中心', requiresAuth: true },
    },
    {
      path: '/trace',
      name: 'trace',
      component: () => import('@/views/TraceLog.vue'),
      meta: { title: 'Trace 链路日志', requiresAuth: true, requiresAdmin: true },
    },
    {
      path: '/eval',
      name: 'eval',
      component: () => import('@/views/RagasEval.vue'),
      meta: { title: 'RAGAS 评测', requiresAuth: true, requiresAdmin: true },
    },
    {
      path: '/config',
      name: 'config',
      component: () => import('@/views/SystemConfig.vue'),
      meta: { title: '系统配置', requiresAuth: true },
    },
    {
      path: '/users',
      name: 'users',
      component: () => import('@/views/UserManagement.vue'),
      meta: { title: '用户管理', requiresAuth: true, requiresAdmin: true },
    },
    { path: '/:pathMatch(.*)*', redirect: '/knowledge' },
  ],
})

router.beforeEach((to) => {
  const store = useUserStore()

  if (to.meta.requiresAuth && !store.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 普通用户禁止访问管理员页面
  if (to.meta.requiresAdmin && !store.isAdmin) {
    ElMessage.error('权限不足：仅管理员可访问该页面')
    return { path: '/knowledge' }
  }

  if (to.path === '/login' && store.isLoggedIn) {
    return '/knowledge'
  }

  return true
})

export default router
