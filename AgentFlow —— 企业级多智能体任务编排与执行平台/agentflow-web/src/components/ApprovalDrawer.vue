<script setup lang="ts">
/**
 * 审批抽屉。
 *
 * 触发时机：
 *  1) 任务详情页收到 SSE `interrupt` 事件 → 自动滑出；
 *  2) 用户在大盘/收件箱点"审批" → 打开并拉取 `GET /tasks/{id}/interrupt`。
 *
 * 数据只来自 `GET /tasks/{id}/interrupt`（InterruptContext）：
 *  reason / preview（脱敏后的高危子任务）/ next_node / expires_at 等。
 * 审批动作走 `POST /tasks/{id}/resume`，由父组件执行，这里只负责收集决策与意见。
 */
import { computed, ref, watch } from 'vue'
import { APPROVAL_TYPE_TEXT, NODE_LABEL } from '@/utils/status'
import { formatFullTime } from '@/utils/format'
import JsonViewer from './JsonViewer.vue'
import type { InterruptContext } from '@/types/approval'

const props = withDefaults(
  defineProps<{
    visible: boolean
    taskId: number | null
    context: InterruptContext | null
    loading?: boolean
    /** 提交中（按钮 loading 与禁用） */
    submitting?: boolean
    /** 当前用户是否为任务创建人（后端默认禁止自审） */
    isCreator?: boolean
  }>(),
  { loading: false, submitting: false, isCreator: false },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
  submit: [payload: { decision: 'APPROVED' | 'REJECTED'; comment: string }]
}>()

const comment = ref('')
const decision = ref<'APPROVED' | 'REJECTED'>('APPROVED')

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      comment.value = ''
      decision.value = 'APPROVED'
    }
  },
)

const typeText = computed(() =>
  props.context?.type ? (APPROVAL_TYPE_TEXT[props.context.type] ?? props.context.type) : '—',
)

/** 高危子任务预览：后端放在 preview.subtasks（审批单 payload 脱敏后），键名开放 */
const riskySubtasks = computed<Array<Record<string, unknown>>>(() => {
  const preview = props.context?.preview ?? {}
  const subtasks = (preview as { subtasks?: unknown }).subtasks
  return Array.isArray(subtasks) ? (subtasks as Array<Record<string, unknown>>) : []
})

/**
 * 子任务工具名：后端审批单里以 `tool` 下发（`tool_hint` 为同值别名，老数据可能只有前者），
 * 两者都读，避免标签因键名差异整块消失。
 */
function toolOf(subtask: Record<string, unknown>): string {
  const value = subtask.tool ?? subtask.tool_hint
  return value ? String(value) : ''
}

/**
 * 是否高危：历史数据里 risk_level 曾按 `str(Enum)` 落成 "SubtaskRiskLevel.HIGH"，
 * 新数据为规范值 "high"；这里统一按「转小写后包含 high」判断，兼容两种形态。
 */
function isHighRisk(subtask: Record<string, unknown>): boolean {
  return String(subtask.risk_level ?? '').toLowerCase().includes('high')
}

const nextNodes = computed(() =>
  (props.context?.next_node ?? []).map((node) => NODE_LABEL[node] ?? node),
)

const canSubmit = computed(() => {
  if (!props.context?.resumable || props.submitting) return false
  // 驳回必须写原因：它会作为重规划的硬约束传给 Planner
  if (decision.value === 'REJECTED' && comment.value.trim().length === 0) return false
  return true
})

function close(): void {
  emit('update:visible', false)
}

function submit(value: 'APPROVED' | 'REJECTED'): void {
  decision.value = value
  if (value === 'REJECTED' && comment.value.trim().length === 0) return
  emit('submit', { decision: value, comment: comment.value.trim() })
}
</script>

<template>
  <a-drawer
    :visible="visible"
    :width="540"
    :mask-closable="!submitting"
    unmount-on-close
    @cancel="close"
  >
    <template #title>
      <div class="af-drawer-title">
        <span>审批决策</span>
        <a-tag v-if="context?.approval_id" size="small" color="orange">#{{ context.approval_id }}</a-tag>
        <a-tag size="small" color="red">{{ typeText }}</a-tag>
      </div>
    </template>

    <a-spin :loading="loading" class="af-drawer-spin">
      <div v-if="!context" class="af-drawer-empty">
        <a-empty description="未获取到挂起上下文" />
      </div>

      <template v-else>
        <a-alert v-if="!context.paused" type="info" class="af-drawer-alert">
          任务当前不在挂起状态（paused=false），无需审批。
        </a-alert>

        <a-alert v-if="isCreator" type="warning" class="af-drawer-alert">
          后端默认禁止审批本人创建的任务（APPROVAL_ALLOW_SELF_DECIDE=false），提交将被拒绝。
        </a-alert>

        <!-- 基础信息 -->
        <a-descriptions :column="2" size="small" class="af-drawer-desc">
          <a-descriptions-item label="任务 ID">{{ taskId }}</a-descriptions-item>
          <a-descriptions-item label="任务状态">{{ context.status ?? '—' }}</a-descriptions-item>
          <a-descriptions-item label="挂起时间">{{ formatFullTime(context.interrupted_at) }}</a-descriptions-item>
          <a-descriptions-item label="可恢复">{{ context.resumable ? '是' : '否' }}</a-descriptions-item>
          <a-descriptions-item label="trace_id" :span="2">
            <span class="af-mono">{{ context.trace_id ?? '—' }}</span>
          </a-descriptions-item>
        </a-descriptions>

        <!-- 阻断原因 -->
        <div class="af-drawer-section">
          <div class="af-drawer-label">阻断原因</div>
          <div class="af-drawer-reason">{{ context.reason || '—' }}</div>
        </div>

        <!-- 高危子任务 -->
        <div class="af-drawer-section">
          <div class="af-drawer-label">
            高危子任务
            <span class="af-muted">（共 {{ riskySubtasks.length }} 条，参数已脱敏）</span>
          </div>
          <div v-if="riskySubtasks.length === 0" class="af-muted">审批单未携带子任务预览</div>
          <div v-for="(task, index) in riskySubtasks" :key="index" class="af-drawer-subtask">
            <div class="af-drawer-subtask-head">
              <span class="af-mono">#{{ task.seq ?? index + 1 }}</span>
              <span class="af-drawer-subtask-name">{{ task.name ?? '未命名子任务' }}</span>
              <a-tag v-if="toolOf(task)" size="small" color="arcoblue">
                {{ toolOf(task).replace(/_/g, '-') }}
              </a-tag>
              <a-tag v-if="isHighRisk(task)" size="small" color="red">高危</a-tag>
            </div>
            <div v-if="task.description" class="af-muted af-drawer-subtask-desc">
              {{ task.description }}
            </div>
          </div>
        </div>

        <!-- 参数预览（payload 原始结构） -->
        <div class="af-drawer-section">
          <div class="af-drawer-label">审批单 payload（脱敏）</div>
          <JsonViewer :value="context.preview" :max-height="180" empty-text="无参数预览" />
        </div>

        <!-- 后续节点 -->
        <div class="af-drawer-section">
          <div class="af-drawer-label">批准后将执行</div>
          <div v-if="nextNodes.length" class="af-drawer-next">
            <a-tag v-for="node in nextNodes" :key="node" size="small" color="arcoblue">{{ node }}</a-tag>
          </div>
          <div v-else class="af-muted">后端未返回 next_node</div>
        </div>

        <!-- 审批意见 -->
        <div class="af-drawer-section">
          <div class="af-drawer-label">
            审批意见
            <span class="af-muted">（驳回时必填，将作为重规划约束）</span>
          </div>
          <a-textarea
            v-model="comment"
            placeholder="例如：SQL 扫描范围过大，请缩小到近 7 天；notify 先推送到测试群。"
            :max-length="500"
            show-word-limit
            :auto-size="{ minRows: 3, maxRows: 6 }"
            :disabled="submitting"
          />
        </div>
      </template>
    </a-spin>

    <template #footer>
      <div class="af-drawer-footer">
        <div class="af-muted af-drawer-hint">
          决策将调用 <span class="af-mono">POST /tasks/{{ taskId }}/resume</span>
        </div>
        <a-space>
          <a-button :disabled="submitting" @click="close">取消</a-button>
          <a-button
            status="danger"
            :loading="submitting && decision === 'REJECTED'"
            :disabled="!canSubmit || !context?.paused"
            @click="submit('REJECTED')"
          >
            驳回
          </a-button>
          <a-button
            type="primary"
            :loading="submitting && decision === 'APPROVED'"
            :disabled="!context?.resumable || submitting || !context?.paused"
            @click="submit('APPROVED')"
          >
            批准并继续
          </a-button>
        </a-space>
      </div>
    </template>
  </a-drawer>
</template>

<style scoped>
.af-drawer-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 500;
}

.af-drawer-spin {
  width: 100%;
}

.af-drawer-empty {
  padding: 40px 0;
}

.af-drawer-alert {
  margin-bottom: 12px;
}

.af-drawer-desc {
  margin-bottom: 8px;
}

.af-drawer-section {
  margin-top: 14px;
}

.af-drawer-label {
  font-size: 12px;
  color: var(--color-text-2);
  margin-bottom: 6px;
}

.af-drawer-reason {
  padding: 8px 10px;
  background: rgb(var(--warning-1));
  border: 1px solid rgb(var(--warning-2));
  border-radius: 2px;
  font-size: 12.5px;
  line-height: 20px;
  color: #b85c00;
}

.af-drawer-subtask {
  padding: 8px 10px;
  margin-bottom: 6px;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: #fff;
}

.af-drawer-subtask-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}

.af-drawer-subtask-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.af-drawer-subtask-desc {
  margin-top: 4px;
  font-size: 11.5px;
  line-height: 18px;
}

.af-drawer-next {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.af-drawer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.af-drawer-hint {
  font-size: 11px;
}
</style>
