<script setup lang="ts">
/**
 * Trace 轨迹时间线（只读回放）。
 *
 * 数据源：`GET /tasks/{id}/trace` → `{ steps, checkpoints, interrupt, total_steps }`
 *  - steps 来自 `task_executions`，按 id 升序即等价于时间顺序；
 *  - G7 已闭环：每步出参 `span_id` / `parent_span_id` / `subtask_id`，展开详情可看到
 *    子任务归属与调用层级（旧数据这三列可能为空，UI 相应隐藏）；
 *  - 回放倍速/拖拽只是前端对 steps 数组的游标推进，不重放后端执行，
 *    所以不会产生任何副作用请求。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import dayjs from 'dayjs'
import JsonViewer from './JsonViewer.vue'
import type { CheckpointStep, TraceStep } from '@/types/trace'
import { EVENT_LABEL, NODE_LABEL } from '@/utils/status'
import { formatClock, formatLatency, parseTime } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    steps: TraceStep[]
    checkpoints?: CheckpointStep[]
    loading?: boolean
  }>(),
  { checkpoints: () => [], loading: false },
)

/** 节点筛选（空串 = 全部） */
const nodeFilter = ref('')
/** 仅看异常 */
const onlyErrors = ref(false)
/** 当前展开的步骤 id */
const expanded = ref<number[]>([])

/** 回放控制 */
const playing = ref(false)
const speed = ref(1)
const playhead = ref(0)
let timer: number | null = null

const nodeOptions = computed(() => {
  const nodes = Array.from(new Set(props.steps.map((step) => step.node))).filter(Boolean)
  return nodes.map((node) => ({ label: NODE_LABEL[node] ?? node, value: node }))
})

/** 是否异常步骤：有 tool_error 或事件类型为 error */
function isErrorStep(step: TraceStep): boolean {
  return Boolean(step.tool_error) || step.event === 'error'
}

const filteredSteps = computed(() =>
  props.steps.filter((step) => {
    if (nodeFilter.value && step.node !== nodeFilter.value) return false
    if (onlyErrors.value && !isErrorStep(step)) return false
    return true
  }),
)

const errorCount = computed(() => props.steps.filter(isErrorStep).length)

/** 相对首条步骤的耗时偏移（ms），用于时间线左侧刻度 */
const firstTime = computed(() => parseTime(props.steps[0]?.started_at ?? null))

function offsetOf(step: TraceStep): string {
  const start = firstTime.value
  const current = parseTime(step.started_at)
  if (!start || !current) return '—'
  const ms = current.diff(start)
  return `+${(ms / 1000).toFixed(2)}s`
}

/** 回放游标：过滤视图下标（与 playhead 对齐到过滤后的列表） */
const currentStep = computed(() => filteredSteps.value[Math.min(playhead.value, filteredSteps.value.length - 1)] ?? null)

function stepClass(step: TraceStep): string {
  const classes: string[] = []
  if (isErrorStep(step)) classes.push('is-error')
  if (currentStep.value && step.id === currentStep.value.id && playing.value) classes.push('is-current')
  if (step.event === 'tool.call' || step.event === 'tool.result') classes.push('is-tool')
  return classes.join(' ')
}

function eventText(step: TraceStep): string {
  const base = EVENT_LABEL[step.event] ?? step.event
  if (step.event === 'tool.call' || step.event === 'tool.result') {
    return `${base} · ${step.tool_name ?? 'unknown'}`
  }
  return base
}

function toggleExpand(stepId: number): void {
  const index = expanded.value.indexOf(stepId)
  if (index >= 0) expanded.value.splice(index, 1)
  else expanded.value.push(stepId)
}

function togglePlay(): void {
  if (filteredSteps.value.length === 0) return
  playing.value = !playing.value
  if (playing.value) {
    // 播放到末尾时从头开始
    if (playhead.value >= filteredSteps.value.length - 1) playhead.value = 0
    startTimer()
  } else {
    stopTimer()
  }
}

function startTimer(): void {
  stopTimer()
  timer = window.setInterval(() => {
    if (playhead.value >= filteredSteps.value.length - 1) {
      playing.value = false
      stopTimer()
      return
    }
    playhead.value += 1
  }, Math.max(120, 900 / speed.value))
}

function stopTimer(): void {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

/** 拖拽进度：直接定位到某一步（不自动播放） */
function onSeek(value: number | [number, number]): void {
  const index = Array.isArray(value) ? value[0] : value
  playing.value = false
  stopTimer()
  playhead.value = Math.min(Math.max(index, 0), Math.max(filteredSteps.value.length - 1, 0))
}

watch(speed, () => {
  if (playing.value) startTimer()
})

// 数据刷新（SSE 增量）时保持游标不越界
watch(
  () => props.steps.length,
  (length) => {
    if (playhead.value > length - 1) playhead.value = Math.max(length - 1, 0)
  },
)

onBeforeUnmount(stopTimer)

/** 检查点摘要（顶部的 checkpoint 轨道） */
const checkpointItems = computed(() => props.checkpoints.slice(-8))
</script>

<template>
  <div class="af-trace">
    <!-- 回放控制条 -->
    <div class="af-trace-bar">
      <a-button size="mini" type="primary" shape="circle" @click="togglePlay">
        <template #icon>
          <icon-pause v-if="playing" />
          <icon-play-arrow v-else />
        </template>
      </a-button>
      <a-slider
        class="af-trace-slider"
        :model-value="playhead"
        :min="0"
        :max="Math.max(filteredSteps.length - 1, 0)"
        :show-tooltip="false"
        @change="onSeek"
      />
      <a-radio-group v-model="speed" type="button" size="mini">
        <a-radio :value="0.5">0.5x</a-radio>
        <a-radio :value="1">1x</a-radio>
        <a-radio :value="2">2x</a-radio>
        <a-radio :value="4">4x</a-radio>
      </a-radio-group>
      <span class="af-muted af-num">
        {{ Math.min(playhead + 1, filteredSteps.length) }} / {{ filteredSteps.length }}
      </span>
      <div class="af-trace-bar-spacer" />
      <a-select
        v-model="nodeFilter"
        size="mini"
        class="af-trace-node"
        placeholder="全部节点"
        allow-clear
        :options="nodeOptions"
      />
      <a-checkbox v-model="onlyErrors">
        仅看异常<span v-if="errorCount" class="af-trace-err-count">{{ errorCount }}</span>
      </a-checkbox>
    </div>

    <!-- checkpoint 轨道 -->
    <div v-if="checkpointItems.length" class="af-trace-checkpoints">
      <span class="af-muted">Checkpoint</span>
      <div class="af-trace-cp-list af-scroll">
        <div v-for="(cp, index) in checkpointItems" :key="index" class="af-trace-cp">
          <div class="af-trace-cp-step">#{{ cp.step ?? index + 1 }}</div>
          <div class="af-trace-cp-node">{{ NODE_LABEL[cp.source ?? ''] ?? cp.source ?? '—' }}</div>
          <div class="af-trace-cp-meta af-muted">
            {{ cp.done_count }}/{{ cp.plan_size }} · {{ cp.status ?? '—' }}
          </div>
        </div>
      </div>
    </div>

    <!-- 时间线 -->
    <a-spin :loading="loading" class="af-trace-spin">
      <div v-if="filteredSteps.length === 0" class="af-trace-empty">
        <a-empty :description="steps.length === 0 ? '暂无轨迹记录' : '当前筛选条件下没有步骤'" />
      </div>

      <div v-else class="af-trace-list af-scroll">
        <div
          v-for="step in filteredSteps"
          :key="step.id"
          class="af-trace-item"
          :class="stepClass(step)"
        >
          <div class="af-trace-time af-mono">
            <div>{{ formatClock(step.started_at) }}</div>
            <div class="af-trace-offset">{{ offsetOf(step) }}</div>
          </div>

          <div class="af-trace-axis">
            <span class="af-trace-dot" />
          </div>

          <div class="af-trace-main">
            <div class="af-trace-line" @click="toggleExpand(step.id)">
              <a-tag size="small" color="gray" class="af-trace-node-tag">
                {{ NODE_LABEL[step.node] ?? step.node }}
              </a-tag>
              <span class="af-trace-event" :class="{ 'is-err': isErrorStep(step) }">
                {{ eventText(step) }}
              </span>
              <span class="af-mono af-muted">#{{ step.id }}</span>

              <span v-if="step.tool_error" class="af-trace-error-code">{{ step.tool_error }}</span>

              <div class="af-trace-line-spacer" />
              <span v-if="step.latency_ms" class="af-muted af-num">{{ formatLatency(step.latency_ms) }}</span>
              <span v-if="step.input_tokens || step.output_tokens" class="af-muted af-num">
                {{ step.input_tokens }}↑ {{ step.output_tokens }}↓
              </span>
              <a-link class="af-trace-toggle">{{ expanded.includes(step.id) ? '收起' : '详情' }}</a-link>
            </div>

            <div v-if="expanded.includes(step.id)" class="af-trace-detail">
              <div class="af-trace-detail-grid">
                <div>
                  <div class="af-trace-detail-label">入参 / 节点输出（payload）</div>
                  <JsonViewer :value="step.payload" :max-height="240" empty-text="该节点无 payload" />
                </div>
                <div class="af-trace-detail-meta">
                  <div class="af-trace-detail-label">本步骤</div>
                  <div class="af-trace-kv"><span>trace_id</span><b class="af-mono">{{ step.trace_id }}</b></div>
                  <div v-if="step.span_id" class="af-trace-kv"><span>span_id</span><b class="af-mono">{{ step.span_id }}</b></div>
                  <div v-if="step.parent_span_id" class="af-trace-kv"><span>父 span</span><b class="af-mono">{{ step.parent_span_id }}</b></div>
                  <div v-if="step.subtask_id" class="af-trace-kv"><span>子任务</span><b class="af-mono">#{{ step.subtask_id }}</b></div>
                  <div class="af-trace-kv"><span>开始时间</span><b>{{ step.started_at ? dayjs(step.started_at).format('YYYY-MM-DD HH:mm:ss.SSS') : '—' }}</b></div>
                  <div class="af-trace-kv"><span>耗时</span><b>{{ formatLatency(step.latency_ms) }}</b></div>
                  <div class="af-trace-kv"><span>Token</span><b>入 {{ step.input_tokens }} / 出 {{ step.output_tokens }}</b></div>
                  <div v-if="step.tool_name" class="af-trace-kv"><span>工具</span><b class="af-mono">{{ step.tool_name }}</b></div>
                  <div v-if="step.tool_error" class="af-trace-kv"><span>错误码</span><b class="is-err">{{ step.tool_error }}</b></div>
                  <div class="af-muted af-trace-note">
                    <template v-if="step.span_id || step.subtask_id">
                      span_id / subtask_id 由后端下发（G7 闭环），子任务归属为精确匹配。
                    </template>
                    <template v-else>
                      该步骤为任务级节点（无 subtask_id），不归属任何子任务。
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </a-spin>
  </div>
</template>

<style scoped>
.af-trace {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.af-trace-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: var(--color-fill-1, #fafbfc);
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
}

.af-trace-slider {
  width: 180px;
  margin: 0 4px;
}

.af-trace-bar-spacer {
  flex: 1;
}

.af-trace-node {
  width: 130px;
}

.af-trace-err-count {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 8px;
  background: rgb(var(--danger-1));
  color: rgb(var(--danger-6));
  font-size: 11px;
}

.af-trace-checkpoints {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px dashed var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: #fff;
}

.af-trace-cp-list {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  flex: 1;
}

.af-trace-cp {
  flex: none;
  min-width: 118px;
  padding: 6px 8px;
  border: 1px solid var(--color-border-1, #f2f3f5);
  border-radius: 2px;
  background: var(--color-fill-1, #fafbfc);
}

.af-trace-cp-step {
  font-size: 11px;
  color: rgb(var(--primary-6));
  font-variant-numeric: tabular-nums;
}

.af-trace-cp-node {
  font-size: 12px;
  margin: 2px 0;
}

.af-trace-cp-meta {
  font-size: 10.5px;
}

.af-trace-spin {
  width: 100%;
}

.af-trace-list {
  max-height: 460px;
  overflow-y: auto;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: #fff;
}

.af-trace-item {
  display: grid;
  grid-template-columns: 92px 20px 1fr;
  gap: 8px;
  padding: 8px 12px 8px 8px;
  border-bottom: 1px solid var(--color-border-1, #f2f3f5);
}

.af-trace-item:last-child {
  border-bottom: 0;
}

.af-trace-item.is-error {
  background: #fff8f7;
}

.af-trace-item.is-current {
  background: #f2f7ff;
  box-shadow: inset 3px 0 0 rgb(var(--primary-6));
}

.af-trace-time {
  font-size: 11px;
  color: var(--color-text-2);
  text-align: right;
  line-height: 17px;
}

.af-trace-offset {
  color: var(--color-text-3);
  font-size: 10.5px;
}

.af-trace-axis {
  position: relative;
  display: flex;
  justify-content: center;
}

.af-trace-axis::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: -8px;
  width: 1px;
  background: var(--color-border-2, #e5e6eb);
}

.af-trace-dot {
  position: relative;
  z-index: 1;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-top: 6px;
  background: #c9cdd4;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px #e5e6eb;
}

.af-trace-item.is-tool .af-trace-dot {
  background: rgb(var(--primary-6));
  box-shadow: 0 0 0 1px rgb(var(--primary-3));
}

.af-trace-item.is-error .af-trace-dot {
  background: rgb(var(--danger-6));
  box-shadow: 0 0 0 1px rgb(var(--danger-3));
}

.af-trace-line {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  min-height: 22px;
}

.af-trace-line-spacer {
  flex: 1;
}

.af-trace-event {
  font-size: 13px;
  color: var(--color-text-1);
}

.af-trace-event.is-err {
  color: rgb(var(--danger-6));
}

.af-trace-error-code {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 10.5px;
  color: rgb(var(--danger-6));
  background: rgb(var(--danger-1));
  border-radius: 2px;
  padding: 1px 5px;
}

.af-trace-toggle {
  font-size: 12px;
}

.af-trace-detail {
  margin-top: 8px;
  padding: 10px;
  background: var(--color-fill-1, #fafbfc);
  border: 1px solid var(--color-border-1, #f2f3f5);
  border-radius: 2px;
}

.af-trace-detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
  gap: 12px;
}

.af-trace-detail-label {
  font-size: 11px;
  color: var(--color-text-3);
  margin-bottom: 6px;
}

.af-trace-kv {
  display: flex;
  gap: 8px;
  font-size: 11.5px;
  line-height: 20px;
}

.af-trace-kv span {
  flex: none;
  width: 64px;
  color: var(--color-text-3);
}

.af-trace-kv b {
  font-weight: 400;
  color: var(--color-text-2);
  word-break: break-all;
}

.af-trace-kv b.is-err {
  color: rgb(var(--danger-6));
}

.af-trace-note {
  margin-top: 8px;
  font-size: 10.5px;
  line-height: 16px;
}

.af-trace-empty {
  padding: 24px 0;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: #fff;
}
</style>
