<script setup lang="ts">
// 通用分页组件（page / size 规范）
defineProps<{ total: number; page: number; size: number }>()
const emit = defineEmits<{
  'update:page': [value: number]
  'update:size': [value: number]
}>()
</script>

<template>
  <div class="page-pagination">
    <el-pagination
      background
      :current-page="page"
      :page-size="size"
      :total="total"
      :page-sizes="[10, 20, 50]"
      layout="total, sizes, prev, pager, next"
      @current-change="(p: number) => emit('update:page', p)"
      @size-change="(s: number) => emit('update:size', s)"
    />
  </div>
</template>
