<script setup lang="ts">
// 通用骨架屏加载占位
withDefaults(defineProps<{ rows?: number; width?: string }>(), { rows: 5, width: '100%' })
</script>

<template>
  <div class="skeleton-wrap">
    <div v-for="i in rows" :key="i" class="skeleton" :style="{ height: '12px', width: i === rows ? '60%' : width }" />
  </div>
</template>

<style scoped>
.skeleton-wrap {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 0;
}
</style>
