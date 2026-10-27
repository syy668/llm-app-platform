<script setup lang="ts">
/**
 * 侧边导航栏：品牌区 + 导航项 + 底部账号信息。
 * 布局与配色对齐项目一 ui-prototype.html（冷色调 B 端）。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/store/auth'

const route = useRoute()
const router = useRouter()
const { state, isAdmin } = useAuthStore()

interface NavItem {
  path: string
  title: string
  icon: string
}

const navItems: NavItem[] = [
  { path: '/dataset', title: '数据集管理', icon: 'Files' },
  { path: '/task', title: '评测任务', icon: 'DataAnalysis' },
  { path: '/ab-compare', title: 'A/B 实验看板', icon: 'Histogram' },
  { path: '/settings', title: '系统设置', icon: 'Setting' },
]

const activePath = computed(() => route.path)
const roleText = computed(() => (isAdmin.value ? '管理员' : '普通用户'))
const displayName = computed(() => state.user?.display_name || state.user?.username || '未登录')

function go(path: string) {
  if (route.path !== path) router.push(path)
}
</script>

<template>
  <aside class="ea-sidebar">
    <div class="brand" @click="go('/dataset')">
      <div class="brand-logo">EA</div>
      <div>
        <div class="brand-name">EvalAgent</div>
        <div class="brand-sub">RAG 评测与 A/B 调优</div>
      </div>
    </div>

    <nav class="nav">
      <div
        v-for="item in navItems"
        :key="item.path"
        class="nav-item"
        :class="{ active: activePath.startsWith(item.path) }"
        @click="go(item.path)"
      >
        <el-icon><component :is="item.icon" /></el-icon>
        <span>{{ item.title }}</span>
        <el-tag v-if="item.path === '/ab-compare'" size="small" type="warning" effect="light">核心</el-tag>
      </div>
    </nav>

    <div class="sidebar-footer">
      <div class="user-chip">
        <el-avatar :size="28" class="user-avatar">{{ displayName.slice(0, 1).toUpperCase() }}</el-avatar>
        <div class="user-meta">
          <div class="user-name">{{ displayName }}</div>
          <el-tag :type="isAdmin ? 'primary' : 'info'" size="small" effect="light">{{ roleText }}</el-tag>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.ea-sidebar {
  width: 232px;
  flex: none;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid var(--ea-border);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 20px;
  cursor: pointer;
  border-bottom: 1px solid var(--ea-border-light);
}

.brand-logo {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--ea-primary);
  color: #fff;
  font-weight: 700;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand-name {
  font-size: 16px;
  font-weight: 700;
  color: var(--ea-text-1);
  line-height: 1.2;
}

.brand-sub {
  font-size: 11px;
  color: var(--ea-text-3);
}

.nav {
  flex: 1;
  padding: 12px 10px;
  overflow-y: auto;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 40px;
  padding: 0 12px;
  margin-bottom: 4px;
  border-radius: 6px;
  color: var(--ea-text-2);
  cursor: pointer;
  transition: all 0.16s ease;
  font-size: 14px;
}

.nav-item:hover {
  background: var(--ea-primary-light);
  color: var(--ea-primary);
}

.nav-item.active {
  background: var(--ea-primary-light);
  color: var(--ea-primary);
  font-weight: 600;
}

.sidebar-footer {
  padding: 12px 16px;
  border-top: 1px solid var(--ea-border-light);
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 10px;
}

.user-avatar {
  background: var(--ea-primary);
  color: #fff;
  flex: none;
}

.user-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.user-name {
  font-size: 13px;
  color: var(--ea-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
