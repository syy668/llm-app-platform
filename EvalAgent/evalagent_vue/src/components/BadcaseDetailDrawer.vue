<script setup lang="ts">
/**
 * BadCase / 得分明细详情抽屉（纯展示组件，不二次计算）：
 * - 严格复用后端返回结构（BadcaseRow / RunScore），逐字段原样展示：
 *   question、ground_truth、answer、retrieved_contexts、四项 RAGAS 指标
 * - ground_truth：/badcases 由后端 LEFT JOIN 补齐、/scores 由后端别名给出（同一列），
 *   取值顺序 ground_truth → reference_answer；两者皆空展示 —
 * - Judge 打分理由：当前后端各接口均未返回该字段（RAGAS 仅落库数值分），
 *   若未来响应体携带 judge_reason 文本则原样展示（类型安全的可选字段探测）
 */
import { computed } from 'vue'
import { METRIC_KEYS, METRIC_LABELS } from '@/utils/abCompare'
import type { BadcaseRow, RunScore } from '@/api/types'

const visible = defineModel<boolean>({ required: true })

const props = defineProps<{
  /** 后端返回的明细行：BadCase 行（含 ground_truth）或得分明细行 */
  row: BadcaseRow | RunScore | null
}>()

const detail = computed(() => props.row)

/** ground_truth：BadcaseRow 必填；RunScore 走同义别名（后端 v1.24 起两个字段都返回），
 *  两者皆缺才展示 —，避免「预览有答案、详情是空」的观感 */
const groundTruth = computed<string | null>(() => {
  const r = props.row as { ground_truth?: string | null; reference_answer?: string | null } | null
  return r?.ground_truth ?? r?.reference_answer ?? null
})

/** Judge 打分理由：类型安全探测可选字段 judge_reason（当前后端未返回 → 展示占位说明） */
const judgeReason = computed<string | null>(() => {
  const r = props.row as { judge_reason?: unknown } | null
  if (!r) return null
  const v = r.judge_reason
  return typeof v === 'string' && v.trim().length > 0 ? v : null
})

function fmt(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(4)
}
</script>

<template>
  <el-drawer v-model="visible" title="样本详情" size="46%">
    <template #header>
      <div style="display: flex; align-items: center; gap: 10px">
        <span style="font-weight: 600">样本详情</span>
        <el-tag v-if="detail" size="small" type="info">item_id={{ detail.item_id }}</el-tag>
        <el-tag v-if="detail" size="small" class="ea-mono">latency {{ detail.latency_ms }}ms</el-tag>
      </div>
    </template>

    <template v-if="detail">
      <!-- question -->
      <div class="detail-section">
        <div class="section-title">question（评测问题）</div>
        <div class="section-body">{{ detail.question }}</div>
      </div>

      <!-- ground_truth -->
      <div class="detail-section">
        <div class="section-title">ground_truth（标准答案）</div>
        <div class="section-body">{{ groundTruth ?? '—（数据集样本已删除 / 该样本未带标准答案）' }}</div>
      </div>

      <!-- answer -->
      <div class="detail-section">
        <div class="section-title">answer（模型生成回答）</div>
        <div class="section-body">{{ detail.answer || '—' }}</div>
      </div>

      <!-- retrieved_contexts -->
      <div class="detail-section">
        <div class="section-title">retrieved_contexts（召回上下文，共 {{ (detail.retrieved_contexts ?? []).length }} 段）</div>
        <template v-if="(detail.retrieved_contexts ?? []).length > 0">
          <div v-for="(ctx, i) in detail.retrieved_contexts" :key="i" class="context-item">
            <span class="context-index ea-mono">#{{ i + 1 }}</span>
            <span class="context-text">{{ ctx }}</span>
          </div>
        </template>
        <div v-else class="section-body">—（无召回内容）</div>
      </div>

      <!-- 四项 RAG 指标 -->
      <div class="detail-section">
        <div class="section-title">RAGAS 指标（后端落库原值）</div>
        <div class="metric-grid">
          <div v-for="k in METRIC_KEYS" :key="k" class="metric-item">
            <div class="metric-label">{{ METRIC_LABELS[k] }}</div>
            <div class="metric-value ea-mono">{{ fmt(detail[k]) }}</div>
          </div>
        </div>
      </div>

      <!-- Judge 打分理由 -->
      <div class="detail-section">
        <div class="section-title">Judge 打分理由</div>
        <div v-if="judgeReason" class="section-body">{{ judgeReason }}</div>
        <div v-else class="section-body section-muted">
          当前评测链路仅落库 RAGAS 数值分数，后端接口未返回 Judge 打分理由文本
        </div>
      </div>
    </template>
    <div v-else class="section-muted" style="padding: 24px 0; text-align: center">暂无数据</div>
  </el-drawer>
</template>

<style scoped>
.detail-section {
  margin-bottom: 18px;
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--ea-text-3);
  margin-bottom: 6px;
}

.section-body {
  font-size: 13px;
  line-height: 1.7;
  color: var(--ea-text-2);
  background: var(--ea-bg-2, rgba(0, 0, 0, 0.02));
  border-radius: 6px;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.section-muted {
  color: var(--ea-text-3);
  font-style: italic;
}

.context-item {
  display: flex;
  gap: 8px;
  padding: 8px 10px;
  margin-bottom: 6px;
  border: 1px solid var(--ea-border, #e5e6eb);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
}

.context-index {
  color: var(--ea-primary);
  flex-shrink: 0;
}

.context-text {
  color: var(--ea-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}

.metric-item {
  border: 1px solid var(--ea-border, #e5e6eb);
  border-radius: 6px;
  padding: 10px 12px;
}

.metric-label {
  font-size: 11px;
  color: var(--ea-text-3);
  margin-bottom: 4px;
}

.metric-value {
  font-size: 18px;
  font-weight: 600;
  color: var(--ea-text-1, #1d2129);
}
</style>
