<script setup lang="ts">
/**
 * 数据集管理页：
 * - 文件上传（CSV / JSON / JSONL 前端解析校验 → 创建数据集 → 批量导入样本，仅管理员）
 * - 追加样本（向已有数据集续加，seq 接续、总量受 500 上限约束）
 * - 数据集列表（登录用户可浏览）
 * - 样本分页预览（对话框）
 * - 删除数据集（仅管理员，级联删除样本；已生成的评测运行记录保留）
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import EmptyState from '@/components/EmptyState.vue'
import DatasetFileInput from '@/components/DatasetFileInput.vue'
import { createDataset, addDatasetItems, deleteDataset, listDatasetItems, listDatasets } from '@/api/dataset'
import { listKbs } from '@/api/kb'
import { useAuthStore } from '@/store/auth'
import { MAX_DATASET_ROWS } from '@/utils/datasetFile'
import type { Dataset, DatasetFileParsed, DatasetItem, ItemInput, KB } from '@/api/types'

const { isAdmin } = useAuthStore()

const loading = ref(false)
const datasets = ref<Dataset[]>([])
const kbs = ref<KB[]>([])

// ---------- 上传表单 ----------
const uploadForm = reactive({
  kbId: undefined as number | undefined,
  name: '',
  description: '',
})
const uploadRef = ref()
const createPicker = ref<InstanceType<typeof DatasetFileInput> | null>(null)
const uploadRules = {
  kbId: [{ required: true, message: '请选择知识库', trigger: 'change' }],
  name: [{ required: true, message: '请输入数据集名称', trigger: 'blur' }],
}
const createItems = ref<ItemInput[]>([])
const createHasError = ref(false)
const submitting = ref(false)

function onCreateFileParsed(payload: DatasetFileParsed) {
  createItems.value = payload.items
  createHasError.value = payload.errors.length > 0
}

async function submitUpload() {
  await uploadRef.value?.validate()
  if (createItems.value.length === 0 || createHasError.value) {
    ElMessage.warning('请先选择并通过校验一个数据文件（CSV / JSON / JSONL）')
    return
  }
  submitting.value = true
  try {
    // 步骤1：创建数据集
    const ds = await createDataset({
      kb_id: uploadForm.kbId!,
      name: uploadForm.name.trim(),
      description: uploadForm.description.trim() || null,
    })
    // 步骤2：批量导入样本（≤500，单次请求即可）
    await addDatasetItems(ds.id, createItems.value)
    ElMessage.success(`数据集「${ds.name}」创建成功，已导入 ${createItems.value.length} 条样本`)
    // 重置表单
    uploadRef.value?.resetFields()
    createPicker.value?.reset()
    createItems.value = []
    createHasError.value = false
    await loadDatasets()
  } finally {
    submitting.value = false
  }
}

// ---------- 追加样本（仅管理员） ----------
const appendVisible = ref(false)
const appendTarget = ref<Dataset | null>(null)
const appendPicker = ref<InstanceType<typeof DatasetFileInput> | null>(null)
const appendItems = ref<ItemInput[]>([])
const appendHasError = ref(false)
const appending = ref(false)

/** 该数据集还能加多少条（总量闸在后端也有一道，这里只做前置提示） */
const appendRemaining = computed(() =>
  appendTarget.value ? Math.max(0, MAX_DATASET_ROWS - appendTarget.value.item_count) : 0,
)
const appendFull = computed(() => appendRemaining.value === 0)

function openAppend(row: Dataset) {
  appendTarget.value = row
  appendItems.value = []
  appendHasError.value = false
  appendVisible.value = true
}

function onAppendFileParsed(payload: DatasetFileParsed) {
  appendItems.value = payload.items
  appendHasError.value = payload.errors.length > 0
}

async function submitAppend() {
  const target = appendTarget.value
  if (!target) return
  if (appendItems.value.length === 0 || appendHasError.value) {
    ElMessage.warning('请先选择并通过校验一个数据文件')
    return
  }
  appending.value = true
  try {
    const res = await addDatasetItems(target.id, appendItems.value)
    ElMessage.success(
      `已向「${target.name}」追加 ${res.imported} 条，当前共 ${res.item_count} 条`,
    )
    appendVisible.value = false
    appendPicker.value?.reset()
    appendItems.value = []
    await loadDatasets()
  } finally {
    appending.value = false
  }
}

// ---------- 列表 ----------
async function loadDatasets() {
  loading.value = true
  try {
    const res = await listDatasets(1, 100)
    datasets.value = res.items
  } finally {
    loading.value = false
  }
}

// ---------- 样本预览 ----------
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewDataset = ref<Dataset | null>(null)
const previewItems = ref<DatasetItem[]>([])
const previewTotal = ref(0)
const previewPage = ref(1)
const previewPageSize = 10

async function openPreview(row: Dataset) {
  previewDataset.value = row
  previewPage.value = 1
  previewVisible.value = true
  await loadPreviewItems()
}

async function loadPreviewItems() {
  if (!previewDataset.value) return
  previewLoading.value = true
  try {
    const res = await listDatasetItems(previewDataset.value.id, previewPage.value, previewPageSize)
    previewItems.value = res.items
    previewTotal.value = res.total
  } finally {
    previewLoading.value = false
  }
}

function onPreviewPageChange(page: number) {
  previewPage.value = page
  void loadPreviewItems()
}

// ---------- 删除数据集（仅管理员） ----------
async function handleDelete(row: Dataset) {
  try {
    await ElMessageBox.confirm(
      `确定删除数据集「${row.name}」？将级联删除其 ${row.item_count} 条样本；已生成的评测任务记录会保留。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 取消删除：ElMessageBox 以 reject 结束，不接住会变成 Unhandled Rejection
  }
  const res = await deleteDataset(row.id)
  ElMessage.success(`已删除数据集 #${res.deleted_dataset_id} 及 ${res.deleted_items} 条样本`)
  await loadDatasets()
}

onMounted(async () => {
  await loadDatasets()
  try {
    const res = await listKbs(1, 100)
    kbs.value = res.items
  } catch {
    // 知识库加载失败不阻塞页面，仅上传时受限
  }
})
</script>

<template>
  <div class="ea-content">
    <div class="ea-page-header">
      <div class="ea-page-title">数据集管理</div>
      <div class="ea-page-sub">
        上传 CSV / JSON 创建评测数据集；单数据集最多 {{ MAX_DATASET_ROWS }} 条样本
      </div>
    </div>

    <!-- 文件上传（仅管理员可见） -->
    <div v-if="isAdmin" class="ea-card">
      <div class="ea-card-header">
        <div class="ea-card-title">上传评测数据集</div>
      </div>
      <div class="ea-card-body">
        <el-form ref="uploadRef" :model="uploadForm" :rules="uploadRules" label-width="100px" style="max-width: 640px">
          <el-form-item label="知识库" prop="kbId">
            <el-select v-model="uploadForm.kbId" placeholder="选择数据集归属的知识库" style="width: 100%">
              <el-option v-for="kb in kbs" :key="kb.id" :label="`#${kb.id} ${kb.name}`" :value="kb.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="数据集名称" prop="name">
            <el-input v-model="uploadForm.name" placeholder="如：客服知识库评测集 v1" maxlength="128" />
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="uploadForm.description" placeholder="可选" maxlength="512" />
          </el-form-item>
          <el-form-item label="数据文件" required>
            <div style="width: 100%">
              <DatasetFileInput
                ref="createPicker"
                :limit="MAX_DATASET_ROWS"
                show-examples
                @change="onCreateFileParsed"
              />
            </div>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="submitting" @click="submitUpload">创建数据集并导入样本</el-button>
          </el-form-item>
        </el-form>
      </div>
    </div>

    <!-- 数据集列表 -->
    <div class="ea-card">
      <div class="ea-card-header">
        <div class="ea-card-title">数据集列表</div>
      </div>
      <div class="ea-card-body" style="padding-top: 8px">
        <el-table v-loading="loading" :data="datasets" stripe>
          <el-table-column prop="id" label="ID" width="64" />
          <el-table-column prop="name" label="数据集名称" min-width="170" show-overflow-tooltip />
          <el-table-column prop="kb_id" label="知识库 ID" width="90" />
          <el-table-column prop="item_count" label="样本数量" width="110">
            <template #default="{ row }">
              <span class="ea-mono">{{ row.item_count }}</span>
              <span v-if="row.item_count >= MAX_DATASET_ROWS" class="ea-hint" style="margin-left: 4px">已满</span>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ row.description || '—' }}</template>
          </el-table-column>
          <el-table-column prop="created_by" label="创建人" width="88">
            <template #default="{ row }">{{ row.created_by ?? '—' }}</template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" width="168">
            <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column label="操作" width="240" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openPreview(row)">预览样本</el-button>
              <el-tooltip
                v-if="isAdmin"
                :disabled="row.item_count < MAX_DATASET_ROWS"
                :content="`已达单数据集上限 ${MAX_DATASET_ROWS} 条，不能再追加`"
                placement="top"
              >
                <span>
                  <el-button
                    link
                    type="primary"
                    :disabled="row.item_count >= MAX_DATASET_ROWS"
                    @click="openAppend(row)"
                  >
                    追加样本
                  </el-button>
                </span>
              </el-tooltip>
              <el-button v-if="isAdmin" link type="danger" @click="handleDelete(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState description="暂无数据集，请先上传 CSV / JSON 创建" />
          </template>
        </el-table>
      </div>
    </div>

    <!-- 追加样本（仅管理员）：seq 接续、总量受 500 上限约束 -->
    <el-dialog v-model="appendVisible" :title="`追加样本：${appendTarget?.name ?? ''}`" width="680px">
      <div v-if="appendTarget">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 14px">
          <template #title>
            当前 {{ appendTarget.item_count }} 条 / 上限 {{ MAX_DATASET_ROWS }} 条 · 本次最多可加
            <strong>{{ appendRemaining }}</strong> 条
          </template>
          <div style="font-size: 12px; line-height: 1.7; margin-top: 2px">
            新样本的序号接着现有最大序号往下排（不重排已有样本）——历史评测明细里的序号是快照，
            重排会让 A/B 对齐错位。
          </div>
        </el-alert>

        <el-alert
          v-if="appendFull"
          type="warning"
          :closable="false"
          show-icon
          title="该数据集已达上限，不能再追加。如需更多样本请新建数据集。"
        />

        <div v-else>
          <DatasetFileInput
            ref="appendPicker"
            :limit="appendRemaining"
            :hint="`只能再加 ${appendRemaining} 条；超出会被直接拦下`"
            @change="onAppendFileParsed"
          />
        </div>
      </div>
      <template #footer>
        <el-button @click="appendVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="appending"
          :disabled="appendFull || appendItems.length === 0 || appendHasError"
          @click="submitAppend"
        >
          追加 {{ appendItems.length || 0 }} 条
        </el-button>
      </template>
    </el-dialog>

    <!-- 样本分页预览 -->
    <el-dialog
      v-model="previewVisible"
      :title="`样本预览：${previewDataset?.name ?? ''}`"
      width="860px"
    >
      <el-table v-loading="previewLoading" :data="previewItems" stripe max-height="480">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="question" label="question" min-width="260" show-overflow-tooltip />
        <el-table-column prop="reference_answer" label="ground_truth" min-width="300" show-overflow-tooltip />
        <template #empty>
          <EmptyState description="该数据集暂无样本" />
        </template>
      </el-table>
      <div style="display: flex; justify-content: flex-end; margin-top: 12px">
        <el-pagination
          layout="total, prev, pager, next"
          :total="previewTotal"
          :page-size="previewPageSize"
          :current-page="previewPage"
          @current-change="onPreviewPageChange"
        />
      </div>
    </el-dialog>
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
