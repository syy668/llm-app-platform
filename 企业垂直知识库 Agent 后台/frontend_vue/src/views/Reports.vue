<script setup lang="ts">
// 报表中心：会话内生成的 Markdown 报表列表 / 预览 / 下载。
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Download, View } from '@element-plus/icons-vue'
import { deleteReport, downloadReport, getReport, listReports } from '@/api/features'
import { renderMarkdown } from '@/utils/markdown'
import type { ReportItem } from '@/types'
import EmptyState from '@/components/EmptyState.vue'
import PagePagination from '@/components/PagePagination.vue'
import SkeletonLoading from '@/components/SkeletonLoading.vue'

const reports = ref<ReportItem[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const loading = ref(false)

// 预览弹窗
const previewVisible = ref(false)
const previewTitle = ref('')
const previewContent = ref('')
const previewLoading = ref(false)

async function loadReports() {
  loading.value = true
  try {
    const res = await listReports({ page: page.value, size: size.value })
    reports.value = res.items
    total.value = res.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

async function openPreview(row: ReportItem) {
  previewTitle.value = row.title
  previewContent.value = ''
  previewVisible.value = true
  previewLoading.value = true
  try {
    const res = await getReport(row.id)
    previewContent.value = res.content
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    previewLoading.value = false
  }
}

async function onDownload(row: ReportItem) {
  try {
    await downloadReport(row.id)
    ElMessage.success('已开始下载')
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function onDelete(row: ReportItem) {
  try {
    await ElMessageBox.confirm(
      `确定删除报表「${row.title}」吗？该报表将从报表中心与其所属会话中一并移除，且不可恢复。`,
      '删除报表确认',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return // 用户取消删除
  }
  try {
    await deleteReport(row.id)
    ElMessage.success('报表已删除')
    await loadReports()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

onMounted(loadReports)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">报表中心</div>
        <div class="page-desc">智能对话中生成的全部 Markdown 报表，可在线预览或下载 .md 文件</div>
      </div>
    </div>

    <div class="card">
      <SkeletonLoading v-if="loading" :rows="6" />
      <template v-else>
        <el-table :data="reports" style="width: 100%">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="title" label="报表标题" min-width="200" show-overflow-tooltip />
          <el-table-column prop="preview" label="内容预览" min-width="280" show-overflow-tooltip />
          <el-table-column label="生成时间" width="170">
            <template #default="{ row }">{{ (row.created_at || '').slice(0, 19).replace('T', ' ') }}</template>
          </el-table-column>
          <el-table-column label="操作" width="255" fixed="right" class-name="ops-col">
            <template #default="{ row }">
              <el-button text type="primary" size="small" :icon="View" @click="openPreview(row)">预览</el-button>
              <el-button text type="primary" size="small" :icon="Download" @click="onDownload(row)">下载</el-button>
              <el-button text type="danger" size="small" :icon="Delete" @click="onDelete(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState title="暂无报表" desc="在智能对话中提问「生成设备汇总报表」即可产生报表" icon="📊" />
          </template>
        </el-table>
        <PagePagination
          :total="total"
          :page="page"
          :size="size"
          @update:page="(p: number) => { page = p; loadReports() }"
          @update:size="(s: number) => { size = s; page = 1; loadReports() }"
        />
      </template>
    </div>

    <!-- 报表预览 -->
    <el-dialog v-model="previewVisible" :title="previewTitle" width="720px" top="6vh">
      <div v-if="previewLoading" class="small muted">加载中…</div>
      <div v-else class="report-body" v-html="renderMarkdown(previewContent)"></div>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.report-body { max-height: 64vh; overflow-y: auto; padding: 0 8px; }
:deep(.ops-col .cell) { white-space: nowrap; }
</style>
