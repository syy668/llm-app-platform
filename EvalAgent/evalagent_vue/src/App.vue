<script setup lang="ts">
/**
 * 根组件：侧边导航 + 内容区。
 * 登录页（/login）不显示侧边栏。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from '@/components/AppSidebar.vue'

const route = useRoute()
const showSidebar = computed(() => route.name !== 'login')
</script>

<template>
  <div class="ea-layout">
    <AppSidebar v-if="showSidebar" />
    <div class="ea-main">
      <router-view />
    </div>
  </div>
</template>
