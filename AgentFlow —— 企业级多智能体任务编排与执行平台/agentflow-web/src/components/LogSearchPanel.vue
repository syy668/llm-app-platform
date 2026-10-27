<script setup lang="ts">
/**
 * 请求日志检索面板 —— `POST /logs/search`（系统设置页，对接清单 G6）。
 *
 * 数据源是 `task_executions` 结构化执行记录（不是 stdout 日志），因此检索维度是执行轨迹语义：
 * trace_id / task_id / event / level / 时间窗，与 `GET /tasks/{id}/trace` 同源、只读。
 *
 * 三条硬约束：
 *  1. **不造假数据**：空结果走空状态，接口不可用走 pending 占位，绝不填充示例行；
 *  2. **不臆造枚举**：事件选项取自 `app/schemas/enums.py::ExecutionEvent`（7 个真实取值）；
 *  3. **请求体整洁**：后端 `LogSearchRequest` 是 `extra="forbid"`，空值必须整键省略。
 *
 * 分页为服务端分页（后端返回 `{items, page, size, total}`），排序固定 `started_at DESC`。
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { searchLogs } from '@/api/logs'
import { isApiError, isMissingEndpoint } from '@/api/http'
import { formatFullTime, formatLatency } from '@/utils/format'
import StateBlock from '@/components/StateBlock.vue'
import JsonViewer from '@/components/JsonViewer.vue'
import type { LogItem, LogSearchQuery } from '@/types/log'

const router = useRouter()

/**
 * `task_executions.event` 的真实取值：
 *  - 前 7 个来自枚举 `app/schemas/enums.py::ExecutionEvent`；
 *  - 最后一个是 `app/services/stream_context.py` 记账时实际写入的 `llm.call`
 *    （枚举里没有，但库里确有该事件，检索必须能选到）。
 */
const EVENT_OPTIONS = ['enter', 'exit', 'tool.call', 'tool.result', 'llm.call', 'replan', 'human_edit', 'error']

// ---------------------------------------------------------------------------
// 过滤条件（受控，重置即清空）
// ---------------------------------------------------------------------------
const traceIdInput = ref('')
const taskIdInput = ref<number | undefined>(undefined)
const eventFilter = ref<string | undefined>(undefined)
const levelFilter = ref<string | undefined>(undefined)
/**
 * RangePicker 的受控值；value-format 决定元素为字符串。
 * **必须带时区偏移**（`...Z` → `2026-10-22T11:00:00+08:00`）：后端 `started_at` 是 UTC naive，
 * 只传本地 naive 会被当成 UTC 直接比较，时间窗整体后移 8 小时（选了今天查不到刚才的日志）。
 */
const range = ref<(string | number | Date)[]>([])

// ---------------------------------------------------------------------------
// 结果状态
// ---------------------------------------------------------------------------
const items = ref<LogItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 接口是否可用（404 → pending 占位；其它错误 → error 占位） */
const available = ref(false)
const missing = ref(false)
const errorMessage = ref<string | null>(null)
const errorCode = ref<string | null>(null)
const errorTrace = ref<string | null>(null)

const detailVisible = ref(false)
const current = ref<LogItem | null>(null)

/** 过滤条 → 请求体：只带非空字段（后端 forbid extra 且 `task_id=0` 会 422） */
function buildQuery(targetPage: number): LogSearchQuery {
  const query: LogSearchQuery = { page: targetPage, size: pageSize.value }
  const traceId = traceIdInput.value.trim()
  if (traceId) query.trace_id = traceId
  if (typeof taskIdInput.value === 'number' && taskIdInput.value >= 1) query.task_id = taskIdInput.value
  if (eventFilter.value) query.event = eventFilter.value
  if (levelFilter.value) query.level = levelFilter.value
  const [start, end] = range.value
  if (start) query.start = String(start)
  if (end) query.end = String(end)
  return query
}

async function search(targetPage = 1): Promise<void> {
  loading.value = true
  errorMessage.value = null
  errorCode.value = null
  errorTrace.value = null
  try {
    const data = await searchLogs(buildQuery(targetPage))
    items.value = data.items
    total.value = data.total
    page.value = data.page
    available.value = true
    missing.value = false
  } catch (error) {
    items.value = []
    total.value = 0
    available.value = false
    if (isMissingEndpoint(error)) {
      missing.value = true
    } else {
      missing.value = false
      errorMessage.value = isApiError(error) ? error.message : '日志检索失败，请稍后重试'
      errorCode.value = isApiError(error) ? error.code : null
      errorTrace.value = isApiError(error) ? error.traceId ?? null : null
    }
  } finally {
    loading.value = false
  }
}

/** RangePicker 的 change 回调：清空时 value 为 undefined，只保留有效端点 */
function onRangeChange(value: unknown): void {
  const list = Array.isArray(value) ? value : []
  range.value = list.filter((item): item is string | number | Date => item !== undefined && item !== null)
  // 只有两端都选定才算有效时间窗，避免半截区间就发请求
  if (range.value.length === 2) void search(1)
}

function onEventChange(value: unknown): void {
  eventFilter.value = value === undefined || value === null || value === '' ? undefined : String(value)
  void search(1)
}

function onLevelChange(value: unknown): void {
  levelFilter.value = value === undefined || value === null || value === '' ? undefined : String(value)
  void search(1)
}

function reset(): void {
  traceIdInput.value = ''
  taskIdInput.value = undefined
  eventFilter.value = undefined
  levelFilter.value = undefined
  range.value = []
  pageSize.value = 20
  void search(1)
}

function changePage(targetPage: number): void {
  void search(targetPage)
}

function changePageSize(size: number): void {
  pageSize.value = size
  void search(1)
}

function openDetail(record: LogItem): void {
  current.value = record
  detailVisible.value = true
}

function openTask(taskId: number): void {
  router.push(`/tasks/${taskId}`)
}

/** token 展示：输入/输出，均为 0 时显示 —（该事件不记账） */
function tokensText(record: LogItem): string {
  if (!record.input_tokens && !record.output_tokens) return '—'
  return `${record.input_tokens} / ${record.output_tokens}`
}

onMounted(() => {
  void search(1)
})
</script>

<template>
  <div class="log-panel">
    <!-- 过滤条：五个维度全部对应后端字段，无前端自造条件 -->
    <div class="log-filters">
      <div class="f-item is-trace">
        <a-input v-model="traceIdInput" placeholder="trace_id（精确匹配）" allow-clear @press-enter="search(1)">
          <template #prefix><icon-search /></template>
        </a-input>
      </div>

      <div class="f-item is-task">
        <a-input-number
          v-model="taskIdInput"
          placeholder="task_id"
          :min="1"
          :precision="0"
          allow-clear
          @press-enter="search(1)"
        />
      </div>

      <div class="f-item is-event">
        <a-select :model-value="eventFilter" placeholder="事件类型" allow-clear @change="onEventChange">
          <a-option v-for="option in EVENT_OPTIONS" :key="option" :value="option">{{ option }}</a-option>
        </a-select>
      </div>

      <div class="f-item is-level">
        <a-select :model-value="levelFilter" placeholder="级别" allow-clear @change="onLevelChange">
          <a-option value="ERROR">仅 ERROR</a-option>
        </a-select>
      </div>

      <div class="f-item is-range">
        <a-range-picker
          :model-value="range"
          show-time
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          :time-picker-props="{ format: 'HH:mm' }"
          @change="onRangeChange"
        />
      </div>

      <a-button type="primary" :loading="loading" @click="search(1)">查询</a-button>
      <a-button @click="reset">重置</a-button>
    </div>

    <div class="log-meta">
      <span class="af-muted">
        共 <b class="af-num">{{ total }}</b> 条 · 按 started_at 倒序 · 数据源 task_executions
      </span>
      <span class="af-muted af-mono">POST /logs/search</span>
    </div>

    <!-- 接口不可用（旧版本后端 / 路由异常）：显式占位 -->
    <StateBlock
      v-if="missing"
      state="pending"
      description="日志检索依赖 task_executions 的结构化查询端点，当前请求未成功。"
      expected-api="POST /logs/search（trace_id / task_id / event / level / start / end / page / size）"
      :min-height="180"
    />

    <!-- 其它错误：带业务码与 trace_id，便于对号入座 -->
    <StateBlock
      v-else-if="errorMessage"
      state="error"
      title="日志检索失败"
      :description="errorMessage"
      :code="errorCode"
      :trace-id="errorTrace"
      :min-height="180"
      @retry="search(page)"
    />

    <!-- 就绪但无命中 -->
    <StateBlock
      v-else-if="available && !loading && items.length === 0"
      state="empty"
      title="没有匹配的执行记录"
      description="换一个 trace_id / 任务 ID，或放宽时间窗与事件类型后重试。"
      :min-height="180"
    />

    <a-table
      v-else
      :data="items"
      :loading="loading"
      row-key="id"
      :pagination="{
        pageSize,
        current: page,
        total,
        showTotal: true,
        showPageSize: true,
        pageSizeOptions: [10, 20, 50, 100],
      }"
      :scroll="{ x: 1180 }"
      size="small"
      :bordered="false"
      @page-change="changePage"
      @page-size-change="changePageSize"
    >
      <template #columns>
        <a-table-column title="时间" :width="160">
          <template #cell="{ record }">
            <span class="af-num af-muted">{{ formatFullTime(record.started_at) }}</span>
          </template>
        </a-table-column>
        <a-table-column title="级别" :width="80">
          <template #cell="{ record }">
            <a-tag :color="record.level === 'ERROR' ? 'red' : 'gray'" size="small">{{ record.level }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="事件" :width="110">
          <template #cell="{ record }"><span class="af-mono">{{ record.event }}</span></template>
        </a-table-column>
        <a-table-column title="节点" :width="105">
          <template #cell="{ record }"><span class="af-mono">{{ record.node }}</span></template>
        </a-table-column>
        <a-table-column title="工具" :width="125">
          <template #cell="{ record }">
            <span class="af-mono">{{ record.tool_name ?? '—' }}</span>
          </template>
        </a-table-column>
        <a-table-column title="任务" :width="112">
          <template #cell="{ record }">
            <a-link class="af-mono" @click="openTask(record.task_id)">#{{ record.task_id }}</a-link>
          </template>
        </a-table-column>
        <a-table-column title="trace_id" :ellipsis="true" :tooltip="true">
          <template #cell="{ record }"><span class="af-mono af-muted">{{ record.trace_id }}</span></template>
        </a-table-column>
        <a-table-column title="耗时" :width="86" align="right">
          <template #cell="{ record }">
            <span class="af-num">{{ record.latency_ms > 0 ? formatLatency(record.latency_ms) : '—' }}</span>
          </template>
        </a-table-column>
        <a-table-column title="tokens 入/出" :width="112" align="right">
          <template #cell="{ record }"><span class="af-num">{{ tokensText(record) }}</span></template>
        </a-table-column>
        <a-table-column title="详情" :width="70">
          <template #cell="{ record }">
            <a-link @click="openDetail(record)">查看</a-link>
          </template>
        </a-table-column>
      </template>
    </a-table>

    <!-- 单条详情：payload 为开放结构，交给 JsonViewer 通用美化 -->
    <a-modal v-model:visible="detailVisible" title="执行记录详情" :footer="false" width="760px">
      <div v-if="current" class="detail">
        <div class="detail-tags">
          <a-tag :color="current.level === 'ERROR' ? 'red' : 'gray'" size="small">{{ current.level }}</a-tag>
          <span class="af-mono">{{ current.event }}</span>
          <span class="af-muted">· {{ current.node }}</span>
          <a-link class="detail-task" @click="openTask(current.task_id)">打开任务 #{{ current.task_id }}</a-link>
        </div>

        <div class="detail-grid">
          <div class="kv"><span>时间</span><b class="af-num">{{ formatFullTime(current.started_at) }}</b></div>
          <div class="kv"><span>耗时</span><b class="af-num">{{ current.latency_ms > 0 ? formatLatency(current.latency_ms) : '—' }}</b></div>
          <div class="kv"><span>工具</span><b class="af-mono">{{ current.tool_name ?? '—' }}</b></div>
          <div class="kv"><span>tokens 入/出</span><b class="af-num">{{ tokensText(current) }}</b></div>
          <div class="kv kv-wide"><span>trace_id</span><b class="af-mono">{{ current.trace_id }}</b></div>
          <div class="kv kv-wide"><span>span_id</span><b class="af-mono">{{ current.span_id }}</b></div>
          <div v-if="current.tool_error" class="kv kv-wide">
            <span>tool_error</span><b class="is-err">{{ current.tool_error }}</b>
          </div>
        </div>

        <div class="detail-payload">
          <div class="detail-caption">payload</div>
          <JsonViewer :value="current.payload" :max-height="320" empty-text="该步骤没有 payload" />
        </div>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.log-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

/*
 * 宽度放在外层 div 上：Arco 的 select / picker 自带 `width: 100%`，
 * 直接给组件根加 class 会被同优先级的 `.arco-select-view-single.arco-select` 抢掉，
 * 这里用 :deep 提高优先级，保证过滤条不被撑成整行。
 */
.f-item {
  display: flex;
}

.f-item :deep(.arco-input-wrapper),
.f-item :deep(.arco-select),
.f-item :deep(.arco-select-view),
.f-item :deep(.arco-picker),
.f-item :deep(.arco-input-number) {
  width: 100%;
}

.f-item.is-trace {
  width: 248px;
}

.f-item.is-task {
  width: 112px;
}

.f-item.is-event {
  width: 160px;
}

.f-item.is-level {
  width: 128px;
}

.f-item.is-range {
  width: 348px;
}

.log-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 10px 0 4px;
  font-size: 11.5px;
}

.log-meta b {
  font-weight: 600;
  color: var(--color-text-1);
}

/* 详情弹窗 */
.detail-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 10px;
}

.detail-task {
  margin-left: auto;
  font-size: 12px;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 16px;
  padding: 10px 12px;
  border: 1px solid var(--color-border-1);
  border-radius: 4px;
  background: var(--color-fill-1);
  font-size: 12px;
}

.kv {
  display: flex;
  gap: 10px;
}

.kv.kv-wide {
  grid-column: span 2;
}

.kv span {
  flex: 0 0 96px;
  color: var(--color-text-3);
}

.kv b {
  font-weight: 400;
  color: var(--color-text-1);
  word-break: break-all;
}

.kv b.is-err {
  color: rgb(var(--danger-6));
}

.detail-payload {
  margin-top: 12px;
}

.detail-caption {
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--color-text-3);
}
</style>
