<script setup lang="ts">
/** 通用空状态组件：图标 + 主文案 + 可选操作按钮。 */
defineProps<{
  description?: string
  actionText?: string
}>()

const emit = defineEmits<{ action: [] }>()
</script>

<template>
  <div class="empty-state">
    <el-empty :description="description || '暂无数据'">
      <el-button v-if="actionText" type="primary" @click="emit('action')">{{ actionText }}</el-button>
      <slot />
    </el-empty>
  </div>
</template>

<style scoped>
.empty-state {
  padding: 24px 0;
}
</style>
