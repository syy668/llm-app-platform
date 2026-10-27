<script setup lang="ts">
/**
 * 状态标签 —— 所有页面统一用它渲染状态，避免各处各写一套颜色。
 *
 * 颜色映射见 utils/status.ts：任务 7 态 / 子任务 7 态 / 风险 2 态。
 */
import { computed } from 'vue'
import { SUBTASK_STATUS_META, TASK_STATUS_META, RISK_META, statusMeta } from '@/utils/status'
import type { SubtaskRiskLevel, SubtaskStatus, TaskStatus } from '@/types/enums'

const props = withDefaults(
  defineProps<{
    /** 状态值；kind 决定用哪张映射表 */
    status?: string | null
    kind?: 'task' | 'subtask' | 'risk'
    size?: 'small' | 'medium'
  }>(),
  { kind: 'task', size: 'small' },
)

const meta = computed(() => {
  if (props.kind === 'subtask') {
    return statusMeta(SUBTASK_STATUS_META as unknown as Record<string, never>, props.status as SubtaskStatus)
  }
  if (props.kind === 'risk') return statusMeta(RISK_META as unknown as Record<string, never>, props.status as SubtaskRiskLevel)
  return statusMeta(TASK_STATUS_META as unknown as Record<string, never>, props.status as TaskStatus)
})
</script>

<template>
  <a-tag :color="meta.color" :size="size" class="af-status-tag">
    <span v-if="meta.pulse" class="af-pulse" />
    {{ meta.text }}
  </a-tag>
</template>

<style scoped>
.af-status-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}
</style>
