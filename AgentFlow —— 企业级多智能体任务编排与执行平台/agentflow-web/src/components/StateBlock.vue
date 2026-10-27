<script setup lang="ts">
/**
 * 统一状态占位块：空 / 错误 / 加载 / **接口待接入**。
 *
 * `pending` 是本项目专用状态：当 UI 区块所需的后端接口尚未提供时（见规范 §0.3 的 G1~G12），
 * 页面必须显式告知"接口待接入"，**不允许用假数据填充**，避免联调期把占位误判为真实数据。
 */
withDefaults(
  defineProps<{
    state: 'empty' | 'error' | 'loading' | 'pending'
    title?: string
    description?: string
    /** 后端业务错误码，便于联调对号入座 */
    code?: string | null
    traceId?: string | null
    /** 待接入接口的建议契约（如 GET /tasks?page&size） */
    expectedApi?: string
    minHeight?: number
  }>(),
  { minHeight: 180 },
)

const emit = defineEmits<{ retry: [] }>()

const RESULT_STATUS = {
  empty: 'info',
  error: 'error',
  loading: 'info',
  pending: 'warning',
} as const
</script>

<template>
  <div class="af-state" :style="{ minHeight: `${minHeight}px` }">
    <a-spin v-if="state === 'loading'" :size="24" tip="加载中…" />

    <a-result
      v-else
      :status="RESULT_STATUS[state]"
      :title="title || (state === 'pending' ? '该区块接口待接入，仅预留 UI 占位' : undefined)"
    >
      <template #subtitle>
        <div class="af-state-sub">
          <div v-if="description">{{ description }}</div>
          <div v-if="code" class="af-mono af-muted">code: {{ code }}<span v-if="traceId"> · trace_id: {{ traceId }}</span></div>
          <div v-if="expectedApi" class="af-mono af-muted">建议契约：{{ expectedApi }}</div>
        </div>
      </template>
      <template v-if="state === 'error'" #extra>
        <a-button size="small" @click="emit('retry')">重试</a-button>
      </template>
    </a-result>
  </div>
</template>

<style scoped>
.af-state {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 12px 0;
}

.af-state-sub {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--color-text-3);
  max-width: 520px;
  margin: 0 auto;
}

.af-state :deep(.arco-result-title) {
  font-size: 14px;
  font-weight: 500;
}
</style>
