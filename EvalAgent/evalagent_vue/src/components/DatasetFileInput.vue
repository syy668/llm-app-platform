<script setup lang="ts">
/**
 * 数据集文件选择器（创建 / 追加共用）。
 *
 * 抽出来的原因：如果"新建时导入"和"追加样本"各写一份 el-upload + 解析 + 报错，
 * 两份逻辑迟早会分叉（一边支持 JSON 了另一边没有）。这里把解析、校验、展示都收口，
 * 父组件只关心「拿到了哪些 items」。
 *
 * `limit` 是**剩余可加条数**（不是单次上限）：追加场景传 `500 - 当前条数`，
 * 超出直接在解析阶段拦下，不用等后端 409。
 */
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadRawFile } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { parseDatasetFile } from '@/utils/datasetFile'
import type { DatasetFileParsed } from '@/api/types'

const props = withDefaults(
  defineProps<{
    /** 剩余可加条数；默认 500（新建场景） */
    limit?: number
    /** 是否展示「支持的两种格式」折叠示例 */
    showExamples?: boolean
    /** 上传区文案里的文件名占位说明 */
    hint?: string
  }>(),
  { limit: 500, showExamples: false, hint: '' },
)

const emit = defineEmits<{ change: [DatasetFileParsed] }>()

const fileName = ref('')
const parsedRows = ref(0)
const errors = ref<string[]>([])
const warnings = ref<string[]>([])

const SUPPORTED_EXTS = ['.csv', '.json', '.jsonl', '.ndjson']

const hasProblem = computed(() => errors.value.length > 0)
const alertType = computed(() =>
  hasProblem.value ? 'error' : warnings.value.length > 0 ? 'warning' : 'success',
)

interface UploadFileLike {
  raw?: UploadRawFile
}

function handleChange(file: UploadFileLike) {
  if (file.raw) parseFile(file.raw)
}

function parseFile(file: UploadRawFile): void {
  errors.value = []
  warnings.value = []
  parsedRows.value = 0
  fileName.value = ''
  emit('change', { items: [], errors: [], warnings: [], fileName: '' })

  const lower = file.name.toLowerCase()
  const ext = SUPPORTED_EXTS.find((e) => lower.endsWith(e))
  if (!ext) {
    ElMessage.error('仅支持 .csv / .json / .jsonl 文件（UTF-8 编码）')
    return
  }

  const reader = new FileReader()
  reader.onload = () => {
    const result = parseDatasetFile(String(reader.result ?? ''), file.name)
    let items = result.items
    const errs = [...result.errors]

    // 剩余额度前置校验：追加场景下"文件本身合法"不等于"能塞进这个数据集"
    if (items.length > props.limit) {
      errs.push(`本次 ${items.length} 条，但剩余可加 ${props.limit} 条，请拆分后再传`)
      items = []
    }

    fileName.value = file.name
    parsedRows.value = items.length
    errors.value = errs
    warnings.value = result.warnings
    emit('change', { items, errors: errs, warnings: result.warnings, fileName: file.name })

    if (errs.length) ElMessage.error(errs[0])
    else if (result.warnings.length) ElMessage.warning(`解析成功：${items.length} 条（${result.warnings.length} 条提示）`)
    else ElMessage.success(`解析成功：${items.length} 条有效样本`)
  }
  reader.onerror = () => ElMessage.error('文件读取失败')
  reader.readAsText(file, 'utf-8')
}

/** 提交成功后由父组件调用，清空当前文件与解析结果 */
function reset(): void {
  fileName.value = ''
  parsedRows.value = 0
  errors.value = []
  warnings.value = []
  emit('change', { items: [], errors: [], warnings: [], fileName: '' })
}

defineExpose({ reset })
</script>

<template>
  <div>
    <el-upload drag accept=".csv,.json,.jsonl,.ndjson" :auto-upload="false" :show-file-list="false" :on-change="handleChange">
      <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
      <div class="el-upload__text">拖拽 CSV / JSON 文件到此处，或 <em>点击选择</em></div>
      <template #tip>
        <div class="el-upload__tip">
          支持 .csv / .json / .jsonl；UTF-8 编码；question 与 ground_truth 必填；本次最多 {{ limit }} 条
        </div>
      </template>
    </el-upload>

    <div v-if="fileName" class="ea-hint" style="margin-top: 6px">
      当前文件：<span class="ea-mono">{{ fileName }}</span>
    </div>
    <div v-else-if="hint" class="ea-hint" style="margin-top: 6px">{{ hint }}</div>

    <!-- 格式示例：JSON 比 CSV 多三个可选字段，摆出来省一轮试错 -->
    <el-collapse v-if="showExamples" style="margin-top: 10px">
      <el-collapse-item title="支持的两种格式（点击展开示例）" name="fmt">
        <div class="fmt-label">CSV（固定表头，可选 stratum 列）</div>
        <pre class="fmt-block">question,ground_truth,stratum
华东区 Q3 销售额是多少？,1200 万元,easy</pre>
        <div class="fmt-label">JSON（数组，或 {"items": [...]}）</div>
        <pre class="fmt-block">[
  {
    "question": "华东区 Q3 销售额是多少？",
    "ground_truth": "1200 万元",
    "stratum": "easy",
    "difficulty": 2,
    "reference_contexts": ["2026 年 Q3 华东区销售额为 1200 万元"]
  }
]</pre>
        <div class="ea-hint">
          字段别名也认：<code>query/问题</code> 等价 question，<code>reference_answer/answer/标准答案</code>
          等价 ground_truth；可选 <code>reference_contexts</code>、<code>stratum</code>、
          <code>difficulty</code>(1~5)、<code>item_type</code>。JSONL（一行一个对象）同样支持。
        </div>
      </el-collapse-item>
    </el-collapse>

    <el-alert
      v-if="parsedRows > 0 || errors.length > 0 || warnings.length > 0"
      style="margin-top: 10px"
      :type="alertType"
      :closable="false"
      show-icon
    >
      <p>已解析 {{ parsedRows }} 条有效样本</p>
      <p v-for="(err, i) in errors.slice(0, 5)" :key="`e${i}`" class="parse-error">{{ err }}</p>
      <p v-if="errors.length > 5">…… 共 {{ errors.length }} 条错误</p>
      <p v-for="(w, i) in warnings.slice(0, 3)" :key="`w${i}`" class="parse-warning">{{ w }}</p>
      <p v-if="warnings.length > 3">…… 共 {{ warnings.length }} 条提示</p>
    </el-alert>
  </div>
</template>

<style scoped>
.parse-error {
  font-size: 12px;
  color: var(--ea-text-2);
}

.parse-warning {
  font-size: 12px;
  color: var(--ea-warning);
}

.fmt-label {
  font-size: 12px;
  color: var(--ea-text-3);
  margin: 8px 0 4px;
}

.fmt-block {
  margin: 0;
  padding: 10px 12px;
  background: #f7f8fa;
  border: 1px solid var(--ea-border-light);
  border-radius: 6px;
  font-family: SFMono-Regular, Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
  color: var(--ea-text-2);
  overflow-x: auto;
  white-space: pre;
}
</style>
