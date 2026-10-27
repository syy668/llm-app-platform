<script setup lang="ts">
// 知识库管理页：知识库列表 / 文档上传解析 / 分块参数 / 文档分页表格。
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadUserFile } from 'element-plus'
import { Refresh, Upload } from '@element-plus/icons-vue'
import {
  createKb,
  deleteDocument,
  deleteKb,
  downloadDocument,
  kbStats,
  listChunks,
  listDocuments,
  listKbs,
  reprocessDocument,
  updateKb,
  uploadDocument,
} from '@/api/kb'
import { useUserStore } from '@/store/useUserStore'
import { grantPermission, listPermissions, revokePermission } from '@/api/features'
import { listUsers } from '@/api/user'
import type { Chunk, DocItem, KBPermissionItem, KBStats, KnowledgeBase, User } from '@/types'
import EmptyState from '@/components/EmptyState.vue'
import PagePagination from '@/components/PagePagination.vue'

const kbs = ref<KnowledgeBase[]>([])
const selectedKbId = ref<number | null>(null)
const selectedKb = ref<KnowledgeBase | null>(null)
const stats = ref<KBStats | null>(null)
const activeTab = ref('doc')

// 文档列表分页
const docs = ref<DocItem[]>([])
const docTotal = ref(0)
const docPage = ref(1)
const docSize = ref(8)
const docLoading = ref(false)

// 上传（支持拖拽与多选）
const fileList = ref<UploadUserFile[]>([])
const uploading = ref(false)

// 分块弹窗
const chunkDialogVisible = ref(false)
const chunks = ref<Chunk[]>([])
const chunkDocTitle = ref('')

// 新建知识库弹窗
const createDialogVisible = ref(false)
const createForm = reactive({ name: '', description: '', chunk_size: 500, chunk_overlap: 50, top_k: 5, rerank_threshold: 0 })
const creating = ref(false)

// 编辑表单
const editForm = reactive({ name: '', description: '', chunk_size: 500, chunk_overlap: 50, top_k: 5, rerank_threshold: 0, status: 'active' })

function statusClass(s: string) {
  if (s === 'done') return 'tag-green'
  if (s === 'failed') return 'tag-red'
  if (s === 'pending' || s === 'parsing') return 'tag-orange'
  return 'tag-gray'
}
function statusLabel(s: string) {
  const map: Record<string, string> = { done: '已完成', failed: '失败', pending: '待解析', parsing: '解析中' }
  return map[s] || s
}

async function loadKbs() {
  const res = await listKbs({ page: 1, size: 100 })
  kbs.value = res.items
  if (selectedKbId.value === null && kbs.value.length) selectedKbId.value = kbs.value[0].id
  selectedKb.value = kbs.value.find((k) => k.id === selectedKbId.value) || null
  if (selectedKb.value) await loadDetail(selectedKb.value.id)
}

async function loadDetail(kbId: number) {
  selectedKb.value = kbs.value.find((k) => k.id === kbId) || null
  stats.value = await kbStats(kbId).catch(() => null)
  docPage.value = 1
  await loadDocs()
}

async function loadDocs() {
  if (!selectedKbId.value) return
  docLoading.value = true
  try {
    const res = await listDocuments(selectedKbId.value, { page: docPage.value, size: docSize.value })
    docs.value = res.items
    docTotal.value = res.total
  } finally {
    docLoading.value = false
  }
}

function selectKb(kbId: number) {
  if (kbId === selectedKbId.value) return
  selectedKbId.value = kbId
  loadDetail(kbId)
  loadPerms(kbId)
}

// ---------------- 授权管理（仅管理员） ----------------
const perms = ref<KBPermissionItem[]>([])
const allUsers = ref<User[]>([])
const permUserId = ref<number | null>(null)
const permLevel = ref<'read' | 'write'>('read')

async function loadPerms(kbId: number) {
  if (!store.isAdmin || !kbId) return
  try {
    perms.value = await listPermissions(kbId)
  } catch {
    perms.value = []
  }
}

async function loadAllUsers() {
  if (!store.isAdmin) return
  try {
    allUsers.value = (await listUsers({ page: 1, size: 100 })).items
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function doGrant() {
  if (!selectedKbId.value || !permUserId.value) return
  try {
    await grantPermission(selectedKbId.value, permUserId.value, permLevel.value)
    ElMessage.success('已授权')
    permUserId.value = null
    await loadPerms(selectedKbId.value)
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function doRevoke(row: KBPermissionItem) {
  if (!selectedKbId.value) return
  try {
    await revokePermission(selectedKbId.value, row.user_id)
    ElMessage.success('已撤销授权')
    await loadPerms(selectedKbId.value)
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function doUpload() {
  if (!selectedKbId.value || !fileList.value.length) return
  uploading.value = true
  try {
    const okNames: string[] = []
    for (const f of fileList.value) {
      if (!f.raw) continue
      const doc = await uploadDocument(selectedKbId.value, f.raw)
      okNames.push(doc.title)
    }
    if (okNames.length) {
      ElMessage.success(`已上传 ${okNames.length} 个文件：${okNames.join('、')}`)
    }
    fileList.value = []
    await Promise.all([loadDetail(selectedKbId.value), loadKbs()])
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    uploading.value = false
  }
}

async function openChunks(doc: DocItem) {
  chunkDocTitle.value = doc.title
  chunkDialogVisible.value = true
  chunks.value = []
  try {
    const res = await listChunks(doc.id, { page: 1, size: 20 })
    chunks.value = res.items
  } catch {
    /* ignore */
  }
}

async function onDownloadDoc(doc: DocItem) {
  try {
    await downloadDocument(doc.id, doc.title)
    ElMessage.success('已开始下载')
  } catch {
    /* 错误已由拦截器提示（如源文件丢失） */
  }
}

async function onReprocessDoc(doc: DocItem) {
  try {
    await reprocessDocument(doc.id)
    ElMessage.success(`已重新解析：${doc.title}`)
    await Promise.all([loadDocs(), loadKbs()])
  } catch {
    /* 错误已由拦截器提示（如源文件丢失 / 解析失败原因） */
    await loadDocs()
  }
}

async function onDeleteDoc(doc: DocItem) {
  try {
    await ElMessageBox.confirm(
      `确定删除文档「${doc.title}」吗？其全部分块与向量数据将一并清除，且不可恢复。`,
      '删除文档确认',
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
  await deleteDocument(doc.id)
  ElMessage.success('文档已删除')
  await Promise.all([loadDocs(), loadKbs()])
}

async function doCreateKb() {
  if (!createForm.name.trim()) {
    ElMessage.warning('请填写知识库名称')
    return
  }
  creating.value = true
  try {
    await createKb({ ...createForm, name: createForm.name.trim() })
    ElMessage.success('创建成功')
    createDialogVisible.value = false
    await loadKbs()
  } catch {
    /* ignore */
  } finally {
    creating.value = false
  }
}

function syncEditForm() {
  if (!selectedKb.value) return
  editForm.name = selectedKb.value.name
  editForm.description = selectedKb.value.description || ''
  editForm.chunk_size = selectedKb.value.chunk_size
  editForm.chunk_overlap = selectedKb.value.chunk_overlap
  editForm.top_k = selectedKb.value.top_k
  editForm.rerank_threshold = selectedKb.value.rerank_threshold ?? 0
  editForm.status = selectedKb.value.status
}

async function doSaveConfig() {
  if (!selectedKbId.value) return
  await updateKb(selectedKbId.value, { ...editForm })
  ElMessage.success('已更新')
  await loadKbs()
}

function onTabChange(t: string | number) {
  if (t === 'cfg') syncEditForm()
}

async function doDeleteKb() {  if (!selectedKbId.value) return
  const kbName = selectedKb.value?.name || `#${selectedKbId.value}`
  try {
    await ElMessageBox.confirm(
      `删除知识库「${kbName}」将级联清除其下全部文档、分块与向量数据，且不可恢复。确定要删除吗？`,
      '删除知识库确认',
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
  await deleteKb(selectedKbId.value)
  ElMessage.success('知识库已删除')
  selectedKbId.value = null
  selectedKb.value = null
  stats.value = null
  await loadKbs()
}

const store = useUserStore()

onMounted(() => {
  loadKbs()
  loadAllUsers()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">知识库管理</div>
        <div class="page-desc">管理知识库、上传解析文档、配置分块参数与文档列表</div>
      </div>
      <div class="page-actions">
        <el-button @click="loadKbs"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-button type="primary" @click="createDialogVisible = true"><el-icon><Upload /></el-icon>新建知识库</el-button>
      </div>
    </div>

    <div class="kb-layout">
      <!-- 左侧知识库列表 -->
      <aside class="card kb-side">
        <div class="card-header"><span class="card-title">知识库列表</span></div>
        <div class="kb-list">
          <EmptyState v-if="!kbs.length" title="暂无知识库" desc="点击右上角新建知识库" icon="📚" />
          <div
            v-for="kb in kbs"
            :key="kb.id"
            class="kb-item"
            :class="{ active: kb.id === selectedKbId }"
            @click="selectKb(kb.id)"
          >
            <div class="kb-item-name">
              <span class="kb-name-group">
                {{ kb.name }}
                <span v-if="kb.is_shared" class="ui-tag tag-blue">共享</span>
                <span v-else class="ui-tag tag-gray">我的</span>
              </span>
              <span v-if="kb.status === 'active'" class="ui-tag tag-green">active</span>
            </div>
            <div class="kb-item-meta">文档 {{ kb.doc_count }} · 分块大小 {{ kb.chunk_size }}</div>
          </div>
        </div>
      </aside>

      <!-- 右侧详情 -->
      <div class="kb-main">
        <EmptyState v-if="!selectedKb" title="请选择知识库" desc="在左侧选择一个知识库后查看详情" icon="📚" />

        <template v-else>
          <div class="kb-head">
            <div>
              <div class="strong" style="font-size: 16px">{{ selectedKb.name }}</div>
              <div class="small muted">#{{ selectedKb.id }} · {{ selectedKb.description || '暂无描述' }}</div>
            </div>
            <div class="mini-stats">
              <div class="stat-card"><div class="stat-label">文档数</div><div class="stat-value">{{ selectedKb.doc_count }}</div></div>
              <div class="stat-card"><div class="stat-label">分块数</div><div class="stat-value">{{ stats?.chunk_count ?? 0 }}</div></div>
              <div class="stat-card"><div class="stat-label">Token 总量</div><div class="stat-value">{{ stats?.token_sum ?? 0 }}</div></div>
            </div>
          </div>

          <el-tabs v-model="activeTab" @tab-change="onTabChange">
            <!-- 文档管理 -->
            <el-tab-pane label="文档管理" name="doc">
              <div class="card">
                <div class="card-header">
                  <span class="card-title">文档上传</span>
                  <span class="small muted">PDF / Word / Excel / txt / md</span>
                </div>
                <div class="card-body">
                  <el-upload
                    v-model:file-list="fileList"
                    class="doc-uploader"
                    drag
                    multiple
                    :auto-upload="false"
                    accept=".pdf,.docx,.xlsx,.txt,.md"
                  >
                    <el-icon class="doc-uploader-icon"><Upload /></el-icon>
                    <div class="el-upload__text">将本地文件拖到此处，或 <em>点击选择</em></div>
                    <template #tip>
                      <div class="el-upload__tip">支持 PDF / Word / Excel / txt / md，可一次拖入多个文件</div>
                    </template>
                  </el-upload>
                  <el-button type="primary" style="margin-top: 12px" :loading="uploading" :disabled="!fileList.length" @click="doUpload">
                    上传并解析
                  </el-button>
                </div>
              </div>

              <div class="card" style="margin-top: 16px">
                <div class="card-header"><span class="card-title">文档列表</span></div>
                <el-table v-loading="docLoading" :data="docs" style="width: 100%">
                  <el-table-column prop="title" label="文档名称" min-width="180" show-overflow-tooltip />
                  <el-table-column prop="file_type" label="类型" width="80" />
                  <el-table-column label="大小" width="100">
                    <template #default="{ row }">{{ (row.file_size / 1024).toFixed(1) }} KB</template>
                  </el-table-column>
                  <el-table-column label="解析状态" width="110">
                    <template #default="{ row }">
                      <span class="ui-tag" :class="statusClass(row.parse_status)">{{ statusLabel(row.parse_status) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="chunk_count" label="分块数" width="90" />
                  <el-table-column label="上传时间" width="175" class-name="time-col">
                    <template #default="{ row }">{{ (row.created_at || '').slice(0, 19).replace('T', ' ') }}</template>
                  </el-table-column>
                  <el-table-column label="操作" width="200" fixed="right">
                    <template #default="{ row }">
                      <el-button link type="primary" size="small" @click="openChunks(row)">分块</el-button>
                      <el-button link type="primary" size="small" @click="onDownloadDoc(row)">下载</el-button>
                      <el-button v-if="row.parse_status === 'failed'" link type="warning" size="small" @click="onReprocessDoc(row)">重新解析</el-button>
                      <el-button link type="danger" size="small" @click="onDeleteDoc(row)">删除</el-button>
                    </template>
                  </el-table-column>
                  <template #empty><EmptyState title="该知识库暂无文档" icon="📄" /></template>
                </el-table>
                <PagePagination
                  :total="docTotal"
                  :page="docPage"
                  :size="docSize"
                  @update:page="(p) => { docPage = p; loadDocs() }"
                  @update:size="(s) => { docSize = s; docPage = 1; loadDocs() }"
                />
              </div>
            </el-tab-pane>

            <!-- 参数配置 -->
            <el-tab-pane label="参数配置" name="cfg">
              <div class="card">
                <div class="card-body">
                  <el-form label-position="top">
                    <el-row :gutter="16">
                      <el-col :span="12"><el-form-item label="名称"><el-input v-model="editForm.name" /></el-form-item></el-col>
                      <el-col :span="12"><el-form-item label="描述"><el-input v-model="editForm.description" /></el-form-item></el-col>
                      <el-col :span="8"><el-form-item label="分块大小"><el-input-number v-model="editForm.chunk_size" :min="100" :max="4000" /></el-form-item></el-col>
                      <el-col :span="8"><el-form-item label="分块重叠"><el-input-number v-model="editForm.chunk_overlap" :min="0" :max="1000" /></el-form-item></el-col>
                      <el-col :span="8"><el-form-item label="top_k"><el-input-number v-model="editForm.top_k" :min="1" :max="50" /></el-form-item></el-col>
                      <el-col :span="8">
                        <el-form-item label="重排序阈值">
                          <el-input-number v-model="editForm.rerank_threshold" :min="0" :max="1" :step="0.05" />
                          <div class="small muted" style="margin-top: 4px">0 = 关闭；&gt;0 时按语义相似度重排并过滤低于阈值的分块（0.3~0.6 建议）</div>
                        </el-form-item>
                      </el-col>
                    </el-row>
                    <el-button type="primary" @click="doSaveConfig">保存配置</el-button>
                  </el-form>
                </div>
              </div>
            </el-tab-pane>

            <!-- 授权管理（仅管理员） -->
            <el-tab-pane v-if="store.isAdmin" label="授权管理" name="perm">
              <div class="card">
                <div class="card-body">
                  <el-alert
                    type="info"
                    :closable="false"
                    show-icon
                    title="read = 可查看与问答；write = 可上传与管理文档。管理员与创建者天然拥有全部权限，无需授权。"
                    style="margin-bottom: 14px"
                  />
                  <div class="row" style="gap: 10px; margin-bottom: 14px">
                    <el-select v-model="permUserId" filterable placeholder="选择用户" style="width: 220px">
                      <el-option
                        v-for="u in allUsers"
                        :key="u.id"
                        :label="`#${u.id} ${u.display_name || u.username}`"
                        :value="u.id"
                        :disabled="u.id === selectedKb?.owner_id"
                      />
                    </el-select>
                    <el-select v-model="permLevel" style="width: 140px">
                      <el-option label="read（只读）" value="read" />
                      <el-option label="write（可管理）" value="write" />
                    </el-select>
                    <el-button type="primary" :disabled="!permUserId" @click="doGrant">授权</el-button>
                  </div>
                  <el-table :data="perms" style="width: 100%">
                    <el-table-column prop="user_id" label="用户ID" width="90" />
                    <el-table-column label="用户名" min-width="120">
                      <template #default="{ row }">{{ row.display_name || row.username }}</template>
                    </el-table-column>
                    <el-table-column label="权限" width="140">
                      <template #default="{ row }">
                        <span class="ui-tag" :class="row.permission === 'write' ? 'tag-blue' : 'tag-gray'">
                          {{ row.permission === 'write' ? 'write 可管理' : 'read 只读' }}
                        </span>
                      </template>
                    </el-table-column>
                    <el-table-column label="授权时间" width="170">
                      <template #default="{ row }">{{ (row.created_at || '').slice(0, 19).replace('T', ' ') }}</template>
                    </el-table-column>
                    <el-table-column label="操作" width="90">
                      <template #default="{ row }">
                        <el-button link type="danger" size="small" @click="doRevoke(row)">撤销</el-button>
                      </template>
                    </el-table-column>
                    <template #empty><EmptyState title="暂无授权用户" icon="🔑" /></template>
                  </el-table>
                </div>
              </div>
            </el-tab-pane>

            <!-- 删除 -->
            <el-tab-pane label="删除知识库" name="del">
              <div class="card">
                <div class="card-body">
                  <el-alert type="warning" :closable="false" show-icon title="删除知识库会级联清理文档、分块与向量数据，不可恢复。" />
                  <el-button type="danger" style="margin-top: 16px" @click="doDeleteKb">删除知识库</el-button>
                </div>
              </div>
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </div>

    <!-- 分块弹窗 -->
    <el-dialog v-model="chunkDialogVisible" :title="`分块列表 · ${chunkDocTitle}`" width="720px">
      <el-collapse v-if="chunks.length">
        <el-collapse-item v-for="ch in chunks" :key="ch.id" :title="`分块 #${ch.chunk_index}（${ch.token_count} tokens）`">
          <pre style="white-space: pre-wrap; font-family: inherit; font-size: 13px">{{ ch.content }}</pre>
        </el-collapse-item>
      </el-collapse>
      <EmptyState v-else title="暂无分块" icon="📄" />
    </el-dialog>

    <!-- 新建知识库弹窗 -->
    <el-dialog v-model="createDialogVisible" title="新建知识库" width="520px">
      <el-form label-position="top">
        <el-form-item label="名称 *"><el-input v-model="createForm.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="createForm.description" /></el-form-item>
        <el-row :gutter="16">
          <el-col :span="12"><el-form-item label="分块大小"><el-input-number v-model="createForm.chunk_size" :min="100" :max="4000" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="分块重叠"><el-input-number v-model="createForm.chunk_overlap" :min="0" :max="1000" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="top_k"><el-input-number v-model="createForm.top_k" :min="1" :max="50" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="重排阈值"><el-input-number v-model="createForm.rerank_threshold" :min="0" :max="1" :step="0.05" /></el-form-item></el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="doCreateKb">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kb-layout { display: grid; grid-template-columns: 300px 1fr; gap: 16px; align-items: start; }
.doc-uploader :deep(.el-upload-dragger) { padding: 24px 16px; width: 100%; }
.doc-uploader-icon { font-size: 36px; color: var(--text-3, #909399); margin-bottom: 8px; }
:deep(.time-col .cell) { white-space: nowrap; }
.kb-side { position: sticky; top: 0; }
.kb-list { display: flex; flex-direction: column; gap: 8px; padding: 12px; }
.kb-item { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 12px 14px; cursor: pointer; transition: 0.14s; }
.kb-item:hover { border-color: #b8d0ff; }
.kb-item.active { border-color: var(--primary); background: var(--primary-weak); }
.kb-item-name { font-size: 14px; font-weight: 600; color: var(--text-1); display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.kb-name-group { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.kb-name-group > .ui-tag { flex: none; }
.kb-item-meta { font-size: 12px; color: var(--text-3); margin-top: 6px; }
.kb-main { min-width: 0; }
.kb-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.mini-stats { display: flex; gap: 12px; }
.mini-stats .stat-card { padding: 12px 16px; }
.mini-stats .stat-value { font-size: 20px; }
</style>
