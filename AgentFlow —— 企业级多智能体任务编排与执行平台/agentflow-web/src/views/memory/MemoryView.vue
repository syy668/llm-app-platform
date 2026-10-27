<script setup lang="ts">
/**
 * P07 记忆管理。
 *
 * 全链路后端就绪（FR-014）：
 *  - `GET /memories`（分页在 data.meta，且带 enabled 开关）
 *  - `GET /memories/meta`（分层配置自检）
 *  - `POST /memories`（仅 mid / long，仅 ADMIN）
 *  - `DELETE /memories/{id}`（仅 ADMIN）
 *  - `POST /memories/recall`（相似度召回演示，top_k ≤ 3）
 *
 * 短期层（redis）不出现在管理 API 中：它是任务会话的运行时缓存，无人工干预语义，
 * 所以界面上没有 short 的列表，只在 meta 卡里展示其 TTL。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { Message, Modal } from '@arco-design/web-vue'
import { createMemory, deleteMemory, getMemoryMeta, listMemories, recallMemories } from '@/api/memories'
import { isApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { formatFullTime } from '@/utils/format'
import JsonViewer from '@/components/JsonViewer.vue'
import StateBlock from '@/components/StateBlock.vue'
import MemoryContentCell from '@/components/MemoryContentCell.vue'
import type { MemoryItem, MemoryMetaData, RecallData } from '@/types/memory'

const auth = useAuthStore()

const meta = ref<MemoryMetaData | null>(null)
const metaError = ref<string | null>(null)

const scope = ref<'all' | 'mid' | 'long'>('all')
const items = ref<MemoryItem[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const loading = ref(false)
const listError = ref<{ code: string; message: string } | null>(null)
const enabled = ref(true)

const createVisible = ref(false)
const creating = ref(false)
const form = reactive({ scope: 'long' as 'mid' | 'long', content: '', importance: 3, taskId: '' })

const recallGoal = ref('')
const recalling = ref(false)
const recall = ref<RecallData | null>(null)
const recallError = ref<string | null>(null)

const canWrite = computed(() => auth.isAdmin)

async function loadMeta(): Promise<void> {
  try {
    meta.value = await getMemoryMeta()
    enabled.value = meta.value.enabled
    metaError.value = null
  } catch (error) {
    metaError.value = isApiError(error) ? error.message : '配置加载失败'
  }
}

async function loadList(): Promise<void> {
  loading.value = true
  listError.value = null
  try {
    const data = await listMemories({
      page: page.value,
      size: size.value,
      scope: scope.value === 'all' ? undefined : scope.value,
    })
    items.value = data.items
    total.value = data.meta.total
    enabled.value = data.enabled
  } catch (error) {
    if (isApiError(error)) listError.value = { code: error.code, message: error.message }
    else listError.value = { code: 'NETWORK_ERROR', message: '无法连接后端服务' }
  } finally {
    loading.value = false
  }
}

function switchScope(value: string | number | boolean): void {
  scope.value = value as 'all' | 'mid' | 'long'
  page.value = 1
  void loadList()
}

function onPageChange(current: number): void {
  page.value = current
  void loadList()
}

function openCreate(): void {
  form.scope = 'long'
  form.content = ''
  form.importance = 3
  form.taskId = ''
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  if (form.content.trim().length === 0) {
    Message.warning('请填写记忆正文')
    return
  }
  creating.value = true
  try {
    await createMemory({
      scope: form.scope,
      content: form.content.trim(),
      // long 层重要度强制 >= 3，这里前端就先约束住
      importance: form.scope === 'long' ? Math.max(3, form.importance) : form.importance,
      task_id: form.taskId ? Number(form.taskId) : null,
    })
    Message.success('已创建')
    createVisible.value = false
    await loadList()
  } catch (error) {
    if (isApiError(error) && error.code === 'SERVICE_UNAVAILABLE') {
      Message.warning('分层记忆未启用：请先设置 ENABLE_LAYERED_MEMORY=true')
    } else {
      Message.error(isApiError(error) ? error.message : '创建失败')
    }
  } finally {
    creating.value = false
  }
}

function confirmDelete(item: MemoryItem): void {
  Modal.confirm({
    title: '删除记忆',
    content: `确定删除记忆 #${item.id}？long 层删除后将同时失去召回资格，且不可恢复。`,
    okText: '删除',
    okButtonProps: { status: 'danger' },
    cancelText: '取消',
    onOk: async () => {
      try {
        await deleteMemory(item.id)
        Message.success('已删除')
        await loadList()
      } catch (error) {
        Message.error(isApiError(error) ? error.message : '删除失败')
      }
    },
  })
}

async function runRecall(): Promise<void> {
  if (recallGoal.value.trim().length === 0) {
    Message.warning('请输入用于召回的目标文本')
    return
  }
  recalling.value = true
  recallError.value = null
  recall.value = null
  try {
    recall.value = await recallMemories({ goal: recallGoal.value.trim(), top_k: 3 })
  } catch (error) {
    recallError.value = isApiError(error) ? error.message : '召回失败'
  } finally {
    recalling.value = false
  }
}

/** 相似度着色：>=0.8 命中强、>=0.6 中、其余弱 */
function scoreTone(score: number): string {
  if (score >= 0.8) return 'is-strong'
  if (score >= 0.6) return 'is-mid'
  return 'is-weak'
}

onMounted(() => {
  void loadMeta()
  void loadList()
})
</script>

<template>
  <div class="af-page">
    <div class="af-page-head">
      <div>
        <h1>记忆管理</h1>
        <div class="af-sub">
          分层记忆：short（Redis 会话） / mid（30 天） / long（永久，可召回）
          <span v-if="meta" class="af-mono"> · store={{ meta.store }}</span>
        </div>
      </div>
      <a-space>
        <a-button :loading="loading" @click="loadList">刷新</a-button>
        <a-button v-if="canWrite" type="primary" :disabled="!enabled" @click="openCreate">+ 新增记忆</a-button>
      </a-space>
    </div>

    <a-alert v-if="!enabled" type="warning" class="block" :show-icon="true">
      分层记忆未启用（<span class="af-mono">ENABLE_LAYERED_MEMORY=false</span>）：
      查询返回空列表，写操作会返回 503。请在服务端打开开关后重启。
    </a-alert>
    <a-alert v-if="metaError" type="error" class="block">{{ metaError }}</a-alert>

    <!-- 分层配置 -->
    <a-row :gutter="12">
      <a-col :span="8">
        <a-card :bordered="true" class="block layer-card">
          <template #title>短期层 short</template>
          <div class="layer-kv"><span>存储</span><b class="af-mono">{{ meta?.layers.short.backend ?? '—' }}</b></div>
          <div class="layer-kv"><span>TTL</span><b>{{ meta ? `${meta.layers.short.ttl_seconds}s` : '—' }}</b></div>
          <div class="layer-note">任务会话的运行时缓存，不提供人工增删入口</div>
        </a-card>
      </a-col>
      <a-col :span="8">
        <a-card :bordered="true" class="block layer-card">
          <template #title>中期层 mid</template>
          <div class="layer-kv"><span>存储</span><b class="af-mono">{{ meta?.layers.mid.backend ?? '—' }}</b></div>
          <div class="layer-kv"><span>TTL</span><b>{{ meta ? `${meta.layers.mid.ttl_days} 天` : '—' }}</b></div>
          <div class="layer-note">已摘要压缩，随任务会话写入</div>
        </a-card>
      </a-col>
      <a-col :span="8">
        <a-card :bordered="true" class="block layer-card">
          <template #title>长期层 long</template>
          <div class="layer-kv"><span>存储</span><b class="af-mono">{{ meta?.layers.long.backend ?? '—' }}</b></div>
          <div class="layer-kv">
            <span>晋级门槛</span>
            <b>review score ≥ {{ meta?.layers.long.min_review_score ?? '—' }}</b>
          </div>
          <div class="layer-note">永久保存，参与 Planner 召回的候选池</div>
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="12">
      <!-- 列表 -->
      <a-col :span="16">
        <a-card :bordered="true" class="block">
          <template #title>
            <a-radio-group :model-value="scope" type="button" size="small" @change="switchScope">
              <a-radio value="all">全部</a-radio>
              <a-radio value="mid">mid</a-radio>
              <a-radio value="long">long</a-radio>
            </a-radio-group>
          </template>
          <template #extra>
            <span class="af-muted">共 {{ total }} 条</span>
          </template>

          <StateBlock
            v-if="listError"
            state="error"
            :description="listError.message"
            :code="listError.code"
            @retry="loadList"
          />

          <a-table
            v-else
            :data="items"
            :loading="loading"
            row-key="id"
            size="small"
            :bordered="false"
            :pagination="{ current: page, pageSize: size, total, showTotal: true, size: 'small' }"
            @page-change="onPageChange"
          >
            <template #columns>
              <a-table-column title="ID" :width="64">
                <template #cell="{ record }"><span class="af-mono">{{ record.id }}</span></template>
              </a-table-column>
              <a-table-column title="层级" :width="70">
                <template #cell="{ record }">
                  <a-tag size="small" :color="record.scope === 'long' ? 'purple' : 'arcoblue'">{{ record.scope }}</a-tag>
                </template>
              </a-table-column>
              <a-table-column title="内容" data-index="content" :ellipsis="true" :tooltip="false">
                <template #cell="{ record }">
                  <!-- 正文最长 4000 字，表格自带 tooltip 会溢出视口被裁；
                       限高浮层 + 抽屉的逻辑统一在 MemoryContentCell 里 -->
                  <MemoryContentCell :item="record" />
                </template>
              </a-table-column>
              <a-table-column title="重要度" :width="76" align="right">
                <template #cell="{ record }"><span class="af-num">{{ record.importance }}</span></template>
              </a-table-column>
              <a-table-column title="命中" :width="64" align="right">
                <template #cell="{ record }"><span class="af-num">{{ record.hit_count }}</span></template>
              </a-table-column>
              <a-table-column title="任务" :width="70">
                <template #cell="{ record }">
                  <a-link v-if="record.task_id" class="af-mono" :href="`#/tasks/${record.task_id}`">{{ record.task_id }}</a-link>
                  <span v-else class="af-muted">—</span>
                </template>
              </a-table-column>
              <a-table-column title="创建时间" :width="156">
                <template #cell="{ record }">
                  <span class="mem-time af-muted af-num">{{ formatFullTime(record.created_at) }}</span>
                </template>
              </a-table-column>
              <a-table-column v-if="canWrite" title="操作" :width="72">
                <template #cell="{ record }">
                  <a-link class="mem-op" status="danger" @click="confirmDelete(record)">删除</a-link>
                </template>
              </a-table-column>
            </template>
          </a-table>
        </a-card>
      </a-col>

      <!-- 召回预览 -->
      <a-col :span="8">
        <a-card :bordered="true" class="block">
          <template #title>相似度召回预览</template>
          <template #extra><span class="af-muted">top_k ≤ 3</span></template>

          <a-textarea
            v-model="recallGoal"
            placeholder="输入一段目标文本，模拟 Planner 召回长期记忆"
            :auto-size="{ minRows: 3, maxRows: 5 }"
            :max-length="500"
            show-word-limit
          />
          <a-button type="primary" long class="mt-sm" :loading="recalling" :disabled="!enabled" @click="runRecall">
            召回
          </a-button>

          <a-alert v-if="recallError" type="error" class="mt-sm">{{ recallError }}</a-alert>

          <a-alert
            v-if="recall && recall.retrieval?.scorer === 'local-keyword'"
            type="warning"
            class="mt-sm"
          >
            当前为本地关键词召回{{ recall.retrieval.degraded ? '（向量通道故障，已自动降级）' : '（Mock 模式）' }}：分数是关键词重合度，不是语义相似度
          </a-alert>

          <template v-if="recall">
            <div v-if="recall.hits.length === 0" class="af-muted mt-sm">没有命中任何记忆（相似度低于阈值）</div>
            <div v-for="hit in recall.hits" :key="hit.memory_id" class="hit">
              <div class="hit-head">
                <span class="af-mono">#{{ hit.memory_id }}</span>
                <a-tag size="small" :color="hit.scope === 'long' ? 'purple' : 'arcoblue'">{{ hit.scope }}</a-tag>
                <span class="hit-score" :class="scoreTone(hit.score)">{{ hit.score.toFixed(2) }}</span>
              </div>
              <div class="hit-content">{{ hit.content }}</div>
            </div>

            <div v-if="recall.injection && recall.injection.length" class="mt-sm">
              <div class="label">注入 Planner 的上下文（MEMORY_INJECT_MAX_CHARS 截断）</div>
              <JsonViewer :value="recall.injection" :max-height="180" />
            </div>
          </template>
        </a-card>
      </a-col>
    </a-row>

    <!-- 新增记忆 -->
    <a-modal v-model:visible="createVisible" title="新增记忆" :ok-loading="creating" ok-text="创建" @ok="submitCreate">
      <a-form :model="form" layout="vertical">
        <a-form-item label="层级">
          <a-radio-group v-model="form.scope" type="button">
            <a-radio value="long">long（永久，可召回）</a-radio>
            <a-radio value="mid">mid（30 天）</a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item label="记忆正文" required>
          <a-textarea v-model="form.content" :max-length="4000" show-word-limit :auto-size="{ minRows: 4, maxRows: 8 }" />
        </a-form-item>
        <a-form-item :label="`重要度（1~5${form.scope === 'long' ? '，long 强制 ≥ 3' : ''}）`">
          <a-slider v-model="form.importance" :min="1" :max="5" :step="1" show-input />
        </a-form-item>
        <a-form-item label="关联任务 ID（可空）">
          <a-input v-model="form.taskId" placeholder="例如 1024" allow-clear />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.block {
  margin-bottom: 12px;
}

.layer-card :deep(.arco-card-body) {
  padding: 12px 16px;
}

.layer-kv {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  line-height: 22px;
}

.layer-kv span {
  color: var(--color-text-3);
}

.layer-kv b {
  font-weight: 400;
}

.layer-note {
  margin-top: 6px;
  font-size: 11px;
  color: var(--color-text-3);
}

.mt-sm {
  margin-top: 8px;
}

.hit {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--color-border-2, #e5e6eb);
  border-radius: 2px;
}

.hit-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  margin-bottom: 4px;
}

.hit-score {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}

.hit-score.is-strong {
  color: rgb(var(--success-6));
}

.hit-score.is-mid {
  color: rgb(var(--warning-6));
}

.hit-score.is-weak {
  color: var(--color-text-3);
}

.hit-content {
  font-size: 12px;
  line-height: 19px;
  color: var(--color-text-2);
}

.label {
  font-size: 11.5px;
  color: var(--color-text-3);
  margin-bottom: 6px;
}

/* 创建时间 / 操作：单行显示，不折行 */
.mem-time,
.mem-op {
  white-space: nowrap;
}
</style>
