<script setup lang="ts">
// Trace 链路日志页（仅管理员）：筛选 + 分页表格 + 行展开链路详情 + 统计汇总 + 工具调用明细。
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Search, CopyDocument } from '@element-plus/icons-vue'
import { getTrace, listToolCalls, listTraces, traceSummary } from '@/api/trace'
import { statsOverview } from '@/api/features'
import type { StatsOverview, TraceDetail, TraceLog, TraceSummary, TraceToolCall } from '@/types'
import EmptyState from '@/components/EmptyState.vue'
import PagePagination from '@/components/PagePagination.vue'
import RouteTag from '@/components/RouteTag.vue'
import SkeletonLoading from '@/components/SkeletonLoading.vue'

const activeTab = ref('list')

// 列表筛选
const filterRoute = ref('')
const filterStatus = ref('')
const filterUserId = ref<number | null>(null)

const traces = ref<TraceLog[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const loading = ref(false)

// 行展开详情
const detailMap = reactive<Record<string, TraceDetail>>({})
const detailLoading = reactive<Record<string, boolean>>({})

// 统计汇总
const summary = ref<TraceSummary | null>(null)
// 全局统计（/stats/overview，仅管理员）
const overview = ref<StatsOverview | null>(null)

// 工具调用明细
const toolTraceId = ref('')
const toolCalls = ref<TraceToolCall[]>([])

const ROUTES = ['rag', 'db_query', 'report', 'parallel_compare', 'direct']
const STATUSES = ['success', 'failed']

async function loadTraces() {
  loading.value = true
  try {
    const params: Record<string, unknown> = { page: page.value, size: size.value }
    if (filterRoute.value) params.route = filterRoute.value
    if (filterStatus.value) params.status = filterStatus.value
    if (filterUserId.value) params.user_id = filterUserId.value
    const res = await listTraces(params)
    traces.value = res.items
    total.value = res.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

async function onExpand(row: TraceLog, expandedRows: TraceLog[]) {
  const isOpen = expandedRows.some((r) => r.trace_id === row.trace_id)
  if (!isOpen || detailMap[row.trace_id]) return
  detailLoading[row.trace_id] = true
  try {
    detailMap[row.trace_id] = await getTrace(row.trace_id)
  } catch {
    /* ignore */
  } finally {
    detailLoading[row.trace_id] = false
  }
}

async function loadSummary() {
  summary.value = await traceSummary().catch(() => null)
  overview.value = await statsOverview().catch(() => null)
}

async function queryToolCalls() {
  if (!toolTraceId.value.trim()) {
    ElMessage.warning('请输入 Trace ID')
    return
  }
  try {
    const res = await listToolCalls(toolTraceId.value.trim())
    toolCalls.value = res.items
  } catch {
    toolCalls.value = []
  }
}

/** 跳转到「工具调用明细」Tab 并自动按该行完整 Trace ID 查询 */
function goToolCalls(row: TraceLog) {
  toolTraceId.value = row.trace_id
  activeTab.value = 'toolcalls'
  queryToolCalls()
}

/** 复制完整 Trace ID（明细查询需要完整 ID，列表仅展示前 12 位） */
async function copyText(text: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success('完整 Trace ID 已复制')
  } catch {
    ElMessage.error('复制失败')
  }
}

function statusClass(s: string) {
  return s === 'success' ? 'tag-green' : 'tag-red'
}

// 路由分布条宽度（按最大值归一化）
function distPct(count: number) {
  const vals = Object.values(summary.value?.route_distribution || {}) as number[]
  const max = Math.max(...vals, 1)
  return `${Math.round((count / max) * 100)}%`
}

onMounted(() => {
  loadTraces()
  loadSummary()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">Trace 链路日志</div>
        <div class="page-desc">追踪每一次 Agent 调用：意图路由、工具调用、重试与异常明细</div>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <!-- 列表 -->
      <el-tab-pane label="Trace 列表" name="list">
        <div class="filter-bar card">
          <div class="filter-item">
            <span class="field-label">路由</span>
            <el-select v-model="filterRoute" clearable placeholder="全部" style="width: 150px">
              <el-option v-for="r in ROUTES" :key="r" :label="r" :value="r" />
            </el-select>
          </div>
          <div class="filter-item">
            <span class="field-label">状态</span>
            <el-select v-model="filterStatus" clearable placeholder="全部" style="width: 150px">
              <el-option v-for="s in STATUSES" :key="s" :label="s" :value="s" />
            </el-select>
          </div>
          <div class="filter-item">
            <span class="field-label">用户 ID</span>
            <el-input-number v-model="filterUserId" :min="0" :controls="false" placeholder="全部" style="width: 150px" />
          </div>
          <el-button type="primary" @click="page = 1; loadTraces()"><el-icon><Search /></el-icon>查询</el-button>
          <el-button @click="filterRoute = ''; filterStatus = ''; filterUserId = null; page = 1; loadTraces()">重置</el-button>
        </div>

        <div class="card">
          <SkeletonLoading v-if="loading" :rows="6" />
          <template v-else>
            <el-table :data="traces" style="width: 100%" @expand-change="onExpand" row-key="trace_id">
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="detail-pad">
                    <div v-if="detailLoading[row.trace_id]" class="small muted">加载中…</div>
                    <template v-else-if="detailMap[row.trace_id]">
                      <div class="row" style="gap: 8px; flex-wrap: wrap">
                        <RouteTag :route="detailMap[row.trace_id].agent_route" />
                        <span class="ui-tag" :class="statusClass(detailMap[row.trace_id].status)">{{ detailMap[row.trace_id].status }}</span>
                        <span class="ui-tag tag-gray">模型 {{ detailMap[row.trace_id].model }}</span>
                        <span class="ui-tag tag-gray">耗时 {{ detailMap[row.trace_id].total_latency_ms }}ms</span>
                        <span class="ui-tag tag-gray">tokens {{ detailMap[row.trace_id].total_tokens }}</span>
                        <span class="ui-tag tag-gray">工具调用 {{ detailMap[row.trace_id].tool_call_count }}</span>
                        <span class="ui-tag tag-gray">检索分块 {{ detailMap[row.trace_id].num_chunks }}</span>
                      </div>

                      <div v-if="detailMap[row.trace_id].error_msg" class="error-card" style="margin-top: 12px">
                        ⚠️ 异常报错：{{ detailMap[row.trace_id].error_msg }}
                      </div>

                      <div class="strong" style="margin: 14px 0 8px">工具调用明细</div>
                      <EmptyState
                        v-if="!detailMap[row.trace_id].tool_calls?.length"
                        :title="detailMap[row.trace_id].agent_route === 'direct' ? '直接回答路由未经过工具节点，无工具调用' : '无工具调用记录'"
                        icon="🛠️"
                      />
                      <div v-for="call in detailMap[row.trace_id].tool_calls" :key="call.id" class="chain-step">
                        <div class="chain-name">
                          {{ call.step_name }} · <span class="mono">{{ call.tool_name }}</span>
                          <span class="ui-tag" :class="call.status === 'success' ? 'tag-green' : 'tag-red'">{{ call.status }}</span>
                          <span class="small muted">{{ call.latency_ms }}ms</span>
                        </div>
                        <div class="small muted">输入：{{ JSON.stringify(call.tool_input ?? {}) }}</div>
                        <div class="small muted">输出：{{ (call.tool_output || '').slice(0, 200) }}</div>
                      </div>
                    </template>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="Trace ID" width="160">
                <template #default="{ row }">
                  <span class="mono trace-id" title="点击复制完整 Trace ID" @click="copyText(row.trace_id)">
                    {{ row.trace_id.slice(0, 12) }}<el-icon class="trace-copy"><CopyDocument /></el-icon>
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="时间" width="170">
                <template #default="{ row }">{{ (row.started_at || row.created_at || '').slice(0, 19) }}</template>
              </el-table-column>
              <el-table-column prop="user_id" label="用户ID" width="80" />
              <el-table-column prop="kb_id" label="知识库ID" width="90" />
              <el-table-column label="路由" width="150" class-name="route-col">
                <template #default="{ row }"><RouteTag :route="row.agent_route" /></template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }"><span class="ui-tag" :class="statusClass(row.status)">{{ row.status }}</span></template>
              </el-table-column>
              <!-- 【v1.16】截断标识：命中 max_tokens 的回答在列表上直接可见，便于统计截断率 -->
              <el-table-column label="截断" width="76" align="center">
                <template #default="{ row }">
                  <el-tooltip v-if="row.is_truncated" content="回答达到 max_tokens 上限，未完整展示" placement="top">
                    <span class="ui-tag tag-red">截断</span>
                  </el-tooltip>
                  <span v-else>—</span>
                </template>
              </el-table-column>
              <el-table-column prop="model" label="模型" width="130" show-overflow-tooltip />
              <el-table-column prop="total_tokens" label="tokens" width="90" />
              <el-table-column prop="total_latency_ms" label="耗时(ms)" width="100" />
              <el-table-column prop="tool_call_count" label="工具调用" width="90" />
              <el-table-column label="操作" width="80">
                <template #default="{ row }">
                  <el-button text type="primary" size="small" @click="goToolCalls(row)">明细</el-button>
                </template>
              </el-table-column>
              <template #empty><EmptyState title="暂无 Trace 记录" desc="调整筛选条件后重试" icon="🔍" /></template>
            </el-table>
            <PagePagination :total="total" :page="page" :size="size" @update:page="(p) => { page = p; loadTraces() }" @update:size="(s) => { size = s; page = 1; loadTraces() }" />
          </template>
        </div>
      </el-tab-pane>

      <!-- 统计汇总 -->
      <el-tab-pane label="统计汇总" name="summary">
        <div class="card" style="margin-bottom: 16px">
          <div class="card-header"><span class="card-title">系统概览</span></div>
          <div class="card-body">
            <div class="stat-grid" style="margin-bottom: 4px">
              <div class="stat-card"><div class="stat-label">用户数</div><div class="stat-value">{{ overview?.user_count ?? '—' }}</div></div>
              <div class="stat-card"><div class="stat-label">知识库数</div><div class="stat-value">{{ overview?.kb_count ?? '—' }}</div></div>
              <div class="stat-card"><div class="stat-label">文档数</div><div class="stat-value">{{ overview?.doc_count ?? '—' }}</div></div>
              <div class="stat-card"><div class="stat-label">问答数</div><div class="stat-value">{{ overview?.qa_count ?? '—' }}</div></div>
              <div class="stat-card"><div class="stat-label">平均耗时(ms)</div><div class="stat-value">{{ overview?.avg_latency ?? '—' }}</div></div>
              <div class="stat-card"><div class="stat-label">Token 总量</div><div class="stat-value">{{ overview?.token_sum ?? '—' }}</div></div>
            </div>
          </div>
        </div>
        <div class="stat-grid">
          <div class="stat-card"><div class="stat-label">调用量</div><div class="stat-value">{{ summary?.total ?? 0 }}</div></div>
          <div class="stat-card"><div class="stat-label">平均耗时(ms)</div><div class="stat-value">{{ summary?.avg_latency_ms ?? 0 }}</div></div>
          <div class="stat-card"><div class="stat-label">Token 总量</div><div class="stat-value">{{ summary?.token_sum ?? 0 }}</div></div>
          <div class="stat-card"><div class="stat-label">LLM 调用次数</div><div class="stat-value">{{ summary?.llm_call_sum ?? 0 }}</div></div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">路由分布</span></div>
          <div class="card-body">
            <div v-for="(count, route) in summary?.route_distribution || {}" :key="route" class="dist-row">
              <span class="dist-label">{{ route }}</span>
              <div class="dist-bar"><div class="dist-fill" :style="{ width: distPct(count) }"></div></div>
              <span class="dist-count">{{ count }}</span>
            </div>
            <EmptyState v-if="!Object.keys(summary?.route_distribution || {}).length" title="暂无路由分布数据" icon="📊" />
          </div>
        </div>
      </el-tab-pane>

      <!-- 工具调用明细 -->
      <el-tab-pane label="工具调用明细" name="toolcalls">
        <div class="card">
          <div class="card-body">
            <div class="row" style="gap: 10px">
              <el-input v-model="toolTraceId" placeholder="输入完整 Trace ID" style="width: 360px" @keyup.enter="queryToolCalls" />
              <el-button type="primary" @click="queryToolCalls"><el-icon><Search /></el-icon>查询</el-button>
            </div>
            <div class="small muted" style="margin-top: 8px">提示：直接回答（direct）路由的问答不经过工具节点，明细为空属正常；完整 Trace ID 可在列表页点击复制，或点行内「明细」按钮直达</div>
            <div style="margin-top: 16px">
              <EmptyState v-if="!toolCalls.length" title="输入 Trace ID 查询工具调用明细" icon="🛠️" />
              <el-table v-else :data="toolCalls" style="width: 100%">
                <el-table-column prop="step_name" label="步骤" width="140" />
                <el-table-column prop="tool_name" label="工具" width="180" />
                <el-table-column label="状态" width="90">
                  <template #default="{ row }"><span class="ui-tag" :class="statusClass(row.status)">{{ row.status }}</span></template>
                </el-table-column>
                <el-table-column prop="latency_ms" label="耗时(ms)" width="100" />
                <el-table-column label="输入" min-width="220">
                  <template #default="{ row }"><span class="mono">{{ JSON.stringify(row.tool_input ?? {}) }}</span></template>
                </el-table-column>
                <el-table-column label="输出" min-width="220">
                  <template #default="{ row }"><span class="mono">{{ (row.tool_output || '').slice(0, 120) }}</span></template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.filter-bar { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end; padding: 16px 20px; margin-bottom: 16px; }
:deep(.route-col .cell) { white-space: nowrap; }
.filter-item { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--text-3); }
.detail-pad { padding: 16px 20px; }
.chain-step { padding: 10px 0; border-bottom: 1px dashed var(--border-light); }
.chain-step:last-child { border-bottom: none; }
.chain-name { font-size: 13px; font-weight: 600; color: var(--text-1); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.dist-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; }
.dist-label { width: 90px; font-size: 13px; color: var(--text-2); }
.dist-bar { flex: 1; height: 10px; background: #f2f3f5; border-radius: 6px; overflow: hidden; }
.dist-fill { height: 100%; background: var(--primary); border-radius: 6px; }
.dist-count { width: 60px; text-align: right; font-size: 13px; color: var(--text-2); }
.trace-id { cursor: pointer; display: inline-flex; align-items: center; gap: 4px; }
.trace-id:hover { color: var(--primary); }
.trace-copy { font-size: 12px; color: var(--text-3); }
</style>
