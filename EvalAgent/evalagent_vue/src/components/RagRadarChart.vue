<script setup lang="ts">
/**
 * RAG 四指标雷达图（可复用组件，复用项目已有 echarts 依赖，不新增 npm 包）：
 * - 单任务模式：runIdA 单独传入，数据来自 GET /eval/runs/{id}（后端落库汇总 metrics）
 * - A/B 对比模式：runIdB 同时传入，数据来自 POST /eval/runs/compare（后端聚合 summary）
 * - 全部指标值由后端接口返回，前端不做任何二次计算
 */
import { nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts'
import { compareRuns, getRun } from '@/api/eval'
import { METRIC_KEYS, METRIC_LABELS } from '@/utils/abCompare'
import type { BadcaseMetric } from '@/api/types'

const props = defineProps<{
  /** A 组（或单任务）评测运行 ID */
  runIdA: number
  /** B 组评测运行 ID：传入即切换为 A/B 对比双雷达 */
  runIdB?: number | null
}>()

const loading = ref(false)
const errorText = ref('')
const chartEl = ref<HTMLElement | null>(null)
const chart = shallowRef<echarts.ECharts | null>(null)

function renderSeries(name: string, values: Record<BadcaseMetric, number | null>) {
  return {
    name,
    type: 'radar' as const,
    // value 数组顺序与 indicator 一一对应：faithfulness / answer_relevancy /
    // context_precision / context_recall（与 METRIC_KEYS 顺序一致）
    data: [
      {
        value: METRIC_KEYS.map((k) => values[k]),
        name,
      },
    ],
    areaStyle: { opacity: 0.15 },
    lineStyle: { width: 2 },
  }
}

async function loadAndRender() {
  if (!props.runIdA || props.runIdA <= 0) return
  const isCompare = !!props.runIdB && props.runIdB > 0
  loading.value = true
  errorText.value = ''
  try {
    let option: echarts.EChartsOption
    if (isCompare) {
      // A/B 对比：后端聚合 summary（avg_a / avg_b），前端只取数渲染
      const res = await compareRuns(props.runIdA, props.runIdB as number)
      const nameA = `A 组（任务 #${res.run_a.id}）`
      const nameB = `B 组（任务 #${res.run_b.id}）`
      const valuesA = {} as Record<BadcaseMetric, number | null>
      const valuesB = {} as Record<BadcaseMetric, number | null>
      for (const s of res.summary) {
        valuesA[s.metric] = s.avg_a
        valuesB[s.metric] = s.avg_b
      }
      option = buildOption([nameA, nameB], [
        renderSeries(nameA, valuesA),
        renderSeries(nameB, valuesB),
      ])
    } else {
      // 单任务：后端落库汇总 metrics
      const run = await getRun(props.runIdA)
      const name = `任务 #${run.id}`
      const values = {} as Record<BadcaseMetric, number | null>
      for (const k of METRIC_KEYS) values[k] = run.metrics?.[k] ?? null
      option = buildOption([name], [renderSeries(name, values)])
    }
    await nextTick()
    if (!chartEl.value) return
    if (!chart.value) {
      chart.value = echarts.init(chartEl.value)
    }
    chart.value.setOption(option, true)
  } catch (err) {
    // 错误提示由全局拦截器统一弹出，这里仅记录兜底文案
    errorText.value = err instanceof Error ? err.message : '雷达图数据加载失败'
  } finally {
    loading.value = false
  }
}

function buildOption(legendNames: string[], series: echarts.SeriesOption[]): echarts.EChartsOption {
  return {
    tooltip: { trigger: 'item' },
    legend: { data: legendNames, bottom: 0 },
    radar: {
      indicator: METRIC_KEYS.map((k) => ({ name: METRIC_LABELS[k], max: 1 })),
      radius: '62%',
      center: ['50%', '46%'],
      axisName: { fontSize: 11, color: 'var(--ea-text-2, #4e5969)' },
      splitArea: { areaStyle: { color: ['transparent'] } },
    },
    series,
  }
}

function handleResize() {
  chart.value?.resize()
}

watch(
  () => [props.runIdA, props.runIdB] as const,
  () => void loadAndRender(),
)

onMounted(() => {
  void loadAndRender()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div v-loading="loading" class="rag-radar">
    <div v-if="errorText" class="radar-error">{{ errorText }}</div>
    <div v-show="!errorText" ref="chartEl" class="radar-chart" />
  </div>
</template>

<style scoped>
.rag-radar {
  width: 100%;
}

.radar-chart {
  width: 100%;
  height: 320px;
}

.radar-error {
  padding: 24px 0;
  text-align: center;
  font-size: 12px;
  color: var(--ea-text-3);
}
</style>
