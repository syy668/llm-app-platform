<script setup lang="ts">
/**
 * 记忆正文单元格（长文本）。
 *
 * 为什么不用 Arco 表格自带的 `:tooltip`：
 * 记忆正文最长可达 4000 字，内建 tooltip 没有高度上限也不会滚动，
 * 正文一长就顶出浏览器视口，顶部内容被裁掉（且必须悬停才能看）。
 *
 * 这里改成三层：
 *  1. 单元格内单行省略，保证表格行高稳定；
 *  2. 悬停 → 限高 280px 的浮层预览，内部可滚动；
 *  3. 点击 → 抽屉看全文（保留原始换行），带层级/重要度/命中/时间等元信息。
 *
 * 记忆管理页（MemoryView）与任务详情页记忆面板（TaskDetailView）共用本组件。
 */
import { computed, ref } from 'vue'
import { formatFullTime } from '@/utils/format'
import type { MemoryItem } from '@/types/memory'

const props = defineProps<{ item: MemoryItem }>()

const detailVisible = ref(false)

/** 短文本不挂浮层，避免无意义的 hover 干扰 */
const needPreview = computed(() => props.item.content.length > 60)

const scopeColor = computed(() => (props.item.scope === 'long' ? 'purple' : 'arcoblue'))

function openDetail(): void {
  detailVisible.value = true
}
</script>

<template>
  <span class="mem-cell">
    <a-popover
      v-if="needPreview"
      position="right"
      trigger="hover"
      :content-style="{ padding: '10px 12px' }"
    >
      <template #content>
        <div class="mem-preview">{{ item.content }}</div>
        <div class="mem-preview-foot">
          <span>{{ item.content.length }} 字 · #{{ item.id }}</span>
          <a-link @click="openDetail">查看全文</a-link>
        </div>
      </template>
      <span class="mem-cell-text" @click="openDetail">{{ item.content }}</span>
    </a-popover>
    <span v-else class="mem-cell-text">{{ item.content }}</span>
  </span>

  <a-drawer
    v-model:visible="detailVisible"
    title="记忆详情"
    :width="520"
    :footer="false"
    unmount-on-close
  >
    <div class="mem-detail-meta">
      <a-tag size="small" :color="scopeColor">{{ item.scope }}</a-tag>
      <span class="af-mono">#{{ item.id }}</span>
      <span class="af-muted">重要度 {{ item.importance }}</span>
      <span class="af-muted">命中 {{ item.hit_count }}</span>
      <span class="af-muted">{{ formatFullTime(item.created_at) }}</span>
    </div>
    <pre class="mem-detail-text">{{ item.content }}</pre>
  </a-drawer>
</template>

<style scoped>
.mem-cell {
  display: block;
  width: 100%;
  overflow: hidden;
}

.mem-cell-text {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

.mem-detail-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 12px;
}

.mem-detail-text {
  margin: 0;
  padding: 12px 14px;
  border-radius: 6px;
  background: var(--color-fill-1);
  font-size: 12.5px;
  line-height: 1.85;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text-1);
}
</style>

<style>
/* 浮层被 teleport 到 body，scoped 样式选不到，这里用全局类名兜住 */
.mem-preview {
  max-height: 280px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text-1);
}

.mem-preview-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--color-border-2);
  font-size: 11.5px;
  color: var(--color-text-3);
  white-space: nowrap;
}
</style>
