<script setup lang="ts">
/**
 * P06 评测与实验看板。
 *
 * 接入策略（W6 文档 §4 映射表 + 对接清单 G3）——**直连 + 异常回落**：
 *  - A/B 对比：`GET /eval/runs/summary`（W6 §3.3，对 V 开放）；失败回落占位。
 *  - 指标趋势：`GET /eval/metrics`（W6 §3.4，对 V 开放）；失败回落占位。
 *  - 运行列表：`GET /eval/runs`（W6 §3.2，仅 A/R；viewer 直接不请求）；失败回落占位。
 *  - 发起回归评测：`POST /eval/runs`（W6 §3.5，仅 A/R）；按钮仅 A/R 渲染，
 *    失败时按错误码显式提示，不静默失败。
 *  - 单任务评测分查询：走既有真实接口 GET /tasks/{id}，保持不变。
 *
 * 回落均为显式占位，不填充模拟数据。
 *
 * v1.3 去伪装（止损）：后端 `MOCK_EXTERNAL_EVAL=true` 时，`_mock_metrics()` 会用
 * `random.randrange` 造完成率 / F1 / Token 成本——这行代码本身没错（联调需要），
 * 错的是"把随机数当业务结论展示且不加警示"。本页因此：
 *  1) 顶部常驻黄色警告条（来自 `GET /api/v1/health` 的 `mock.external_eval`）；
 *  2) 运行列表逐行按 `is_mock` 打红标「Mock」；
 *  3) 所有指标数字置灰并带 title 提示，视觉上与真实可用指标区分开。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { Message } from '@arco-design/web-vue'
import { useAuthStore } from '@/stores/auth'
import { getTaskDetail } from '@/api/tasks'
import { fetchHealthSummary } from '@/api/health'
import { listEvalRuns, getEvalRunSummary, getEvalMetricsTrend, createEvalRun } from '@/api/eval'
import { isApiError, isMissingEndpoint } from '@/api/http'
import { formatTokens, formatPercent, formatShortTime, formatEvalScore, formatReviewVerdict, formatReviewScore } from '@/utils/format'
import JsonViewer from '@/components/JsonViewer.vue'
import StateBlock from '@/components/StateBlock.vue'
import TaskStatusTag from '@/components/TaskStatusTag.vue'
import type { TaskDetail } from '@/types/task'
import type { EvalSummaryGroup, EvalMetricsPoint, EvalRunItem } from '@/types/eval'

const auth = useAuthStore()
/** PRD 旅程三：访客只看汇总指标与单任务查询；运行列表 / 发起评测仅 A/R（W6 §3 角色约定） */
const isViewer = computed(() => auth.isViewer)
/** 汇总类端点（summary/metrics）对三角色开放，viewer 也可请求 */
const canManage = computed(() => !auth.isViewer)

// ---------------------------------------------------------------------------
// Mock 模式自曝（GET /api/v1/health → mock.external_eval）
// ---------------------------------------------------------------------------
/**
 * 后端 `MOCK_EXTERNAL_EVAL=true` 时指标是随机数。这里主动问后端要开关状态，
 * 而不是在前端写死——开关只可能在服务端改，前端猜必然漂移。
 * 请求失败按「未知」处理：不弹警告但也不会假装数据可信（灰度由 is_mock 逐行兜底）。
 */
const mockEval = ref(false)

async function loadMockFlag(): Promise<void> {
  try {
    const health = await fetchHealthSummary()
    mockEval.value = Boolean(health.mock?.external_eval)
  } catch {
    mockEval.value = false
  }
}

/** 指标单元格的"模拟数据"提示（逐行 is_mock 与全局 mockEval 都走这一份文案） */
const MOCK_TITLE = '模拟数据（MOCK_EXTERNAL_EVAL=true，由 _mock_metrics 随机生成），不可用于业务结论'

/**
 * 秒 → 可读耗时（真实评测墙钟，可能长达数十分钟）。
 * 不用 formatDuration：那个函数入参是「开始/结束时间字符串」，口径不同。
 */
function fmtDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return rest > 0 ? `${minutes}m${rest}s` : `${minutes}m`
}

// ---------------------------------------------------------------------------
// A/B 对比（GET /eval/runs/summary）
// ---------------------------------------------------------------------------
const summaryOn = ref(false)
const summaryGroups = ref<EvalSummaryGroup[]>([])
/** 404（未上线）与 403（无权限）分开呈现 */
const summaryBlocked = ref(false)
/** 非权限类失败的原始原因（如后端不可用时代理回的 SERVICE_UNAVAILABLE），直接展示给用户 */
const summaryError = ref('')

async function loadSummary(): Promise<void> {
  try {
    const data = await getEvalRunSummary(['compress_on', 'compress_off'])
    summaryGroups.value = data.groups
    summaryOn.value = true
    summaryError.value = ''
  } catch (error) {
    summaryOn.value = false
    summaryBlocked.value = isApiError(error) && error.code === 'FORBIDDEN'
    summaryError.value = isApiError(error) && !summaryBlocked.value ? error.message : ''
    if (!isMissingEndpoint(error)) console.error('[eval] A/B 对比加载失败', error)
  }
}

// ---------------------------------------------------------------------------
// 指标趋势（GET /eval/metrics）
// ---------------------------------------------------------------------------
const trendOn = ref(false)
const trendPoints = ref<EvalMetricsPoint[]>([])

async function loadTrend(): Promise<void> {
  try {
    const data = await getEvalMetricsTrend()
    trendPoints.value = data.points
    trendOn.value = true
  } catch (error) {
    trendOn.value = false
    if (!isMissingEndpoint(error)) console.error('[eval] 指标趋势加载失败', error)
  }
}

// ---------------------------------------------------------------------------
// 运行列表（GET /eval/runs，仅 A/R）+ 发起回归评测（POST /eval/runs）
// ---------------------------------------------------------------------------
const runsOn = ref(false)
const runsPage = ref(1)
const runsSize = 10
const runsTotal = ref(0)
const runs = ref<EvalRunItem[]>([])
const runsLoading = ref(false)
/** viewer 视角的禁用说明（接口就绪但无权限） */
const runsForbidden = ref(false)
const triggering = ref(false)
/** G8 评测标签过滤：与任务创建时的 eval_tag 对应，后端按精确匹配过滤 */
const runsTag = ref('')
/** 发起评测时绑定的标签（≤64），留空则不绑定 */
const newRunTag = ref('')
/** A/B 分组：空 = 普通回归（无分组）；compress_on/off = 进「上下文压缩 A/B 对比」卡对应组 */
const newRunVariant = ref<'compress_on' | 'compress_off' | ''>('')

async function loadRuns(page = runsPage.value): Promise<void> {
  if (isViewer.value) return // viewer 无权且无需请求（W6：V 仅汇总）
  runsLoading.value = true
  try {
    const tag = runsTag.value.trim()
    const data = await listEvalRuns({ page, size: runsSize, ...(tag ? { eval_tag: tag } : {}) })
    runs.value = data.items
    runsTotal.value = data.meta.total
    runsPage.value = page
    runsOn.value = true
  } catch (error) {
    runsOn.value = false
    runsForbidden.value = isApiError(error) && error.code === 'FORBIDDEN'
    if (!isMissingEndpoint(error)) console.error('[eval] 运行列表加载失败', error)
  } finally {
    runsLoading.value = false
  }
}

function changeRunsPage(page: number): void {
  loadRuns(page)
}

/** 发起回归评测：默认 PRD 写死的数据集 agentflow-30；接口不可用时显式提示 */
async function triggerRegression(): Promise<void> {
  triggering.value = true
  try {
    const tag = newRunTag.value.trim()
    const variant = newRunVariant.value
    const result = await createEvalRun({
      dataset_id: 'agentflow-30',
      ...(tag ? { eval_tag: tag } : {}),
      // 带 variant = 进 A/B 对比分组；不选 = 普通回归运行（variant=null）
      ...(variant ? { variant } : {}),
    })
    Message.success(
      `回归评测已提交：${result.run_no}（${result.status}）${variant ? `· 分组 ${variant}` : ''}，指标将由后端轮询回写`,
    )
    await loadRuns(1)
  } catch (error) {
    if (isMissingEndpoint(error)) {
      Message.warning('评测接口不可用（旧版本后端或路由异常），未提交评测')
    } else if (isApiError(error)) {
      Message.error(`提交失败：${error.code} ${error.message}`)
    } else {
      Message.error('无法连接后端服务')
    }
  } finally {
    triggering.value = false
  }
}

// ---------------------------------------------------------------------------
// 单任务评测分查询（既有真实接口，保持不变）
// ---------------------------------------------------------------------------
const query = reactive({ taskId: '' })
const loading = ref(false)
const result = ref<TaskDetail | null>(null)
const queryError = ref<string | null>(null)

async function lookup(): Promise<void> {
  // 数字 = 任务主键；AF 前缀 = 任务编号（后端 2026-10-27 起两者都认，
  // 因为界面各列表只展示任务编号，数字主键无处可看，只收数字等于让人无从查起）
  const ref = query.taskId.trim()
  if (!ref) {
    Message.warning('请输入任务 ID（数字）或任务编号（AF2026…）')
    return
  }
  loading.value = true
  queryError.value = null
  result.value = null
  try {
    result.value = await getTaskDetail(ref)
  } catch (error) {
    queryError.value = isApiError(error) ? `${error.code}：${error.message}` : '无法连接后端服务'
  } finally {
    loading.value = false
  }
}

/** 页头状态标签：全部接入 → 绿色；否则保持橙色提示 */
const allConnected = computed(() => summaryOn.value && trendOn.value && (isViewer.value || runsOn.value))

function refreshAll(): void {
  loadMockFlag()
  loadSummary()
  loadTrend()
  loadRuns(1)
}

onMounted(() => {
  refreshAll()
})
</script>

<template>
  <div class="af-page">
    <div class="af-page-head">
      <div>
        <h1>评测与实验</h1>
        <div class="af-sub">用数据判断上下文压缩与分层记忆策略是否值得开启</div>
      </div>
      <a-space>
        <!-- Mock 模式下"接口已接入"是误导：接上了，接的是随机数。绿标必须让位给黄标。 -->
        <a-tag v-if="mockEval" color="orange" size="large" class="mock-tag">⚠️ Mock 数据</a-tag>
        <a-tag v-else-if="!allConnected" color="orange">评测接口暂不可用</a-tag>
        <a-select
          v-if="canManage"
          v-model="newRunVariant"
          placeholder="分组（可留空）"
          allow-clear
          size="small"
          style="width: 170px"
        >
          <a-option value="compress_on">compress_on（压缩开启）</a-option>
          <a-option value="compress_off">compress_off（压缩关闭）</a-option>
        </a-select>
        <a-input
          v-if="canManage"
          v-model="newRunTag"
          placeholder="eval_tag（可留空）"
          allow-clear
          size="small"
          style="width: 180px"
          :max-length="64"
        />
        <a-button v-if="canManage" type="primary" :loading="triggering" @click="triggerRegression">
          发起回归评测
        </a-button>
      </a-space>
    </div>

    <!-- Mock 自曝：本条是这个页面最重要的一句话，任何情况下都不能被折叠/省略 -->
    <a-alert v-if="mockEval" type="warning" class="mock-banner" :closable="false">
      <template #title>
        <span class="mock-banner-title">⚠️ 当前为 Mock 评测数据 · 仅供接口联调，不可用于业务结论</span>
      </template>
      后端 <span class="af-mono">MOCK_EXTERNAL_EVAL=true</span>：四项指标
      全部由 <span class="af-mono">_mock_metrics()</span> 随机生成，每次运行数值都不同（同一个数据集跑两次，
      完成率会从 88% 跳到 91%）。要拿它判断"上下文压缩值不值得开"，请先关闭该开关、接入真实评测服务后重跑。
    </a-alert>

    <a-alert v-if="isViewer" type="info" class="af-viewer-tip">
      访客角色仅可查看汇总指标与单任务查询；评测运行列表、发起回归评测等完整功能仅对管理员 / 审核人开放。
    </a-alert>

    <!-- A/B 对比：GET /eval/runs/summary（W6 §3.3，V 可看，token 字段脱敏） -->
    <a-row :gutter="12">
      <a-col :span="12">
        <a-card :bordered="true" class="block">
          <template #title>上下文压缩 A/B 对比</template>
          <template #extra><span class="af-muted af-mono">GET /eval/runs/summary</span></template>

          <StateBlock
            v-if="!summaryOn"
            state="pending"
            :description="summaryBlocked
              ? '当前角色无权访问评测汇总接口。'
              : (summaryError || '按 variant（compress_on / compress_off）聚合 eval_runs，接口不可用或暂无数据。')"
            expected-api="GET /eval/runs/summary?variant=compress_on&variant=compress_off"
            :min-height="200"
          />

          <!-- 结构性空态：后端按 variant 分组聚合，而触发评测时前端没带 variant，
               所以 6 条 run 的 variant 全是 null → groups 恒为空。这不是"暂时没数据"。 -->
          <StateBlock
            v-else-if="summaryGroups.length === 0"
            state="empty"
            title="暂无分组实验数据"
            description="暂无分组实验数据，请创建带 variant 的评测任务（compress_on / compress_off）。"
            expected-api="POST /eval/runs { variant: 'compress_on' | 'compress_off' }"
            :min-height="200"
          />

          <div v-else class="ab-wrap">
            <table class="ab-table">
              <thead>
                <tr>
                  <th>分组</th>
                  <th class="num">运行数</th>
                  <th class="num">Faithfulness</th>
                  <th class="num">答案相关性</th>
                  <th class="num">上下文精确率</th>
                  <th class="num">上下文召回率</th>
                  <th class="num">Token 节省率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="group in summaryGroups" :key="group.variant ?? 'none'">
                  <td><span class="af-mono">{{ group.variant ?? '—' }}</span></td>
                  <td class="num">{{ group.run_count }}</td>
                  <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                    {{ formatPercent(group.avg_faithfulness) }}
                  </td>
                  <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                    {{ formatPercent(group.avg_answer_relevancy) }}
                  </td>
                  <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                    {{ formatPercent(group.avg_context_precision) }}
                  </td>
                  <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                    {{ formatPercent(group.avg_context_recall) }}
                  </td>
                  <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                    {{ group.token_saving_rate == null ? '—（基线）' : formatPercent(group.token_saving_rate) }}
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="af-muted foot-note">
              指标为 ragas 四项原始口径（temperature 固定 0），
              评的是「知识库检索 + 生成」链路，不是 AgentFlow 编排质量。
              token_saving_rate 以 compress_off 组为基线（1 − 该组/基线），由后端按 W6 §3.3 口径计算。
              <span v-if="mockEval" class="mock-note">（当前为 Mock 数据，此处仅验证口径连通，不代表真实结论）</span>
            </div>
          </div>
        </a-card>
      </a-col>

      <!-- 指标趋势：GET /eval/metrics（W6 §3.4，V 可看） -->
      <a-col :span="12">
        <a-card :bordered="true" class="block">
          <template #title>指标趋势</template>
          <template #extra><span class="af-muted af-mono">GET /eval/metrics</span></template>

          <StateBlock
            v-if="!trendOn"
            state="pending"
            description="评测分、Token 成本、P95 延迟的按天趋势来自 eval_runs 聚合；接口不可用或暂无数据。"
            expected-api="GET /eval/metrics?start&end"
            :min-height="200"
          />

          <table v-else class="ab-table">
            <thead>
              <tr>
                <th>日期</th>
                <th class="num">运行数</th>
                <th class="num">Faithfulness</th>
                <th class="num">答案相关性</th>
                <th class="num">上下文精确率</th>
                <th class="num">上下文召回率</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="point in trendPoints" :key="point.date">
                <td class="af-mono">{{ point.date }}</td>
                <td class="num">{{ point.run_count }}</td>
                <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                  {{ formatPercent(point.avg_faithfulness) }}
                </td>
                <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                  {{ formatPercent(point.avg_answer_relevancy) }}
                </td>
                <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                  {{ formatPercent(point.avg_context_precision) }}
                </td>
                <td class="num" :class="{ 'mock-cell': mockEval }" :title="mockEval ? MOCK_TITLE : undefined">
                  {{ formatPercent(point.avg_context_recall) }}
                </td>
              </tr>
              <tr v-if="trendPoints.length === 0">
                <td colspan="6" class="af-muted" style="text-align: center">窗口内暂无 DONE 状态的评测运行</td>
              </tr>
            </tbody>
          </table>
        </a-card>
      </a-col>
    </a-row>

    <!-- 运行列表：GET /eval/runs（W6 §3.2，仅 A/R） -->
    <a-card :bordered="true" class="block">
      <template #title>评测运行列表</template>
      <template #extra>
        <a-space :size="8">
          <a-input
            v-model="runsTag"
            placeholder="按 eval_tag 过滤"
            allow-clear
            size="mini"
            style="width: 170px"
            @press-enter="loadRuns(1)"
            @clear="loadRuns(1)"
          >
            <template #prefix>标签</template>
          </a-input>
          <span class="af-muted">仅管理员 / 审核人可见（PRD：V 仅汇总）</span>
        </a-space>
      </template>

      <!-- viewer：不请求，直接给出权限说明（与后端 403 行为一致） -->
      <StateBlock
        v-if="isViewer"
        state="empty"
        title="访客角色不可见"
        description="评测运行明细仅对管理员 / 审核人开放；访客可查看上方 A/B 对比与指标趋势汇总。"
        :min-height="180"
      />

      <template v-else>
        <StateBlock
          v-if="!runsOn"
          state="pending"
          :description="runsForbidden
            ? '当前角色无权访问评测运行列表（后端 require_role 拦截）。'
            : '接口不可用或暂无评测运行记录（eval_runs 为空）。'"
          expected-api="GET /eval/runs?page&size&source&variant"
          :min-height="220"
        />

        <a-table
          v-else
          :data="runs"
          :loading="runsLoading"
          row-key="run_id"
          :pagination="{ pageSize: runsSize, current: runsPage, total: runsTotal, showTotal: true }"
          @page-change="changeRunsPage"
          size="small"
          :bordered="false"
          class="runs-table"
        >
          <template #columns>
            <a-table-column title="运行编号" :width="200">
              <template #cell="{ record }">
                <span class="af-mono">{{ record.run_no }}</span>
                <!-- 逐行红标：即使全局开关后来关掉，历史 mock 记录仍要自曝 -->
                <a-tag v-if="record.is_mock" color="red" size="small" class="mock-row-tag" :title="MOCK_TITLE">
                  Mock
                </a-tag>
              </template>
            </a-table-column>
            <a-table-column title="来源" :width="130">
              <template #cell="{ record }"><span class="af-mono">{{ record.source }}</span></template>
            </a-table-column>
            <a-table-column title="分组" :width="120">
              <template #cell="{ record }"><span class="af-mono">{{ record.variant ?? '—' }}</span></template>
            </a-table-column>
            <a-table-column title="评测标签" :width="140">
              <template #cell="{ record }">
                <a-tag v-if="record.eval_tag" size="small" color="gold">{{ record.eval_tag }}</a-tag>
                <span v-else class="af-muted">—</span>
              </template>
            </a-table-column>
            <a-table-column title="状态" :width="100">
              <template #cell="{ record }">
                <a-tag
                  :color="record.status === 'DONE' ? 'green' : record.status === 'FAILED' ? 'red' : 'orange'"
                  size="small"
                >{{ record.status }}</a-tag>
              </template>
            </a-table-column>
            <a-table-column title="指标来源" :width="150">
              <template #cell="{ record }">
                <a-tag v-if="record.is_mock" color="red" size="small" :title="MOCK_TITLE">随机生成</a-tag>
                <a-tag
                  v-else-if="record.eval_backend === 'project1_ragas'"
                  color="arcoblue"
                  size="small"
                  :title="`判分模型 ${record.judge_model ?? '—'}（temperature=0）；评的是知识库检索+生成链路`"
                >项目一 RAGAS</a-tag>
                <span v-else class="af-muted">—</span>
              </template>
            </a-table-column>
            <a-table-column title="Faithfulness" :width="110" align="right">
              <template #cell="{ record }">
                <span :class="{ 'mock-cell': record.is_mock }" :title="record.is_mock ? MOCK_TITLE : undefined">
                  {{ formatPercent(record.metrics?.faithfulness) }}
                </span>
              </template>
            </a-table-column>
            <a-table-column title="答案相关性" :width="120" align="right">
              <template #cell="{ record }">
                <span :class="{ 'mock-cell': record.is_mock }" :title="record.is_mock ? MOCK_TITLE : undefined">
                  {{ formatPercent(record.metrics?.answer_relevancy) }}
                </span>
              </template>
            </a-table-column>
            <a-table-column title="上下文精确率" :width="130" align="right">
              <template #cell="{ record }">
                <span :class="{ 'mock-cell': record.is_mock }" :title="record.is_mock ? MOCK_TITLE : undefined">
                  {{ formatPercent(record.metrics?.context_precision) }}
                </span>
              </template>
            </a-table-column>
            <a-table-column title="上下文召回率" :width="130" align="right">
              <template #cell="{ record }">
                <span :class="{ 'mock-cell': record.is_mock }" :title="record.is_mock ? MOCK_TITLE : undefined">
                  {{ formatPercent(record.metrics?.context_recall) }}
                </span>
              </template>
            </a-table-column>
            <a-table-column title="样本数" :width="80" align="right">
              <template #cell="{ record }">
                <span class="af-num">
                  {{ record.metrics?.success_items != null
                    ? `${record.metrics.success_items}/${record.sample_count}`
                    : record.sample_count }}
                </span>
              </template>
            </a-table-column>
            <a-table-column title="耗时" :width="90" align="right">
              <template #cell="{ record }">
                <span class="af-num af-muted">
                  {{ record.duration_s == null ? '—' : fmtDuration(record.duration_s) }}
                </span>
              </template>
            </a-table-column>
            <a-table-column title="创建时间" :width="110">
              <template #cell="{ record }">
                <span class="af-muted af-num">{{ formatShortTime(record.created_at) }}</span>
              </template>
            </a-table-column>
            <a-table-column title="失败原因" :ellipsis="true" :tooltip="true">
              <template #cell="{ record }">
                <span class="af-muted">{{ record.status === 'FAILED' ? (record.error_message ?? '—') : '—' }}</span>
              </template>
            </a-table-column>
          </template>
        </a-table>
      </template>
    </a-card>

    <!-- 真实可用：单任务评测分（既有接口，保持不变） -->
    <a-card :bordered="true" class="block">
      <template #title>单任务评测分查询</template>
      <template #extra><span class="af-muted af-mono">GET /tasks/{id}</span></template>

      <a-space class="lookup">
        <a-input
          v-model="query.taskId"
          placeholder="任务 ID（数字）或任务编号，如 AF20261025-0025"
          style="width: 260px"
          allow-clear
          @press-enter="lookup"
        />
        <a-button type="primary" :loading="loading" @click="lookup">查询</a-button>
        <span class="af-muted">评测分来自任务详情快照的 eval_score，仅在该任务跑到 reviewer 后才会有值</span>
      </a-space>

      <a-alert v-if="queryError" type="error" class="mt">{{ queryError }}</a-alert>

      <a-descriptions v-if="result" :column="4" size="small" class="mt" bordered>
        <a-descriptions-item label="任务编号">
          <span class="af-mono">{{ result.task_no }}</span>
        </a-descriptions-item>
        <a-descriptions-item label="任务 ID">
          <span class="af-mono">{{ result.task_id }}</span>
        </a-descriptions-item>
        <a-descriptions-item label="状态"><TaskStatusTag :status="result.status" /></a-descriptions-item>
        <a-descriptions-item label="评测分 eval_score">{{ formatEvalScore(result.eval_score) }}</a-descriptions-item>
        <a-descriptions-item label="Token 消耗">{{ formatTokens(result.token_usage) }}</a-descriptions-item>
        <a-descriptions-item label="重规划次数">{{ result.replan_count }}</a-descriptions-item>
        <a-descriptions-item label="审阅结论">{{ formatReviewVerdict(result.review) }}</a-descriptions-item>
        <a-descriptions-item label="审阅得分">{{ formatReviewScore(result.review) }}</a-descriptions-item>
        <a-descriptions-item label="子任务数">{{ result.subtasks.length }}</a-descriptions-item>
      </a-descriptions>

      <div v-if="result" class="mt">
        <div class="label">review 原文（开放结构）</div>
        <JsonViewer :value="result.review" :max-height="220" empty-text="无 review 数据" />
      </div>
    </a-card>
  </div>
</template>

<style scoped>
.block {
  margin-bottom: 12px;
}

.lookup {
  margin-bottom: 4px;
}

.mt {
  margin-top: 12px;
}

.label {
  font-size: 11.5px;
  color: var(--color-text-3);
  margin-bottom: 6px;
}

/* A/B 对比与趋势的紧凑表格 */
.ab-wrap {
  padding: 4px 0;
}

/* ---------------- Mock 去伪装 ---------------- */
/* 黄色大标：比普通 tag 大一号、加粗，让"数据是假的"在视线高度上第一眼可见 */
.mock-banner {
  margin-bottom: 12px;
  border-left: 3px solid rgb(var(--warning-6));
}

.mock-banner :deep(.arco-alert-content) {
  font-size: 12.5px;
  line-height: 20px;
}

.mock-banner-title {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.2px;
}

.mock-tag {
  font-weight: 600;
}

.mock-row-tag {
  margin-left: 6px;
  font-weight: 600;
}

/* 指标置灰：与"真实可用数字"在视觉上必须能一眼分开 */
.mock-cell {
  color: var(--color-text-3) !important;
  cursor: help;
}

.mock-note {
  color: rgb(var(--warning-6));
}

.ab-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.ab-table th,
.ab-table td {
  padding: 7px 8px;
  border-bottom: 1px solid var(--color-fill-2);
  text-align: left;
  white-space: nowrap;
}

.ab-table th {
  color: var(--color-text-3);
  font-weight: 400;
  background: var(--color-fill-1);
}

.ab-table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.foot-note {
  margin-top: 8px;
  font-size: 11px;
}

/* 评测运行列表：指标列表头（答案相关性/上下文精确率/上下文召回率）不换行。
   Arco 默认 th 允许折行，列宽 100px 装不下五个汉字会被拆成两行。 */
.runs-table :deep(.arco-table-th) {
  white-space: nowrap;
}
</style>
