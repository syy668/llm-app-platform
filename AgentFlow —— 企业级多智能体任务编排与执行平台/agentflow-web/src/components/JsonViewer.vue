<script setup lang="ts">
/**
 * JSON 预览器 —— Trace 面板、子任务入参出参、快照统一复用。
 *
 * 设计要点：
 *  - 后端 payload / values / artifacts 都是**开放结构**，键名不固定，所以只做通用美化，
 *    不假设任何具体字段；
 *  - 大对象默认折叠到 12 行，避免撑爆面板；
 *  - 提供复制，便于把 span payload 贴到日志平台检索。
 */
import { computed, ref } from 'vue'
import { Message } from '@arco-design/web-vue'

const props = withDefaults(
  defineProps<{
    value?: unknown
    /** 渲染高度上限（px） */
    maxHeight?: number
    /** 空值占位文案 */
    emptyText?: string
    /** 是否显示顶部标题栏 */
    showHeader?: boolean
    title?: string
  }>(),
  { maxHeight: 260, emptyText: '暂无数据', showHeader: false, title: 'Payload' },
)

const expanded = ref(false)

const isEmpty = computed(() => {
  const value = props.value
  if (value === null || value === undefined) return true
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value as Record<string, unknown>).length === 0
  if (typeof value === 'string') return value.trim().length === 0
  return false
})

const formatted = computed(() => {
  const value = props.value
  if (typeof value === 'string') {
    // 后端有些字段存的是 JSON 字符串（如 error.message 里嵌 JSON），尝试美化
    try {
      return JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      return value
    }
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
})

/** 折叠态只展示前 12 行 */
const collapsedText = computed(() => {
  const lines = formatted.value.split('\n')
  if (expanded.value || lines.length <= 12) return formatted.value
  return `${lines.slice(0, 12).join('\n')}\n…`
})

const lineCount = computed(() => formatted.value.split('\n').length)

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(formatted.value)
    Message.success('已复制 JSON')
  } catch {
    Message.error('复制失败，请手动选择文本')
  }
}
</script>

<template>
  <div class="af-json">
    <div v-if="showHeader" class="af-json-head">
      <span class="af-muted">{{ title }}</span>
      <a-space :size="4">
        <a-link v-if="lineCount > 12" @click="expanded = !expanded">
          {{ expanded ? '收起' : `展开全部 ${lineCount} 行` }}
        </a-link>
        <a-link @click="copy">复制</a-link>
      </a-space>
    </div>
    <div v-if="isEmpty" class="af-json-empty">{{ emptyText }}</div>
    <pre
      v-else
      class="af-json-body af-scroll"
      :style="{ maxHeight: `${maxHeight}px` }"
    >{{ collapsedText }}</pre>
  </div>
</template>

<style scoped>
.af-json {
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
  background: var(--color-fill-1, #fafbfc);
  overflow: hidden;
}

.af-json-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-border-1, #f2f3f5);
  background: #fff;
}

.af-json-body {
  margin: 0;
  padding: 10px;
  font-family: ui-monospace, SFMono-Regular, Consolas, Menlo, monospace;
  font-size: 12px;
  line-height: 19px;
  color: #1d2129;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.af-json-empty {
  padding: 16px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-3);
}
</style>
