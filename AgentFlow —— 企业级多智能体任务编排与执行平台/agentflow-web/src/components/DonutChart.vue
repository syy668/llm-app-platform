<script setup lang="ts">
/**
 * 环形占比图（甜甜圈）—— 纯 SVG 实现，不引入图表库（分段 ≤ 8，够用且零依赖）。
 *
 * 对齐原型 p02「工具调用分布」：左侧环图 + 圆心总量，右侧色块图例（名称 + 占比 + 副行明细）。
 * 数据契约由调用方决定（本项目为 `GET /dashboards/tools` 的 call_count），
 * 组件只做展示，不派生数值；占比 = item.value / Σvalue，与后端口径一致。
 *
 * 可访问性：整体 role="img" + 摘要 aria-label；图例行可 Tab 聚焦，聚焦/悬浮即高亮对应扇区。
 */
import { computed, ref } from 'vue'

export interface DonutItem {
  name: string
  value: number
  /** 图例第二行小字（如「12 次 · 成功率 98%」），纯展示 */
  sub?: string
  /** 系列色；缺省按「已知工具名 → 原型色」映射，未知名称走调色板轮转 */
  color?: string
}

const props = withDefaults(
  defineProps<{
    items: DonutItem[]
    /** 圆心大字（总量） */
    centerValue?: string
    /** 圆心小字（单位说明） */
    centerLabel?: string
  }>(),
  { centerValue: '', centerLabel: '' },
)

/** 已知工具名固定色（照原型 p02 图例），未知工具走调色板 */
const NAMED_COLORS: Record<string, string> = {
  'sql-query': '#165DFF',
  'chart-render': '#00B42A',
  'kb-search': '#FF7D00',
  notify: '#722ED1',
}
const PALETTE = ['#165DFF', '#00B42A', '#FF7D00', '#722ED1', '#F53F3F', '#0FC6C2', '#FFC107', '#86909C']

const R = 54
const C = 2 * Math.PI * R

const total = computed(() => props.items.reduce((sum, item) => sum + item.value, 0))

interface Arc {
  name: string
  value: number
  sub: string
  color: string
  frac: number
  pct: number
  dash: string
  offset: number
}

const arcs = computed<Arc[]>(() => {
  const sum = total.value
  let acc = 0
  return props.items.map((item, index) => {
    const frac = sum > 0 ? item.value / sum : 0
    const len = frac * C
    const arc: Arc = {
      name: item.name,
      value: item.value,
      sub: item.sub ?? '',
      color: item.color ?? NAMED_COLORS[item.name] ?? PALETTE[index % PALETTE.length],
      frac,
      pct: Math.round(frac * 1000) / 10,
      dash: `${len.toFixed(2)} ${(C - len).toFixed(2)}`,
      offset: -acc,
    }
    acc += len
    return arc
  })
})

const hover = ref(-1)

/** 同步高亮：扇区与图例行互点 */
function onEnter(index: number): void {
  hover.value = index
}
function onLeave(): void {
  hover.value = -1
}

const ariaSummary = computed(() => {
  if (arcs.value.length === 0) return '占比图：暂无数据'
  const parts = arcs.value.map((a) => `${a.name} ${a.pct}%`).join('，')
  return `工具调用占比环形图，共 ${total.value} 次，${parts}`
})
</script>

<template>
  <div class="donut-wrap">
    <svg class="donut-svg" viewBox="0 0 140 140" role="img" :aria-label="ariaSummary" @mouseleave="onLeave">
      <!-- 底环 -->
      <circle cx="70" cy="70" :r="R" fill="none" stroke="#F2F3F5" stroke-width="20" />
      <!-- 扇区：每段独立 circle，dashoffset 控制起始角（整体旋转 -90° 从 12 点开始） -->
      <g transform="rotate(-90 70 70)">
        <circle
          v-for="(arc, index) in arcs"
          :key="arc.name"
          cx="70"
          cy="70"
          :r="R"
          fill="none"
          :stroke="arc.color"
          :stroke-width="hover === index ? 23 : 20"
          :stroke-dasharray="arc.dash"
          :stroke-dashoffset="arc.offset"
          :opacity="hover === -1 || hover === index ? 1 : 0.35"
          style="transition: stroke-width 0.15s ease, opacity 0.15s ease"
        />
      </g>
      <!-- 圆心总量 -->
      <text x="70" y="66" text-anchor="middle" font-size="20" font-weight="500" fill="#1D2129">
        {{ centerValue }}
      </text>
      <text x="70" y="84" text-anchor="middle" font-size="10" fill="#86909C">{{ centerLabel }}</text>
    </svg>

    <ul class="donut-legend">
      <li
        v-for="(arc, index) in arcs"
        :key="arc.name"
        class="lg-row"
        :class="{ 'is-active': hover === index }"
        tabindex="0"
        @mouseenter="onEnter(index)"
        @mouseleave="onLeave"
        @focus="onEnter(index)"
        @blur="onLeave"
      >
        <i class="lg-chip" :style="{ background: arc.color }" />
        <div class="lg-text">
          <div class="lg-name">
            <span class="lg-label">{{ arc.name }}</span>
            <span class="lg-pct af-num">{{ arc.pct }}%</span>
          </div>
          <div v-if="arc.sub" class="lg-sub af-num">{{ arc.sub }}</div>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.donut-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0 6px;
}

.donut-svg {
  flex: 0 0 158px;
  width: 158px;
  height: 158px;
  font-family: inherit;
}

.donut-legend {
  flex: 1;
  min-width: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.lg-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: 4px;
  cursor: default;
  outline: none;
  transition: background 0.15s ease;
}

.lg-row.is-active,
.lg-row:hover,
.lg-row:focus-visible {
  background: var(--color-fill-1, #f7f8fa);
}

.lg-chip {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 1px;
}

.lg-text {
  flex: 1;
  min-width: 0;
}

.lg-name {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  line-height: 16px;
}

.lg-label {
  color: var(--color-text-2, #4e5969);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lg-pct {
  flex: none;
  color: var(--color-text-1, #1d2129);
  font-weight: 500;
}

.lg-sub {
  font-size: 10px;
  line-height: 14px;
  color: var(--color-text-3, #86909c);
}
</style>
