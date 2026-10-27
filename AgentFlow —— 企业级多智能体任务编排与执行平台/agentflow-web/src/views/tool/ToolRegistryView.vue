<script setup lang="ts">
/**
 * P08 工具注册表（v1.1）。
 *
 * 数据来源（全部真实接口，页面不做任何本地模拟）：
 *  - `GET   /tools`        工具目录：入参 / 出参 / 能力说明 / 依赖配置 / 启停状态 / 最近变更
 *  - `PATCH /tools/{name}` 启用 / 禁用（仅 ADMIN，写 Redis + 审计）
 *
 * 语义边界（界面已标注）：
 *  - 开关只影响**后续新建任务**的工具集：被禁用的工具不会出现在 Planner 的可用工具里；
 *  - 已挂起任务的断点续跑不受影响（连接池不摘除工具进程）；
 *  - 全部禁用时后端会退回全量，避免任务必然产出空计划。
 */
import { computed, onMounted, ref } from 'vue'
import { Message } from '@arco-design/web-vue'
import { isApiError } from '@/api/http'
import { listTools, toggleTool } from '@/api/tools'
import { useAuthStore } from '@/stores/auth'
import { formatFullTime } from '@/utils/format'
import StateBlock from '@/components/StateBlock.vue'
import type { ToolInfo, ToolInputProperty, ToolListData } from '@/types/tool'

const auth = useAuthStore()

const data = ref<ToolListData | null>(null)
const loading = ref(false)
const listError = ref<{ code: string; message: string } | null>(null)
const toggling = ref<string | null>(null)

const detailVisible = ref(false)
const active = ref<ToolInfo | null>(null)

const canWrite = computed(() => auth.isAdmin)

const CATEGORY_TEXT: Record<string, string> = {
  retrieval: '检索',
  database: '数据查询',
  visualization: '可视化',
  notification: '通知',
}

const RISK_TEXT: Record<string, string> = { low: '低', medium: '中', high: '高' }
const RISK_COLOR: Record<string, string> = { low: 'green', medium: 'orange', high: 'red' }

const items = computed(() => data.value?.items ?? [])

async function load(): Promise<void> {
  loading.value = true
  listError.value = null
  try {
    data.value = await listTools()
  } catch (error) {
    if (isApiError(error)) listError.value = { code: error.code, message: error.message }
    else listError.value = { code: 'NETWORK_ERROR', message: '无法连接后端服务' }
  } finally {
    loading.value = false
  }
}

async function onToggle(item: ToolInfo, next: boolean): Promise<void> {
  if (toggling.value) return
  toggling.value = item.name
  try {
    const updated = await toggleTool(item.name, { enabled: next, reason: `前端工具注册表切换` })
    Message.success(`${updated.display_name} 已${next ? '启用' : '禁用'}（下一次新建任务生效）`)
    await load()
    if (active.value?.name === updated.name) active.value = updated
  } catch (error) {
    Message.error(isApiError(error) ? error.message : '切换失败')
  } finally {
    toggling.value = null
  }
}

function openDetail(item: ToolInfo): void {
  active.value = item
  detailVisible.value = true
}

/** 入参 schema → 便于表格展示的行 */
function inputRows(tool: ToolInfo | null): Array<Record<string, unknown>> {
  const props = tool?.input_schema?.properties ?? {}
  const required = new Set(tool?.input_schema?.required ?? [])
  return Object.entries(props).map(([name, spec]) => ({
    name,
    type: spec.enum ? `enum(${spec.enum.join('/')})` : spec.type ?? '—',
    required: required.has(name),
    rule: buildRule(spec),
    description: spec.description ?? '',
  }))
}

function buildRule(spec: ToolInputProperty): string {
  const parts: string[] = []
  if (spec.minimum !== undefined && spec.maximum !== undefined) parts.push(`${spec.minimum}~${spec.maximum}`)
  if (spec.minLength !== undefined) parts.push(`长度≥${spec.minLength}`)
  if (spec.maxLength !== undefined) parts.push(`长度≤${spec.maxLength}`)
  if (spec.maxItems !== undefined) parts.push(`最多 ${spec.maxItems} 项`)
  if (spec.default !== undefined) parts.push(`默认 ${String(spec.default)}`)
  return parts.join('，') || '—'
}

function outputRows(tool: ToolInfo | null): Array<{ field: string; desc: string }> {
  return Object.entries(tool?.output_schema ?? {}).map(([field, desc]) => ({ field, desc: String(desc) }))
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="af-page">
    <div class="af-page-head">
      <div>
        <h1>工具注册表</h1>
        <div class="af-sub">
          内置 MCP 工具的元信息与启停开关 · 被禁用的工具不会进入 Planner 的可用工具集
          <span v-if="data" class="af-mono"> · 生成于 {{ formatFullTime(data.generated_at) }}</span>
        </div>
      </div>
      <a-space>
        <a-button :loading="loading" @click="load">刷新</a-button>
      </a-space>
    </div>

    <a-alert type="info" class="block" :show-icon="true">
      开关只影响<strong>后续新建任务</strong>：Planner 看不到被禁用的工具就不会规划它；
      已挂起任务的断点续跑不受影响。全部禁用时后端会退回全量，避免任务必然产出空计划。
    </a-alert>

    <!-- 缺陷 D-02 建议 b：全部禁用 → fail-open 回退全量，如实提示，避免运维误判 -->
    <a-alert v-if="data?.fail_open" type="warning" class="block" :show-icon="true">
      当前 <strong>全部 {{ data.total }} 个工具均被禁用</strong>，运行时已<strong>自动回退为全量工具</strong>
      （安全兜底：避免 Planner 无工具可用而必然产出空计划）。
      也就是说新建任务仍可使用全部工具；如需真正停用，请保留至少一个工具为启用状态或改用其他管控手段。
    </a-alert>

    <StateBlock
      v-if="listError"
      state="error"
      :description="listError.message"
      :code="listError.code"
      @retry="load"
    />

    <template v-else>
      <a-row :gutter="12">
        <a-col :span="6">
          <a-card :bordered="true" class="block stat-card">
            <div class="stat-label">工具总数</div>
            <div class="stat-value af-num">{{ data?.total ?? 0 }}</div>
          </a-card>
        </a-col>
        <a-col :span="6">
          <a-card :bordered="true" class="block stat-card">
            <div class="stat-label">已启用</div>
            <div class="stat-value af-num is-ok">{{ data?.enabled ?? 0 }}</div>
          </a-card>
        </a-col>
        <a-col :span="6">
          <a-card :bordered="true" class="block stat-card">
            <div class="stat-label">已禁用</div>
            <div class="stat-value af-num is-warn">{{ data?.disabled ?? 0 }}</div>
          </a-card>
        </a-col>
        <a-col :span="6">
          <a-card :bordered="true" class="block stat-card">
            <div class="stat-label">外部依赖</div>
            <div class="stat-value af-num">{{ items.filter((i) => i.external).length }}</div>
          </a-card>
        </a-col>
      </a-row>

      <a-card :bordered="true" class="block">
        <template #title>内置工具</template>
        <template #extra>
          <span class="af-muted">共 {{ data?.total ?? 0 }} 个</span>
        </template>

        <a-table :data="items" :loading="loading" row-key="name" size="small" :bordered="false" :pagination="false">
          <template #columns>
            <a-table-column title="工具" :width="200">
              <template #cell="{ record }">
                <a-link @click="openDetail(record)">{{ record.display_name }}</a-link>
                <div class="af-mono af-muted">{{ record.name }}</div>
              </template>
            </a-table-column>
            <a-table-column title="类别" :width="100">
              <template #cell="{ record }">
                <a-tag size="small">{{ CATEGORY_TEXT[record.category] ?? record.category }}</a-tag>
              </template>
            </a-table-column>
            <a-table-column title="风险" :width="80">
              <template #cell="{ record }">
                <a-tag size="small" :color="RISK_COLOR[record.risk_level] ?? 'gray'">
                  {{ RISK_TEXT[record.risk_level] ?? record.risk_level }}
                </a-tag>
              </template>
            </a-table-column>
            <a-table-column title="依赖" :width="110">
              <template #cell="{ record }">
                <a-tag v-if="record.external" size="small" color="arcoblue">外部服务</a-tag>
                <span v-else class="af-muted">内置</span>
              </template>
            </a-table-column>
            <a-table-column title="说明" data-index="description" :ellipsis="true" :tooltip="true" />
            <a-table-column title="状态" :width="150">
              <template #cell="{ record }">
                <a-tag size="small" :color="record.enabled ? 'green' : 'gray'">
                  {{ record.enabled ? '已启用' : '已禁用' }}
                </a-tag>
                <a-tooltip v-if="record.circuit_open" content="连续失败触发熔断，冷却期内调用将被快速拒绝，到期自动恢复">
                  <a-tag size="small" color="orangered">熔断中</a-tag>
                </a-tooltip>
              </template>
            </a-table-column>
            <a-table-column title="开关" :width="90">
              <template #cell="{ record }">
                <a-switch
                  :model-value="record.enabled"
                  :disabled="!canWrite || toggling === record.name"
                  :loading="toggling === record.name"
                  size="small"
                  @change="(value: string | number | boolean) => onToggle(record, Boolean(value))"
                />
              </template>
            </a-table-column>
            <a-table-column title="操作" :width="80">
              <template #cell="{ record }">
                <a-link @click="openDetail(record)">详情</a-link>
              </template>
            </a-table-column>
          </template>
        </a-table>

        <a-alert v-if="!canWrite" type="normal" class="block" style="margin-top: 12px">
          当前角色为 {{ auth.roleText }}：可查看工具目录，启停开关仅 ADMIN 可操作。
        </a-alert>
      </a-card>

      <a-card v-if="data?.audit?.length" :bordered="true" class="block">
        <template #title>最近变更</template>
        <div v-for="(row, index) in data.audit" :key="index" class="audit-row">
          <span class="af-mono af-muted">{{ formatFullTime(row.at) }}</span>
          <a-tag size="small" :color="row.enabled ? 'green' : 'gray'">{{ row.tool }}</a-tag>
          <span>{{ row.enabled ? '启用' : '禁用' }}</span>
          <span class="af-muted">by {{ row.operator || '—' }}</span>
          <span v-if="row.reason" class="af-muted">· {{ row.reason }}</span>
        </div>
      </a-card>
    </template>

    <!-- 工具详情 -->
    <a-drawer v-model:visible="detailVisible" :width="620" unmount-on-close title="工具详情">
      <template v-if="active">
        <div class="detail-head">
          <div>
            <div class="detail-title">{{ active.display_name }}</div>
            <div class="af-mono af-muted">{{ active.name }} · server={{ active.server }}</div>
          </div>
          <a-tag :color="active.enabled ? 'green' : 'gray'">{{ active.enabled ? '已启用' : '已禁用' }}</a-tag>
        </div>

        <a-descriptions :column="2" size="small" class="block" :label-style="{ width: '92px' }">
          <a-descriptions-item label="类别">{{ CATEGORY_TEXT[active.category] ?? active.category }}</a-descriptions-item>
          <a-descriptions-item label="风险">
            <a-tag size="small" :color="RISK_COLOR[active.risk_level] ?? 'gray'">
              {{ RISK_TEXT[active.risk_level] ?? active.risk_level }}
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="依赖">
            {{ active.external ? '外部服务（项目一知识库）' : '内置' }}
          </a-descriptions-item>
          <a-descriptions-item label="最近变更">
            {{ active.updated_at ? `${formatFullTime(active.updated_at)} · ${active.updated_by ?? '—'}` : '从未变更' }}
          </a-descriptions-item>
        </a-descriptions>

        <a-card :bordered="true" class="block" title="能力说明">
          <p class="detail-text">{{ active.description }}</p>
          <p class="detail-text af-muted">{{ active.capability }}</p>
        </a-card>

        <a-card :bordered="true" class="block" title="入参">
          <a-table :data="inputRows(active)" size="small" :pagination="false" :bordered="false">
            <template #columns>
              <a-table-column title="字段" :width="110">
                <template #cell="{ record }"><span class="af-mono">{{ record.name }}</span></template>
              </a-table-column>
              <a-table-column title="类型" :width="120">
                <template #cell="{ record }"><span class="af-mono">{{ record.type }}</span></template>
              </a-table-column>
              <a-table-column title="必填" :width="60">
                <template #cell="{ record }">
                  <a-tag v-if="record.required" size="small" color="red">是</a-tag>
                  <span v-else class="af-muted">否</span>
                </template>
              </a-table-column>
              <a-table-column title="约束" :width="140" data-index="rule" />
              <a-table-column title="说明" data-index="description" :ellipsis="true" :tooltip="true" />
            </template>
          </a-table>
        </a-card>

        <a-card :bordered="true" class="block" title="出参">
          <div v-for="row in outputRows(active)" :key="row.field" class="kv-row">
            <span class="af-mono kv-key">{{ row.field }}</span>
            <span class="kv-val">{{ row.desc }}</span>
          </div>
        </a-card>

        <a-card :bordered="true" class="block" title="依赖配置">
          <a-descriptions :column="1" size="small" :label-style="{ width: '220px' }">
            <a-descriptions-item v-for="hint in active.config_hints" :key="hint.key" :label="hint.key">
              <div class="af-mono">{{ hint.value || '（未配置）' }}</div>
              <div class="af-muted" style="font-size: 12px">{{ hint.purpose }}</div>
            </a-descriptions-item>
          </a-descriptions>
        </a-card>

        <a-card v-if="active.notes.length" :bordered="true" class="block" title="使用注意">
          <div v-for="(note, index) in active.notes" :key="index" class="note-row">· {{ note }}</div>
        </a-card>
      </template>
    </a-drawer>
  </div>
</template>

<style scoped>
.stat-card {
  padding: 4px 0;
}
.stat-label {
  font-size: 12px;
  color: var(--color-text-3);
}
.stat-value {
  font-size: 24px;
  font-weight: 600;
  margin-top: 4px;
}
.stat-value.is-ok {
  color: rgb(var(--green-6));
}
.stat-value.is-warn {
  color: rgb(var(--orange-6));
}
.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 12px;
}
.detail-title {
  font-size: 16px;
  font-weight: 600;
}
.detail-text {
  margin: 0 0 8px;
  line-height: 1.7;
}
.kv-row {
  display: flex;
  gap: 12px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--color-border-1);
}
.kv-key {
  width: 120px;
  flex: 0 0 auto;
}
.kv-val {
  flex: 1;
  color: var(--color-text-2);
}
.audit-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  font-size: 13px;
}
.note-row {
  font-size: 13px;
  color: var(--color-text-2);
  padding: 2px 0;
}
</style>
