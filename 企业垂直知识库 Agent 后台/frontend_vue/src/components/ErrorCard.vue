<script setup lang="ts">
// 通用报错提示卡片
defineProps<{ message: string }>()
</script>

<template>
  <div class="error-card">⚠️ {{ message }}</div>
</template>
