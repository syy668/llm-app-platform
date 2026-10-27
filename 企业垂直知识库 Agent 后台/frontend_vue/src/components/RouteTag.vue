<script setup lang="ts">
// 路由彩色标签：rag / db_query / report / parallel_compare / direct
import { computed } from 'vue'
import type { AgentRoute } from '@/types'

const props = defineProps<{
  route?: AgentRoute | string | null
  /** v1.12：本次路由继承自上一轮（短追问意图继承） */
  inherited?: boolean
  /** v1.18：分类想走、但被右栏参数开关关掉的那一路（route 已回退 direct） */
  disabled?: string | null
}>()

const META: Record<string, { label: string; color: string }> = {
  rag: { label: '知识库检索', color: '#165DFF' },
  db_query: { label: '业务库查询', color: '#FF7D00' },
  report: { label: '报表生成', color: '#722ED1' },
  parallel_compare: { label: '并行对比', color: '#14C9C9' },
  direct: { label: '直接回答', color: '#86909C' },
}

const meta = computed(() => META[props.route || ''] || { label: props.route || '未知', color: '#86909C' })
// 被关掉的那一路用同一套名称，避免出现"已关闭：rag"这种黑话
const disabledLabel = computed(() => (props.disabled ? (META[props.disabled]?.label ?? props.disabled) : ''))
</script>

<template>
  <span class="route-tag" :style="{ background: meta.color }">
    🏷️ {{ meta.label }}
    <span
      v-if="inherited"
      class="inherit-badge"
      title="短指代型追问，沿用上一轮的检索意图（跳过 LLM 分类）"
    >继承</span>
    <span
      v-if="disabledLabel"
      class="disabled-badge"
      :title="`右栏参数配置里关闭了「${disabledLabel}」，本轮按直接回答处理（未检索/未查询）`"
    >已关闭{{ disabledLabel }}</span>
  </span>
</template>

<style scoped>
.inherit-badge {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.28);
  font-size: 10px;
  line-height: 14px;
  font-weight: 500;
  vertical-align: 1px;
}

/* 被开关拦下的路由：与「继承」同位置同尺寸，但用描边区分"这是说明不是状态" */
.disabled-badge {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.55);
  font-size: 10px;
  line-height: 13px;
  font-weight: 500;
  vertical-align: 1px;
}
</style>
