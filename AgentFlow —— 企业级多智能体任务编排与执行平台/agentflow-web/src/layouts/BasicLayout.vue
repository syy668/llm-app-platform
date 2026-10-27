<script setup lang="ts">
/**
 * 主框架布局：左侧固定导航 + 顶部工具栏 + 内容区。
 *
 * 导航按角色裁剪（配置见 router/nav.ts）；徽标数字来自 `GET /approvals` 的 total。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Message, Modal } from '@arco-design/web-vue'
import { useAuthStore } from '@/stores/auth'
import { useApprovalStore } from '@/stores/approval'
import { NAV_GROUPS, findNavByPath } from '@/router/nav'
import { fetchReadyz } from '@/api/health'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const approvals = useApprovalStore()

const appTitle = import.meta.env.VITE_APP_TITLE || 'AgentFlow'
const appVersion = import.meta.env.VITE_APP_VERSION || '1.0.0'

/** 折叠侧边栏（窄屏时自动收起） */
const collapsed = ref(false)

/** 按角色过滤后的导航分组 */
const visibleGroups = computed(() =>
  NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => auth.hasAnyRole(item.roles)),
  })).filter((group) => group.items.length > 0),
)

const currentPath = computed(() => route.path)
const currentNav = computed(() => findNavByPath(route.path))
const pageTitle = computed(() => route.meta.title ?? currentNav.value?.label ?? '')

/** 详情页面包屑需要回到列表 */
const crumbParent = computed(() => (route.name === 'task-detail' ? { label: '工作台', path: '/dashboard' } : null))

function go(path: string): void {
  if (route.path !== path) router.push(path)
}

/** 全局搜索：任务编号 / trace_id 直接跳详情页参数（列表接口缺失，故按输入解析） */
const searchValue = ref('')
function submitSearch(): void {
  const keyword = searchValue.value.trim()
  if (!keyword) return
  const numeric = keyword.match(/(\d+)\s*$/)?.[1]
  if (numeric) {
    router.push(`/tasks/${Number(numeric)}`)
    return
  }
  Message.info('请粘贴任务编号（AF20261021-0007）末尾的任务 ID 或 trace_id 所属任务 ID')
}

async function handleLogout(): Promise<void> {
  Modal.confirm({
    title: '退出登录',
    content: '将注销当前会话并清空本地令牌，确定退出？',
    okText: '退出',
    cancelText: '取消',
    onOk: async () => {
      await auth.logout()
      approvals.clear()
      router.replace({ name: 'login' })
    },
  })
}

/** 演示模式角标：/readyz 报告 mode=demo（嵌入式 SQLite/fakeredis）时常驻提示，
 * 避免后端在演示库与真实库之间静默切换时被误认为"数据丢了" */
const demoMode = ref(false)

async function probeDemoMode(): Promise<void> {
  try {
    const { data } = await fetchReadyz()
    demoMode.value = data?.mode === 'demo'
  } catch {
    // 探针不可达（后端未起）时维持现状，不额外打扰
  }
}

onMounted(() => {
  if (auth.isAdmin || auth.isApprover) void approvals.refresh()
  void probeDemoMode()
})

// 进入收件箱后徽标可能变化，路由切换时轻量刷新一次
watch(
  () => route.fullPath,
  () => {
    if (auth.isAdmin || auth.isApprover) void approvals.refresh()
  },
)
</script>

<template>
  <a-layout class="af-shell">
    <a-layout-sider
      :collapsed="collapsed"
      :width="220"
      :collapsed-width="56"
      collapsible
      :hide-trigger="false"
      class="af-sider"
      @collapse="(val: boolean) => (collapsed = val)"
    >
      <div class="af-brand" :class="{ 'is-collapsed': collapsed }">
        <div class="af-brand-mark">AF</div>
        <div v-if="!collapsed" class="af-brand-text">
          <div class="af-brand-name">{{ appTitle }}</div>
          <div class="af-brand-ver">v{{ appVersion }}</div>
        </div>
      </div>

      <a-menu :selected-keys="[currentNav?.key ?? '']" class="af-menu" @menu-item-click="(key: string) => {
        const item = visibleGroups.flatMap((g) => g.items).find((i) => i.key === key)
        if (item) go(item.path)
      }">
        <template v-for="group in visibleGroups" :key="group.title">
          <a-menu-item-group :title="group.title">
            <a-menu-item v-for="item in group.items" :key="item.key">
              <template #icon>
                <component :is="item.icon" />
              </template>
              <span class="af-menu-label">{{ item.label }}</span>
              <a-badge
                v-if="item.badge === 'approvals' && approvals.total > 0"
                :count="approvals.total"
                :offset="[8, -2]"
              />
            </a-menu-item>
          </a-menu-item-group>
        </template>
      </a-menu>

      <div v-if="!collapsed" class="af-sider-foot">
        <div>电商经营分析 Agent 平台</div>
        <div class="af-muted">环境：{{ auth.roleText }} · {{ auth.displayName }}</div>
      </div>
    </a-layout-sider>

    <a-layout>
      <a-layout-header class="af-header">
        <a-breadcrumb class="af-crumb">
          <a-breadcrumb-item v-if="crumbParent">
            <a-link @click="go(crumbParent.path)">{{ crumbParent.label }}</a-link>
          </a-breadcrumb-item>
          <a-breadcrumb-item>{{ pageTitle }}</a-breadcrumb-item>
        </a-breadcrumb>

        <div class="af-header-spacer" />

        <a-input
          v-model="searchValue"
          class="af-search"
          placeholder="任务编号 / 任务 ID"
          allow-clear
          size="small"
          @press-enter="submitSearch"
        >
          <template #prefix>
            <icon-search />
          </template>
        </a-input>

        <a-badge :count="approvals.total" :offset="[-4, 4]">
          <a-button size="small" type="text" @click="go('/approvals')">
            <template #icon><icon-notification /></template>
          </a-button>
        </a-badge>

        <a-dropdown trigger="click">
          <div class="af-user">
            <a-avatar :size="26" :style="{ background: '#e8f0ff', color: '#165dff' }">
              {{ auth.displayName.slice(0, 1).toUpperCase() }}
            </a-avatar>
            <div class="af-user-meta">
              <div class="af-user-name">{{ auth.displayName }}</div>
              <div class="af-user-role">{{ auth.roleText }}</div>
            </div>
            <icon-down />
          </div>
          <template #content>
            <a-doption disabled>
              <div class="af-muted">{{ auth.roleScope }}</div>
            </a-doption>
            <a-divider :margin="4" />
            <a-doption @click="handleLogout">
              <template #icon><icon-export /></template>
              退出登录
            </a-doption>
          </template>
        </a-dropdown>
      </a-layout-header>

      <a-alert v-if="demoMode" type="warning" banner class="af-demo-banner">
        <template #icon><icon-exclamation-circle-fill /></template>
        当前处于演示模式（Demo Mode）：后端运行在嵌入式 SQLite + fakeredis 环境，数据为临时数据，与真实环境完全隔离。
      </a-alert>

      <a-layout-content class="af-content">
        <router-view v-slot="{ Component }">
          <component :is="Component" />
        </router-view>
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style scoped>
.af-shell {
  height: 100vh;
}

.af-sider {
  border-right: 1px solid var(--color-border-2, #e5e6eb);
  background: #fff;
}

.af-sider :deep(.arco-layout-sider-children) {
  display: flex;
  flex-direction: column;
}

.af-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 56px;
  padding: 0 16px;
  border-bottom: 1px solid var(--color-border-1, #f2f3f5);
  flex: none;
}

.af-brand.is-collapsed {
  justify-content: center;
  padding: 0;
}

.af-brand-mark {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  background: rgb(var(--primary-6));
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
}

.af-brand-name {
  font-size: 15px;
  font-weight: 500;
  letter-spacing: -0.2px;
}

.af-brand-ver {
  font-size: 11px;
  color: var(--color-text-3);
}

.af-menu {
  flex: 1;
  overflow-y: auto;
  padding-top: 4px;
}

.af-menu-label {
  font-size: 13px;
}

.af-sider-foot {
  flex: none;
  padding: 10px 16px 14px;
  border-top: 1px solid var(--color-border-1, #f2f3f5);
  font-size: 11px;
  line-height: 1.7;
  color: var(--color-text-2);
}

.af-header {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 56px;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid var(--color-border-2, #e5e6eb);
}

.af-header-spacer {
  flex: 1;
}

.af-crumb :deep(.arco-breadcrumb-item) {
  font-size: 13px;
}

.af-search {
  width: 200px;
}

.af-user {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 12px;
  border-left: 1px solid var(--color-border-1, #f2f3f5);
  cursor: pointer;
}

.af-user-meta {
  line-height: 1.3;
}

.af-user-name {
  font-size: 12px;
  color: var(--color-text-1);
}

.af-user-role {
  font-size: 11px;
  color: var(--color-text-3);
}

.af-demo-banner {
  flex: none;
}

.af-content {
  overflow-y: auto;
  background: var(--color-fill-2, #f7f8fa);
}
</style>
