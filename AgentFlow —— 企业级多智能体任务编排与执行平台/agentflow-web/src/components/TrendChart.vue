<script setup lang="ts">
/**
 * 任务完成趋势折线图 —— 纯 SVG 实现，不引入图表库（数据量 ≤ 31 点，够用且零依赖）。
 *
 * 数据契约：`GET /dashboards/trend` 的 `points`（见 types/dashboard.ts）。
 * **只渲染后端真实字段**：task_total / completed / completion_rate，不派生虚构序列
 * （后端不返回「失败数」，因此这里不画失败线，避免把推算值当真实值展示）。
 *
 * 可访问性：整体 role="img" + 摘要 aria-label；每个数据点可 Tab 聚焦，聚焦即显示 tooltip。
 */
import { computed, ref } from 'vue'
import type { TrendPoint } from '@/types/dashboard'
import { formatPercent } from '@/utils/format'

const props = withDefaults(defineProps<{ points: TrendPoint[]; height?: number }>(), { height: 228 })

/** 设计坐标系宽度（配合 viewBox + width:100% 自适应，高度按比例缩放） */
const W = 600
const PAD = { top: 14, right: 16, bottom: 28, left: 40 }

const activeIndex = ref(-1)

const innerW = computed(() => W - PAD.left - PAD.right)
const innerH = computed(() => props.height - PAD.top - PAD.bottom)
const baseY = computed(() => PAD.top + innerH.value)

/** 纵轴刻度：4 段，步长取 1/2/5×10^n，保证整数刻度且不留过多空白 */
const axis = computed(() => {
  const peak = Math.max(1, ...props.points.map((p) => Math.max(p.task_total, p.completed)))
  const intervals = 4
  const raw = peak / intervals
  const exp = Math.pow(10, Math.floor(Math.log10(raw)))
  const n = raw / exp
  const mult = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
  const step = Math.max(1, Math.round(mult * exp))
  return { max: step * intervals, ticks: Array.from({ length: intervals + 1 }, (_, i) => step * i) }
})

interface Coord {
  point: TrendPoint
  x: number
  yTotal: number
  yDone: number
  label: string
  showLabel: boolean
}

const coords = computed<Coord[]>(() => {
  const list = props.points
  const n = list.length
  if (n === 0) return []
  const max = axis.value.max
  const y = (v: number): number => PAD.top + innerH.value * (1 - Math.min(v, max) / max)
  return list.map((point, i) => {
    const x = n === 1 ? PAD.left + innerW.value / 2 : PAD.left + (i * innerW.value) / (n - 1)
    return {
      point,
      x,
      yTotal: y(point.task_total),
      yDone: y(point.completed),
      label: point.date.slice(5), // YYYY-MM-DD → MM-DD
      showLabel: n <= 8 || i % 2 === 0 || i === n - 1,
    }
  })
})

function linePath(pick: (c: Coord) => number): string {
  return coords.value.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)} ${pick(c).toFixed(1)}`).join(' ')
}

const totalPath = computed(() => linePath((c) => c.yTotal))
const donePath = computed(() => linePath((c) => c.yDone))

/** 面积仅铺在「新建任务」线下，作为视觉底衬 */
const totalArea = computed(() => {
  const list = coords.value
  if (list.length < 2) return ''
  const first = list[0]
  const last = list[list.length - 1]
  return `${totalPath.value} L${last.x.toFixed(1)} ${baseY.value.toFixed(1)} L${first.x.toFixed(1)} ${baseY.value.toFixed(1)} Z`
})

const active = computed<Coord | null>(() => (activeIndex.value >= 0 ? coords.value[activeIndex.value] ?? null : null))

/** tooltip 用百分比定位（svg 按 viewBox 等比缩放，百分比即设计坐标占比） */
const tipStyle = computed(() => {
  const c = active.value
  if (!c) return {}
  return {
    left: `${Math.min(Math.max((c.x / W) * 100, 12), 88)}%`,
    top: `${(c.yTotal / props.height) * 100}%`,
  }
})

const ariaSummary = computed(() => {
  const list = props.points
  if (list.length === 0) return '任务完成趋势：暂无数据'
  const total = list.reduce((sum, p) => sum + p.task_total, 0)
  const done = list.reduce((sum, p) => sum + p.completed, 0)
  return `任务完成趋势折线图，${list[0].date} 至 ${list[list.length - 1].date}，共新建 ${total} 个任务，完成 ${done} 个`
})
</script>

<template>
  <div class="trend-wrap">
    <svg
      class="trend-svg"
      :viewBox="`0 0 ${W} ${height}`"
      role="img"
      :aria-label="ariaSummary"
      @mouseleave="activeIndex = -1"
    >
      <!-- 横向网格 + 纵轴刻度 -->
      <g>
        <line
          v-for="tick in axis.ticks"
          :key="tick"
          class="grid"
          :x1="PAD.left"
          :x2="W - PAD.right"
          :y1="baseY - (tick / axis.max) * innerH"
          :y2="baseY - (tick / axis.max) * innerH"
        />
        <text
          v-for="tick in axis.ticks"
          :key="`t-${tick}`"
          class="axis-text axis-y"
          :x="PAD.left - 8"
          :y="baseY - (tick / axis.max) * innerH + 3.5"
          text-anchor="end"
        >
          {{ tick }}
        </text>
      </g>

      <!-- 新建任务：面积 + 折线；已完成：虚线 -->
      <path v-if="totalArea" class="area-total" :d="totalArea" />
      <!-- 已完成先行绘制：与新建任务重合时（全部完成）让实线主序列在上层可辨 -->
      <path class="line-done" :d="donePath" />
      <path class="line-total" :d="totalPath" />

      <!-- 数据点（同时作为 hover / focus 命中区，命中区覆盖整列便于交互） -->
      <g>
        <template v-for="(c, i) in coords" :key="c.point.date">
          <line
            v-if="activeIndex === i"
            class="guide"
            :x1="c.x"
            :x2="c.x"
            :y1="PAD.top"
            :y2="baseY"
          />
          <circle class="dot-total" :cx="c.x" :cy="c.yTotal" :r="activeIndex === i ? 4 : 3" />
          <circle class="dot-done" :cx="c.x" :cy="c.yDone" :r="activeIndex === i ? 3.5 : 2.5" />
          <rect
            class="hit"
            :x="c.x - (innerW / Math.max(coords.length - 1, 1)) / 2"
            :y="PAD.top"
            :width="innerW / Math.max(coords.length - 1, 1)"
            :height="innerH"
            tabindex="0"
            :aria-label="`${c.point.date}：新建 ${c.point.task_total}，完成 ${c.point.completed}，完成率 ${formatPercent(c.point.completion_rate)}`"
            @mouseenter="activeIndex = i"
            @focus="activeIndex = i"
            @blur="activeIndex = -1"
          >
            <title>{{ c.point.date }} · 新建 {{ c.point.task_total }} · 完成 {{ c.point.completed }}</title>
          </rect>
        </template>
      </g>

      <!-- 横轴日期 -->
      <g>
        <text
          v-for="c in coords"
          :key="`x-${c.point.date}`"
          v-show="c.showLabel"
          class="axis-text"
          :x="c.x"
          :y="baseY + 16"
          text-anchor="middle"
        >
          {{ c.label }}
        </text>
      </g>
    </svg>

    <!-- hover / focus 数值卡（真实字段，无推算） -->
    <div v-if="active" class="tip" :style="tipStyle">
      <div class="tip-date af-num">{{ active.point.date }}</div>
      <div class="tip-row">
        <i class="dot is-total" />新建任务<b class="af-num">{{ active.point.task_total }}</b>
      </div>
      <div class="tip-row">
        <i class="dot is-done" />已完成<b class="af-num">{{ active.point.completed }}</b>
      </div>
      <div class="tip-row">
        <i class="dot is-rate" />完成率<b class="af-num">{{ formatPercent(active.point.completion_rate) }}</b>
      </div>
    </div>
  </div>
</template>

<style scoped>
.trend-wrap {
  position: relative;
  padding: 4px 0 0;
}

.trend-svg {
  display: block;
  width: 100%;
  height: auto;
}

.grid {
  stroke: var(--color-fill-2);
  stroke-width: 1;
}

.axis-text {
  fill: var(--color-text-3);
  font-size: 10px;
}

.area-total {
  fill: rgb(var(--primary-6));
  opacity: 0.08;
}

.line-total {
  fill: none;
  stroke: rgb(var(--primary-6));
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.line-done {
  fill: none;
  stroke: rgb(var(--success-6));
  stroke-width: 1.5;
  stroke-dasharray: 4 3;
  stroke-linecap: round;
}

.dot-total {
  fill: rgb(var(--primary-6));
}

.dot-done {
  fill: rgb(var(--success-6));
}

.guide {
  stroke: var(--color-border-2);
  stroke-width: 1;
  stroke-dasharray: 3 3;
}

/* 命中区透明但可聚焦：键盘 Tab 逐点读数值 */
.hit {
  fill: transparent;
  outline: none;
  cursor: crosshair;
}

.hit:focus-visible {
  stroke: rgb(var(--primary-4));
  stroke-width: 1;
}

.tip {
  position: absolute;
  transform: translate(-50%, calc(-100% - 12px));
  min-width: 132px;
  padding: 8px 10px;
  border: 1px solid var(--color-border-2);
  border-radius: 4px;
  background: var(--color-bg-elevated);
  box-shadow: 0 4px 12px rgb(0 0 0 / 8%);
  pointer-events: none;
  z-index: 2;
}

.tip-date {
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--color-text-3);
}

.tip-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  line-height: 20px;
  color: var(--color-text-2);
}

.tip-row b {
  margin-left: auto;
  font-weight: 600;
  color: var(--color-text-1);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: 0 0 6px;
}

.dot.is-total {
  background: rgb(var(--primary-6));
}

.dot.is-done {
  background: rgb(var(--success-6));
}

.dot.is-rate {
  background: var(--color-text-4);
}
</style>
