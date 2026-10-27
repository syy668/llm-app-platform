<script setup lang="ts">
/**
 * P03 任务提交页。
 *
 * 入参严格等于后端 `TaskCreateRequest`（extra="forbid"）：
 *   { goal, time_range: { start, end }, sync, compress, use_memory, eval_tag }
 * 多传任何字段都会 422。
 *
 * G8 三个高级开关（v1.0 已闭环，后端图上有对应分支，默认全关走原链路）：
 *  - 上下文压缩 `compress`  → planner 之后插入 `context_compress` 节点；
 *  - 分层记忆   `use_memory`→ START 之后插入 `memory_recall` 节点；
 *  - 评测标记   `eval_tag`  → 任务落库标签，供 `GET /eval/runs?eval_tag=` 关联筛选（≤64 字）。
 *
 * 提交走 `POST /tasks/stream`：它是 W3 提供的流式超集入口，返回 stream_url，
 * 创建成功后直接跳详情页并由详情页建立 SSE 连接。
 * 若不传 sync，任务在后台执行；传 true 则阻塞到终态（大任务会把请求挂很久）。
 */
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Message } from '@arco-design/web-vue'
import { createTaskStream } from '@/api/tasks'
import { getTaskDetail } from '@/api/tasks'
import { rememberTask } from '@/utils/recentTasks'
import { isApiError } from '@/api/http'
import dayjs from 'dayjs'

const router = useRouter()

const form = reactive({
  goal: '',
  range: [] as string[],
  sync: false,
  /** G8：上下文压缩 */
  compress: false,
  /** G8：分层记忆检索 */
  useMemory: false,
  /** G8：是否标记为评测样本（开关） */
  evalMark: false,
  /** G8：评测标签正文（≤64） */
  evalTag: '',
})

const errors = reactive({ goal: '', range: '', evalTag: '' })
const submitting = ref(false)

const EVAL_TAG_MAX = 64
const goalLength = computed(() => form.goal.trim().length)
const evalTagLength = computed(() => form.evalTag.trim().length)
const canSubmit = computed(() => goalLength.value > 0 && form.range.length === 2 && !submitting.value)
/** 提交前给用户看一眼实际生效的高级选项 */
const activeSwitchText = computed(() => {
  const active: string[] = []
  if (form.compress) active.push('上下文压缩')
  if (form.useMemory) active.push('分层记忆')
  if (form.evalMark && form.evalTag.trim()) active.push(`评测标签 ${form.evalTag.trim()}`)
  return active
})

/** 常用时间范围快捷项 */
const shortcuts = [
  { label: '近 7 天', value: () => [dayjs().subtract(6, 'day').format('YYYY-MM-DD'), dayjs().format('YYYY-MM-DD')] },
  { label: '近 30 天', value: () => [dayjs().subtract(29, 'day').format('YYYY-MM-DD'), dayjs().format('YYYY-MM-DD')] },
  {
    label: '上月整月',
    value: () => [
      dayjs().subtract(1, 'month').startOf('month').format('YYYY-MM-DD'),
      dayjs().subtract(1, 'month').endOf('month').format('YYYY-MM-DD'),
    ],
  },
]

const examples = [
  '分析 2026 年 9 月女装类目 GMV 环比下滑原因，输出结论与改进建议',
  '生成本周库存周转异常 SKU 清单并推送运营群',
  '对比近 30 天两场大促的转化率差异，输出归因',
]

function validate(): boolean {
  errors.goal = ''
  errors.range = ''
  errors.evalTag = ''
  if (goalLength.value === 0) errors.goal = '请输入任务目标'
  else if (goalLength.value > 500) errors.goal = '目标不能超过 500 字'
  if (form.range.length !== 2) errors.range = '请选择业务时间范围'
  else if (form.range[0] > form.range[1]) errors.range = '开始日期不能晚于结束日期'
  if (form.evalMark) {
    if (evalTagLength.value === 0) errors.evalTag = '已开启「标记为评测样本」，请填写标签'
    else if (evalTagLength.value > EVAL_TAG_MAX) errors.evalTag = `标签不能超过 ${EVAL_TAG_MAX} 字`
  }
  return !errors.goal && !errors.range && !errors.evalTag
}

function applyExample(text: string): void {
  form.goal = text
  errors.goal = ''
}

function reset(): void {
  form.goal = ''
  form.range = []
  form.sync = false
  form.compress = false
  form.useMemory = false
  form.evalMark = false
  form.evalTag = ''
  errors.goal = ''
  errors.range = ''
  errors.evalTag = ''
}

async function handleSubmit(): Promise<void> {
  if (!validate()) {
    // 校验不过必须给明确反馈，不能静默 return（曾表现为「点提交没反应」）
    const first = errors.goal || errors.range || errors.evalTag
    Message.error(first || '表单校验未通过，请检查输入')
    return
  }
  submitting.value = true
  try {
    const result = await createTaskStream({
      goal: form.goal.trim(),
      time_range: { start: form.range[0], end: form.range[1] },
      sync: form.sync,
      // G8 三开关：关掉时显式传 false / null，语义与后端默认值一致，避免"没传"和"关了"分不清
      compress: form.compress,
      use_memory: form.useMemory,
      eval_tag: form.evalMark && evalTagLength.value > 0 ? form.evalTag.trim() : null,
    })
    const suffix = activeSwitchText.value.length ? `（已启用：${activeSwitchText.value.join('、')}）` : ''
    Message.success(`任务已创建：${result.task_no}${suffix}`)
    // 记录到本机最近访问（大盘用）
    try {
      const detail = await getTaskDetail(result.task_id)
      rememberTask({
        taskId: detail.task_id,
        taskNo: detail.task_no,
        goal: detail.goal,
        status: detail.status,
      })
    } catch {
      // 详情拉取失败不影响跳转
      rememberTask({
        taskId: result.task_id,
        taskNo: result.task_no,
        goal: form.goal.trim(),
        status: result.status,
      })
    }
    await router.push(`/tasks/${result.task_id}`)
  } catch (error) {
    if (isApiError(error)) {
      if (error.code === 'VALIDATION_ERROR' || error.httpStatus === 422) {
        Message.error(`入参未通过后端校验：${error.message}`)
      } else {
        Message.error(error.message)
      }
    } else {
      Message.error('无法连接后端服务')
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="af-page">
    <div class="af-page-head">
      <div>
        <h1>新建任务</h1>
        <div class="af-sub">用一句业务问题描述目标，Planner 会生成 DAG 子任务计划并逐节点执行</div>
      </div>
    </div>

    <a-row :gutter="12">
      <a-col :span="16">
        <a-card :bordered="true">
          <a-form :model="form" layout="vertical" class="task-form">
            <a-form-item
              label="任务目标"
              :validate-status="errors.goal ? 'error' : undefined"
              :help="errors.goal"
              required
            >
              <a-textarea
                v-model="form.goal"
                placeholder="例如：分析 2026 年 9 月女装类目 GMV 环比下滑原因，输出结论与改进建议"
                :max-length="500"
                show-word-limit
                :auto-size="{ minRows: 5, maxRows: 10 }"
              />
              <div class="goal-hint">
                <span class="af-muted">后端限制 1~500 字，含注入词会被拒绝（validate_goal 做了黑名单校验）</span>
              </div>
            </a-form-item>

            <a-form-item
              label="业务时间范围"
              :validate-status="errors.range ? 'error' : undefined"
              :help="errors.range"
              required
            >
              <div class="inline-row">
                <a-range-picker
                  v-model="form.range"
                  value-format="YYYY-MM-DD"
                  :shortcuts="shortcuts"
                  style="width: 320px"
                />
                <span class="af-muted range-hint">对应后端 `time_range.start` / `time_range.end`（date）</span>
              </div>
            </a-form-item>

            <a-form-item label="执行方式">
              <div class="inline-row">
                <a-switch v-model="form.sync" />
                <span class="sync-hint">
                  同步等待至终态（`sync=true`）
                  <a-tooltip>
                    <template #content>
                      传 true 时请求会阻塞到任务完成或下次挂起，大任务易超时；
                      默认 false，任务在后台执行，页面通过 SSE 观察进度。
                    </template>
                    <icon-question-circle class="af-muted" />
                  </a-tooltip>
                </span>
              </div>
            </a-form-item>

            <a-divider :margin="12" />

            <a-form-item label="高级选项" class="advanced-form-item">
              <!-- Arco 的 FormItem 中 contentFlex 默认 true，内容区会带上 .arco-form-item-content-flex
                   （display:flex 横向排列），多个子项会并排互相挤宽导致文字截断；
                   这里用 :deep 覆盖为块级，再用唯一容器做竖向堆叠 -->
              <div class="advanced-block">
                <div class="advanced">
                  <div class="advanced-item">
                    <a-switch v-model="form.compress" size="small" />
                    <span>上下文压缩</span>
                    <a-tooltip
                      content="开启后规划完成会插入 context_compress 节点，把累积上下文压成摘要供执行/审阅复用；默认关闭走原链路"
                    >
                      <icon-question-circle class="af-muted" />
                    </a-tooltip>
                  </div>
                  <div class="advanced-item">
                    <a-switch v-model="form.useMemory" size="small" />
                    <span>启用分层记忆</span>
                    <a-tooltip
                      content="开启后规划前先做长期记忆召回（最多 3 条），命中并入 Planner / Replan 提示词；召回失败自动降级不影响执行"
                    >
                      <icon-question-circle class="af-muted" />
                    </a-tooltip>
                  </div>
                  <div class="advanced-item">
                    <a-switch v-model="form.evalMark" size="small" />
                    <span>标记为评测样本</span>
                    <a-tooltip content="写入任务级评测标签，供评测页按 eval_tag 关联筛选（≤64 字）">
                      <icon-question-circle class="af-muted" />
                    </a-tooltip>
                  </div>
                </div>

                <div v-if="form.evalMark" class="eval-tag-row">
                  <a-input
                    v-model="form.evalTag"
                    :max-length="EVAL_TAG_MAX"
                    show-word-limit
                    size="small"
                    placeholder="评测标签，例如 campaign-a"
                    :error="Boolean(errors.evalTag)"
                  />
                  <span v-if="errors.evalTag" class="eval-tag-error">{{ errors.evalTag }}</span>
                </div>

                <div class="advanced-note">
                  <icon-info-circle class="advanced-note-icon" />
                  <span>
                    默认全关即走上图实线链路；开启后由 LangGraph 在规划前后分别插入
                    <span class="af-mono">context_compress</span> 与
                    <span class="af-mono">memory_recall</span> 节点。以上三个开关对应
                    <span class="af-mono">TaskCreateRequest</span> 的
                    <span class="af-mono">compress</span> /
                    <span class="af-mono">use_memory</span> /
                    <span class="af-mono">eval_tag</span>。
                  </span>
                </div>
              </div>
            </a-form-item>

            <a-space>
              <a-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="handleSubmit">
                提交并执行
              </a-button>
              <a-button :disabled="submitting" @click="reset">重置</a-button>
            </a-space>
          </a-form>
        </a-card>
      </a-col>

      <a-col :span="8">
        <a-card :bordered="true" title="提交须知" class="side-card">
          <ol class="tips">
            <li>任务创建后立即进入 <b>PLANNING</b>，Planner 生成计划后转 <b>RUNNING</b>。</li>
            <li>命中高危工具（notify 等）会停在 <b>human_gate</b>，状态变为 <b>AWAITING_APPROVAL</b>，需审批后继续。</li>
            <li>执行过程通过 SSE 实时推送，详情页可看 DAG、轨迹与产物。</li>
            <li>Reviewer 判定不合格会触发 <b>replan</b>，重规划次数有上限，耗尽后任务 <b>FAILED</b>。</li>
          </ol>
          <a-divider :margin="12" />
          <div class="af-muted">接口：<span class="af-mono">POST /tasks/stream</span>（仅 admin）</div>
        </a-card>

        <a-card :bordered="true" title="示例目标" class="side-card">
          <div v-for="item in examples" :key="item" class="example" @click="applyExample(item)">
            <icon-swap class="example-icon" />
            <span>{{ item }}</span>
          </div>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<style scoped>
.goal-hint {
  margin-top: 6px;
}

.range-hint {
  font-size: 12px;
}

.sync-hint {
  font-size: 12px;
  color: var(--color-text-2);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

/* Arco 的 FormItem contentFlex 默认 true，内容区固定带 .arco-form-item-content-flex
   （display:flex 横向排列）：控件与提示文字会并排挤宽、长文本被截断。
   本页都是竖向表单，统一覆盖为块级，需要同行的再用 .inline-row 显式包裹 */
.task-form :deep(.arco-form-item-content-flex) {
  display: block;
  width: 100%;
}

/* 需要与控件同处一行的辅助文字 */
.inline-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* 表单项内容区的唯一子项：占满整行，内部各区块竖向堆叠，互不抢宽 */
.advanced-block {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.advanced {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
}

.advanced-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--color-text-2);
}

/* 说明文字单独成行，与上方控件之间用虚线分隔，彻底拉开距离 */
.advanced-note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  width: 100%;
  padding-top: 8px;
  border-top: 1px dashed var(--color-border-2);
  font-size: 11.5px;
  line-height: 19px;
  color: var(--color-text-3);
  white-space: normal;
  word-break: break-word;
}

.advanced-note-icon {
  flex: none;
  margin-top: 3px;
}

.eval-tag-row {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  width: 100%;
  max-width: 360px;
}

/* 输入框占满整行，不再与校验红字抢宽度 */
.eval-tag-row :deep(.arco-input-wrapper) {
  width: 100%;
}

.eval-tag-error {
  font-size: 12px;
  color: rgb(var(--danger-6));
  line-height: 1.5;
}

.side-card {
  margin-bottom: 12px;
}

.tips {
  margin: 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 22px;
  color: var(--color-text-2);
}

.example {
  display: flex;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px dashed var(--color-border-1, #f2f3f5);
  font-size: 12.5px;
  line-height: 19px;
  color: var(--color-text-2);
  cursor: pointer;
}

.example:last-child {
  border-bottom: 0;
}

.example:hover {
  color: rgb(var(--primary-6));
}

.example-icon {
  flex: none;
  margin-top: 2px;
  color: var(--color-text-3);
}
</style>
