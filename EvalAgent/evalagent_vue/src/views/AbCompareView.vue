<script setup lang="ts">
/**
 * A/B 实验看板（本轮改接后端对比聚合接口，对齐/差值/汇总计算全部在后端完成）：
 * 1) 下拉选择两个状态=done 的评测任务（A 组 / B 组）
 * 2) 前端校验：两任务必须来自同一数据集（dataset_id），否则弹窗拦截
 * 3) 调用 POST /eval/runs/compare（B1-5）：后端完成 question 对齐、四项指标差值 Δ 与汇总
 * 4) ECharts 柱状图展示后端返回的 A/B 两组四项 RAGAS 指标均值
 * 5) 样本左右分栏对比表格，后端 is_diff（|Δ|>0.1）标黄
 * 6) 生成并导出 Markdown 对比报告（Blob 下载）
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import EmptyState from '@/components/EmptyState.vue'
import RagRadarChart from '@/components/RagRadarChart.vue'
import { compareRuns, getRun, listRuns } from '@/api/eval'
import { listDatasets } from '@/api/dataset'
import {
  diffRows,
  METRIC_KEYS,
  METRIC_LABELS,
  type AbSummary,
  type AlignedRow,
} from '@/utils/abCompare'
import { buildAbReport } from '@/utils/markdown'
import { downloadText } from '@/utils/download'
import { formatExpLabel } from '@/utils/expParams'
import type { Dataset, EvalRun } from '@/api/types'

// ---------- 任务选择 ----------
const runs = ref<EvalRun[]>([])
const doneRuns = computed(() => runs.value.filter((r) => r.status === 'done'))
const taskAId = ref<number | undefined>()
const taskBId = ref<number | undefined>()
const datasets = ref<Dataset[]>([])

// ---------- 对比数据 ----------
const comparing = ref(false)
const compared = ref(false)
const runA = ref<EvalRun | null>(null)
const runB = ref<EvalRun | null>(null)
const alignedRows = ref<AlignedRow[]>([])
const summary = ref<AbSummary[]>([])

const datasetById = computed(() => new Map(datasets.value.map((d) => [d.id, d])))

const datasetName = computed(() => {
  if (!runA.value) return ''
  const ds = datasetById.value.get(runA.value.dataset_id)
  return ds ? `${ds.name}（ID=${ds.id}）` : `dataset_id=${runA.value.dataset_id}`
})

const diffList = computed(() => diffRows(alignedRows.value))
const matchedCount = computed(() => alignedRows.value.filter((r) => r.a && r.b).length)

// ---------- 样本对比表格（前端分页渲染，避免大 DOM 卡死） ----------
const tablePage = ref(1)
const tablePageSize = 20
const tableRows = computed(() => alignedRows.value)
const pagedRows = computed(() =>
  tableRows.value.slice((tablePage.value - 1) * tablePageSize, tablePage.value * tablePageSize),
)
const tableTotal = computed(() => tableRows.value.length)

// ---------- ECharts ----------
const chartEl = ref<HTMLElement | null>(null)
const chart = shallowRef<echarts.ECharts | null>(null)

function renderChart() {
  if (!chartEl.value || !runA.value || !runB.value) return
  // compared 的 v-if 会重建图表 DOM：旧 ECharts 实例挂在已移除的元素上时必须
  // dispose 重建，否则 setOption 画到离屏节点，图区呈现空白
  if (chart.value && chart.value.getDom() !== chartEl.value) {
    chart.value.dispose()
    chart.value = null
  }
  if (!chart.value) {
    chart.value = echarts.init(chartEl.value)
  }
  const option: echarts.EChartsOption = {
    tooltip: { trigger: 'axis' },
    legend: { data: [`A 组（任务 #${runA.value.id}）`, `B 组（任务 #${runB.value.id}）`] },
    grid: { left: 48, right: 24, top: 48, bottom: 32 },
    xAxis: {
      type: 'category',
      data: METRIC_KEYS.map((k) => METRIC_LABELS[k]),
      axisLabel: { interval: 0, fontSize: 11 },
    },
    yAxis: { type: 'value', max: 1, min: 0 },
    series: [
      {
        name: `A 组（任务 #${runA.value.id}）`,
        type: 'bar',
        barGap: 0,
        itemStyle: { color: '#165DFF' },
        data: summary.value.map((s) => (s.avgA === null ? null : Number(s.avgA.toFixed(4)))),
        label: { show: true, position: 'top', fontSize: 10, formatter: (p) => Number(p.value).toFixed(3) },
      },
      {
        name: `B 组（任务 #${runB.value.id}）`,
        type: 'bar',
        itemStyle: { color: '#00B42A' },
        data: summary.value.map((s) => (s.avgB === null ? null : Number(s.avgB.toFixed(4)))),
        label: { show: true, position: 'top', fontSize: 10, formatter: (p) => Number(p.value).toFixed(3) },
      },
    ],
  }
  chart.value.setOption(option)
}

function handleResize() {
  chart.value?.resize()
}

// ---------- 执行对比 ----------
async function runCompare() {
  if (!taskAId.value || !taskBId.value) {
    ElMessage.warning('请先选择 A 组与 B 组评测任务')
    return
  }
  if (taskAId.value === taskBId.value) {
    ElMessage.warning('A 组与 B 组不能选择同一个任务')
    return
  }

  // 前置校验：必须是同一个数据集（不满足直接拦截，不请求后端明细）
  const ra = await getRun(taskAId.value)
  const rb = await getRun(taskBId.value)
  if (ra.dataset_id !== rb.dataset_id) {
    ElMessageBox.alert(
      `A 组（数据集 ID=${ra.dataset_id}）与 B 组（数据集 ID=${rb.dataset_id}）不属于同一数据集，无法对比。请选择同一数据集下两份评测任务。`,
      '数据集不一致',
      { confirmButtonText: '知道了', type: 'warning' },
    )
    return
  }

  comparing.value = true
  compared.value = false
  try {
    // 【本轮改接后端】样本对齐、四项指标差值 Δ、汇总全部由后端 /eval/runs/compare 完成，
    // 前端仅把响应体映射为页面既有渲染结构（字段一一对应，不做二次计算）
    const res = await compareRuns(ra.id, rb.id)
    runA.value = res.run_a
    runB.value = res.run_b
    alignedRows.value = res.rows.map((row) => ({
      question: row.question,
      a: row.a,
      b: row.b,
      diffs: row.diffs,
      isDiff: row.is_diff,
    }))
    summary.value = res.summary.map((s) => ({
      metric: s.metric,
      avgA: s.avg_a,
      avgB: s.avg_b,
      diff: s.diff,
    }))
    compared.value = true
    tablePage.value = 1
    await nextTick()
    renderChart()
  } finally {
    comparing.value = false
  }
}

// ---------- 导出 Markdown 报告 ----------
function exportReport() {
  if (!runA.value || !runB.value) return
  const md = buildAbReport({
    runA: runA.value,
    runB: runB.value,
    dataset: datasets.value.find((d) => d.id === runA.value!.dataset_id) ?? null,
    summary: summary.value,
    rows: alignedRows.value,
    diffList: diffList.value,
    generatedAt: new Date().toLocaleString(),
  })
  downloadText(
    `ab_report_run${runA.value.id}_vs_run${runB.value.id}.md`,
    md,
    'text/markdown;charset=utf-8',
  )
  ElMessage.success('Markdown 对比报告已开始下载')
}

async function refreshRuns() {
  const res = await listRuns(1, 100)
  runs.value = res.items
}

function runLabel(run: EvalRun): string {
  const ds = datasetById.value.get(run.dataset_id)
  const dsName = ds ? ds.name : `数据集#${run.dataset_id}`
  return `#${run.id} · ${dsName} · ${formatExpLabel(run)}`
}

// ---------- 同数据集联动过滤（B 方案）：选定一侧后，另一侧仅列出同 dataset_id 的任务 ----------
const candidateRunsA = computed(() => {
  if (!taskBId.value) return doneRuns.value
  const b = runs.value.find((r) => r.id === taskBId.value)
  return b ? doneRuns.value.filter((r) => r.dataset_id === b.dataset_id) : doneRuns.value
})
const candidateRunsB = computed(() => {
  if (!taskAId.value) return doneRuns.value
  const a = runs.value.find((r) => r.id === taskAId.value)
  return a ? doneRuns.value.filter((r) => r.dataset_id === a.dataset_id) : doneRuns.value
})

function fmt(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(4)
}

function rowClassName({ row }: { row: AlignedRow }): string {
  return row.isDiff ? 'ea-diff-row' : ''
}

function diffText(row: AlignedRow, key: (typeof METRIC_KEYS)[number]): string {
  const d = row.diffs[key]
  return d === null ? '—' : (d > 0 ? '+' : '') + d.toFixed(4)
}

watch([taskAId, taskBId], () => {
  compared.value = false
  alignedRows.value = []
})

// 切换一侧后，另一侧已选任务若不再属于同数据集，自动清空避免无效组合
watch(taskAId, () => {
  if (taskBId.value && !candidateRunsB.value.some((r) => r.id === taskBId.value)) {
    taskBId.value = undefined
  }
})
watch(taskBId, () => {
  if (taskAId.value && !candidateRunsA.value.some((r) => r.id === taskAId.value)) {
    taskAId.value = undefined
  }
})

onMounted(async () => {
  try {
    const [runRes, dsRes] = await Promise.all([listRuns(1, 100), listDatasets(1, 100)])
    runs.value = runRes.items
    datasets.value = dsRes.items
  } catch {
    // 列表加载失败由全局拦截器提示
  }
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart.value?.dispose()
})
</script>

<template>
  <div class="ea-content">
    <div class="ea-page-header">
      <div class="ea-page-title">A/B 实验看板</div>
      <div class="ea-page-sub">
        对比两套评测任务结果：样本对齐、指标差值计算与汇总由后端 /eval/runs/compare 聚合接口完成，前端仅渲染
      </div>
    </div>

    <!-- 选择器 -->
    <div class="ea-card">
      <div class="ea-card-header">
        <div class="ea-card-title">选择对比任务（仅列出状态=已完成的任务）</div>
      </div>
      <div class="ea-card-body">
        <div class="select-row">
          <div class="select-group">
            <div class="select-title a-title">A 组</div>
            <el-select
              v-model="taskAId"
              placeholder="选择 A 组评测任务"
              filterable
              style="width: 100%"
            >
              <el-option v-for="r in candidateRunsA" :key="r.id" :label="runLabel(r)" :value="r.id" />
            </el-select>
          </div>
          <span class="vs">VS</span>
          <div class="select-group">
            <div class="select-title b-title">B 组</div>
            <el-select
              v-model="taskBId"
              :placeholder="taskAId ? '仅列出同数据集任务' : '选择 B 组评测任务'"
              filterable
              style="width: 100%"
            >
              <el-option v-for="r in candidateRunsB" :key="r.id" :label="runLabel(r)" :value="r.id" />
            </el-select>
          </div>
          <el-button type="primary" :loading="comparing" style="margin-left: 8px" @click="runCompare">
            开始对比
          </el-button>
          <el-button v-if="compared" :disabled="diffList.length === 0" @click="exportReport">
            导出 Markdown 报告
          </el-button>
        </div>
        <el-alert
          style="margin-top: 12px"
          type="info"
          :closable="false"
          show-icon
          title="前置约束：两个任务必须来自同一数据集 —— 选定一侧后另一侧仅列出同数据集任务（不同数据集仍会在对比时拦截兜底）"
        />
      </div>
    </div>

    <!-- 结果区 -->
    <template v-if="compared && runA && runB">
      <!-- 【本轮新增】A/B 双雷达：数据来自后端 /eval/runs/compare 聚合，与柱状图互为补充 -->
      <div class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">指标雷达对比（A/B 双雷达）</div>
          <span style="font-size: 12px; color: var(--ea-text-3)">后端聚合均值 · 四项 RAGAS 指标同轴对比</span>
        </div>
        <div class="ea-card-body">
          <RagRadarChart :run-id-a="runA.id" :run-id-b="runB.id" />
        </div>
      </div>

      <!-- 汇总指标柱状图 -->
      <div class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">汇总指标对比（平均分）</div>
          <span class="ea-mono" style="color: var(--ea-text-3)">数据集：{{ datasetName }} · 对齐样本 {{ matchedCount }}/{{ tableTotal }}</span>
        </div>
        <div class="ea-card-body">
          <div ref="chartEl" class="chart" />
          <el-table :data="summary" stripe size="small" style="margin-top: 8px">
            <el-table-column label="指标" min-width="240">
              <template #default="{ row }">{{ METRIC_LABELS[row.metric as keyof typeof METRIC_LABELS] }}</template>
            </el-table-column>
            <el-table-column label="A 组平均">
              <template #default="{ row }"><span class="ea-mono">{{ fmt(row.avgA) }}</span></template>
            </el-table-column>
            <el-table-column label="B 组平均">
              <template #default="{ row }"><span class="ea-mono">{{ fmt(row.avgB) }}</span></template>
            </el-table-column>
            <el-table-column label="差值（B - A）">
              <template #default="{ row }">
                <span class="ea-mono" :style="{ color: row.diff > 0 ? 'var(--ea-success)' : row.diff < 0 ? 'var(--ea-danger)' : '' }">
                  {{ row.diff === null ? '—' : (row.diff > 0 ? '+' : '') + row.diff.toFixed(4) }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <!-- 样本横向对比 -->
      <div class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">样本对比（同一 question 左右分栏，任一指标 |Δ| &gt; 0.1 标黄）</div>
          <el-pagination
            layout="total, prev, pager, next"
            small
            :total="tableTotal"
            :page-size="tablePageSize"
            :current-page="tablePage"
            @current-change="(p: number) => (tablePage = p)"
          />
        </div>
        <div class="ea-card-body" style="padding-top: 8px">
          <el-table :data="pagedRows" stripe :row-class-name="rowClassName">
            <el-table-column prop="question" label="question" min-width="200" show-overflow-tooltip />
            <el-table-column label="A 组 answer / 分数" min-width="300">
              <template #default="{ row }">
                <div class="answer-cell">
                  <div class="answer-text">{{ row.a?.answer || '—（无对齐样本）' }}</div>
                  <div class="score-line ea-mono">
                    faith {{ fmt(row.a?.faithfulness) }} · recall {{ fmt(row.a?.context_recall) }} · relevancy {{ fmt(row.a?.answer_relevancy) }}
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="B 组 answer / 分数" min-width="300">
              <template #default="{ row }">
                <div class="answer-cell">
                  <div class="answer-text">{{ row.b?.answer || '—（无对齐样本）' }}</div>
                  <div class="score-line ea-mono">
                    faith {{ fmt(row.b?.faithfulness) }} · recall {{ fmt(row.b?.context_recall) }} · relevancy {{ fmt(row.b?.answer_relevancy) }}
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="Δ faith" width="100">
              <template #default="{ row }"><span class="ea-mono">{{ diffText(row, 'faithfulness') }}</span></template>
            </el-table-column>
            <el-table-column label="Δ recall" width="100">
              <template #default="{ row }"><span class="ea-mono">{{ diffText(row, 'context_recall') }}</span></template>
            </el-table-column>
            <template #empty>
              <EmptyState description="无对齐样本" />
            </template>
          </el-table>
        </div>
      </div>
    </template>

    <div v-else-if="!comparing" class="ea-card">
      <EmptyState
        description="选择两组已完成的评测任务后点击「开始对比」"
        action-text="刷新任务列表"
        @action="refreshRuns"
      />
    </div>
  </div>
</template>

<style scoped>
.select-row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
}

.select-group {
  flex: 1;
}

.select-title {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 4px;
}

.a-title {
  color: var(--ea-primary);
}

.b-title {
  color: var(--ea-success);
}

.vs {
  font-weight: 700;
  color: var(--ea-text-3);
  padding-bottom: 4px;
}

.chart {
  width: 100%;
  height: 340px;
}

.answer-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.answer-text {
  font-size: 12px;
  color: var(--ea-text-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.score-line {
  font-size: 11px;
  color: var(--ea-text-3);
}
</style>
