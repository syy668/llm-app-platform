<script setup lang="ts">
// 通用空状态组件
defineProps<{
  title: string
  desc?: string
  icon?: string
}>()
</script>

<template>
  <div class="empty-state">
    <div class="empty-icon">{{ icon || '💬' }}</div>
    <div class="empty-title">{{ title }}</div>
    <div v-if="desc" class="empty-desc">{{ desc }}</div>
  </div>
</template>
