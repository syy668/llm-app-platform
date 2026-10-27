<script setup lang="ts">
/**
 * 低分 BadCase 筛选组件。
 * 纯前端过滤：输入阈值后，由父组件对已加载的得分明细做
 * faithfulness < 阈值 或 context_recall < 阈值 的筛选
 * （上游无 /eval/task/{id}/badcase 接口，不新增后端）。
 */
export interface BadCaseThreshold {
  faithfulness: number
  contextRecall: number
}

const model = defineModel<BadCaseThreshold>({ required: true })

const props = defineProps<{
  /** 是否启用筛选 */
  enabled: boolean
}>()

const emit = defineEmits<{ 'update:enabled': [value: boolean] }>()
</script>

<template>
  <div class="badcase-filter">
    <el-switch
      :model-value="props.enabled"
      active-text="BadCase 筛选"
      @update:model-value="(v: string | number | boolean) => emit('update:enabled', Boolean(v))"
    />
    <template v-if="props.enabled">
      <div class="threshold-item">
        <span class="label">faithfulness &lt;</span>
        <el-input-number v-model="model.faithfulness" :min="0" :max="1" :step="0.05" :precision="2" size="small" />
      </div>
      <div class="threshold-item">
        <span class="label">context_recall &lt;</span>
        <el-input-number v-model="model.contextRecall" :min="0" :max="1" :step="0.05" :precision="2" size="small" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.badcase-filter {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.threshold-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.label {
  font-size: 12px;
  color: var(--ea-text-3);
  font-family: 'SFMono-Regular', Consolas, monospace;
}
</style>
