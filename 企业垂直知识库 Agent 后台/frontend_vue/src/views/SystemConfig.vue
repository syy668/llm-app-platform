<script setup lang="ts">
// 系统配置页：读取后端运行时配置并支持在线修改（管理员），实时生效无需重启。
// 覆盖项：AI 限流 / 工具超时与重试 / LLM 运行模式与模型参数 / 评测判分模型。
// v1.6：新增「恢复默认」——逐键删除服务端覆盖值，回退 .env / 系统默认值。
// v1.7：新增「运行模式 Provider」下拉（mock 时模型/地址/Key 置灰），
//       并新增「评测判分模型（可选）」卡片——留空即复用对话模型，避免自评偏差。
// v1.8：模型下拉改为由后端 provider_presets 驱动（原来只硬编码 2 个 DeepSeek 模型，
//       后端下发的硅基流动 / 魔搭预设被白白丢弃），并加「服务商预设」一键填充按钮。
import { onMounted, reactive, ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteSystemConfigKey, getSystemConfig, updateSystemConfig } from '@/api/system'
import type { ProviderPreset } from '@/api/system'
import { useUserStore } from '@/store/useUserStore'

const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin)
// 管理员可编辑；普通用户只读
const editable = isAdmin

const loading = ref(false)
const saving = ref(false)
const restoring = ref(false)

// 本页面所管理的配置键：只有这些键的覆盖值才由本页「恢复默认」清理，
// 避免误删本页未展示的配置（Embedding 等）。
const PAGE_KEYS = [
  'ai_rate_limit_per_minute',
  'tool_timeout_seconds',
  'tool_max_attempts',
  'tool_retry_delay_seconds',
  'llm_provider',
  'llm_temperature',
  'llm_model',
  'llm_api_base',
  'llm_api_key',
  'eval_llm_model',
  'eval_llm_api_base',
  'eval_llm_api_key',
] as const
// 服务端返回的「已覆盖」键集合（覆盖值优先于 .env）
const overriddenKeys = ref<string[]>([])
const pageOverrides = computed(() =>
  overriddenKeys.value.filter((k) => (PAGE_KEYS as readonly string[]).includes(k)),
)

// API Key 掩码：后端已配置密钥时返回 ******，前端据此决定占位提示
const SECRET_MASK = '******'
// 用户输入的新 API Key（不参与 dirty 对比，仅在非空时随保存提交）
const apiKeyInput = ref('')
// 评测模型的 API Key（同样只在非空时提交；留空 = 复用对话模型的 Key）
const evalApiKeyInput = ref('')

// 服务端已保存的覆盖值快照，用于对比是否发生修改
const saved = reactive({
  ai_rate_limit_per_minute: 30,
  tool_timeout_seconds: 30,
  tool_max_attempts: 3,
  tool_retry_delay_seconds: 0.5,
  llm_provider: 'mock',
  llm_temperature: 0.2,
  llm_model: 'deepseek-chat',
  llm_api_base: '',
  llm_api_key_set: false,
  eval_llm_model: '',
  eval_llm_api_base: '',
  eval_llm_api_key_set: false,
})

const form = reactive({
  ai_rate_limit_per_minute: 30,
  tool_timeout_seconds: 30,
  tool_max_attempts: 3,
  tool_retry_delay_seconds: 0.5,
  llm_provider: 'mock',
  llm_temperature: 0.2,
  llm_model: 'deepseek-chat',
  llm_api_base: '',
  eval_llm_model: '',
  eval_llm_api_base: '',
})

// 运行模式的语义：mock 下模型名/地址/Key 都不参与调用，置灰避免「填了却不生效」
const isMock = computed(() => form.llm_provider === 'mock')
const providerLabel = computed(
  () => ({ mock: 'Mock', openai: 'OpenAI 兼容', anthropic: 'Anthropic' })[form.llm_provider],
)
const providerHint = computed(() => {
  if (form.llm_provider === 'mock') {
    return '当前运行模式：Mock（离线演示）。如需真实调用，请切换至 OpenAI 兼容模式并配置 Key。'
  }
  if (form.llm_provider === 'openai') {
    return '当前运行模式：OpenAI 兼容接口。保存后对话与评测判分立即真实调用下方模型，无需重启。'
  }
  return '当前运行模式：Anthropic Claude。保存后对话与评测判分立即真实调用下方模型，无需重启。'
})
// 选了真实 provider 但还没有任何可用 Key → 提前提示，避免请求时才报错
const missingKey = computed(
  () => form.llm_provider !== 'mock' && !saved.llm_api_key_set && !apiKeyInput.value.trim(),
)
// 评测模型是否已独立配置（决定卡片上的状态标签与留空提示）
const evalModelConfigured = computed(() => form.eval_llm_model.trim() !== '')

// 【v1.8】后端下发的 OpenAI 兼容服务商预设（只读元数据），用于模型下拉与一键填充。
// 后端不可用 / 老版本不下发时为空数组，此时下拉回退为内置的 DeepSeek 两个模型。
const presets = ref<ProviderPreset[]>([])
// 已知模型的中文注解（仅做展示，未列出的自定义模型名照原样显示）
const MODEL_HINTS: Record<string, string> = {
  'deepseek-chat': '通用对话',
  'deepseek-reasoner': '深度推理',
  'deepseek-ai/DeepSeek-V3': 'DeepSeek-V3 通用',
  'Qwen/Qwen2.5-72B-Instruct': '通义千问 72B',
  // 阿里云百炼（默认关闭思考模式的商业版型号）
  'qwen3-max': '通义旗舰',
  'qwen-plus': '通义均衡（通用首选）',
  'qwen-flash': '通义快速（低价低延迟）',
  'qwen-long': '通义长文本',
}
// 去掉尾部斜杠后比较，避免 https://x/v1 与 https://x/v1/ 认不出是同一家
function normBase(v: string) {
  return v.trim().replace(/\/+$/, '').toLowerCase()
}
// 当前 API Base 命中的预设（用于按钮高亮 + 模型候选优先排序）
const currentPreset = computed<ProviderPreset | null>(() => {
  const cur = normBase(form.llm_api_base)
  if (!cur) return null
  return presets.value.find((p) => normBase(p.llm_api_base) === cur) ?? null
})
// 模型下拉候选：命中预设的模型排前面，再补齐其它预设的模型与内置兜底，
// 最后把「已保存但不在任何预设里」的自定义模型名加回去，否则会显示成空白。
const modelOptions = computed(() => {
  const list: string[] = []
  const push = (m?: string) => {
    const v = (m ?? '').trim()
    if (v && !list.includes(v)) list.push(v)
  }
  currentPreset.value?.llm_models.forEach(push)
  presets.value.forEach((p) => p.llm_models.forEach(push))
  push('deepseek-chat')
  push('deepseek-reasoner')
  push(form.llm_model)
  return list.map((m) => ({ value: m, label: MODEL_HINTS[m] ? `${m}（${MODEL_HINTS[m]}）` : m }))
})

// 【自由输入】模型名的候选建议：只做「打字时的补全」，不限制可填内容。
// 用 el-autocomplete 而不是 el-select(allow-create) 的原因：allow-create 必须先打字
// 才会冒出「新建」项，看上去仍像一个封闭列表，用户根本不知道能自己填；
// autocomplete 本身就是输入框，候选只是建议，随便打什么都算合法值。
function queryModels(query: string, cb: (items: { value: string; hint: string }[]) => void) {
  const kw = query.trim().toLowerCase()
  const matched = modelOptions.value
    .filter(
      (o) =>
        !kw || o.value.toLowerCase().includes(kw) || (MODEL_HINTS[o.value] ?? '').includes(kw),
    )
    .map((o) => ({ value: o.value, hint: MODEL_HINTS[o.value] ?? '' }))
  // 一个候选都没命中 → 追加「自定义」项，明确告诉用户「这个名字会直接生效」。
  // 只在零命中时追加：否则用户打「通义」看到 4 个候选的同时还会冒出个叫「通义」的模型名。
  if (kw && !matched.length) {
    matched.push({ value: query.trim(), hint: '自定义（按此名直接调用）' })
  }
  cb(matched)
}

// 模型名与接口地址不同源 → 强烈提示（不拦截保存，自由输入是刚需）。
// 线上真实故障：把硅基流动的 Qwen/Qwen2.5-14B-Instruct 填到百炼地址上，调用直接 404
// model_not_found，而报错来自模型服务、用户只能猜。这里只做「名字属于别家预设」的判定：
// 完全自定义的名字（qwen3.8-max、自部署模型名）不报，避免把正常用法误标成错误。
const modelProviderMismatch = computed(() => {
  const cur = currentPreset.value
  const m = form.llm_model.trim().toLowerCase()
  if (!cur || !m) return null
  if (cur.llm_models.some((x) => x.toLowerCase() === m)) return null
  const owner = presets.value.find(
    (p) => p.id !== cur.id && p.llm_models.some((x) => x.toLowerCase() === m),
  )
  return owner ? { current: cur, owner } : null
})

// 云厂商控制台常给「模板地址」：https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/...
// 占位符没替换就保存 → 域名解析失败 → SDK 只抛 "Connection error."，
// 对话页显示「AI 服务连接超时，请检查网络」，完全指不到真因。后端保存时已拦，
// 这里提前把占位符点到用户脸上，避免「填了、存了、还是不通」的一轮排查。
const apiBasePlaceholder = computed(() => {
  const m = /\{[^}]{1,64}\}/.exec(form.llm_api_base || '')
  return m ? m[0] : ''
})

// 一键填充：切到 OpenAI 兼容模式，写入该厂商 API Base，模型名不在候选里则取首个推荐模型。
// 只在本地改表单，仍需点「保存」才落库——避免误点就改掉线上配置。
function applyPreset(p: ProviderPreset) {
  if (!editable.value) return
  form.llm_provider = 'openai'
  form.llm_api_base = p.llm_api_base
  if (!p.llm_models.includes(form.llm_model)) {
    form.llm_model = p.llm_models[0] ?? form.llm_model
  }
  ElMessage.success(`已填充「${p.name}」的接口地址与推荐模型，记得点保存生效`)
}

function applyConfig(data: Record<string, unknown>) {
  form.ai_rate_limit_per_minute = Number(data.ai_rate_limit_per_minute ?? 30)
  form.tool_timeout_seconds = Number(data.tool_timeout_seconds ?? 30)
  form.tool_max_attempts = Number(data.tool_max_attempts ?? 3)
  form.tool_retry_delay_seconds = Number(data.tool_retry_delay_seconds ?? 0.5)
  form.llm_provider = String(data.llm_provider ?? 'mock')
  form.llm_temperature = Number(data.llm_temperature ?? 0.2)
  form.llm_model = String(data.llm_model ?? 'deepseek-chat')
  form.llm_api_base = String(data.llm_api_base ?? '')
  form.eval_llm_model = String(data.eval_llm_model ?? '')
  form.eval_llm_api_base = String(data.eval_llm_api_base ?? '')
  saved.ai_rate_limit_per_minute = form.ai_rate_limit_per_minute
  saved.tool_timeout_seconds = form.tool_timeout_seconds
  saved.tool_max_attempts = form.tool_max_attempts
  saved.tool_retry_delay_seconds = form.tool_retry_delay_seconds
  saved.llm_provider = form.llm_provider
  saved.llm_temperature = form.llm_temperature
  saved.llm_model = form.llm_model
  saved.llm_api_base = form.llm_api_base
  saved.llm_api_key_set = data.llm_api_key === SECRET_MASK
  saved.eval_llm_model = form.eval_llm_model
  saved.eval_llm_api_base = form.eval_llm_api_base
  saved.eval_llm_api_key_set = data.eval_llm_api_key === SECRET_MASK
  apiKeyInput.value = ''
  evalApiKeyInput.value = ''
  overriddenKeys.value = Array.isArray(data.overridden_keys)
    ? (data.overridden_keys as string[])
    : []
  // 【v1.8】服务商预设（只读元数据）：拿不到就留空，下拉自动回退到内置 DeepSeek 两项
  presets.value = Array.isArray(data.provider_presets)
    ? (data.provider_presets as ProviderPreset[])
    : []
}

async function load() {
  loading.value = true
  try {
    const data = await getSystemConfig()
    applyConfig(data as unknown as Record<string, unknown>)
  } finally {
    loading.value = false
  }
}

const dirty = computed(
  () =>
    form.ai_rate_limit_per_minute !== saved.ai_rate_limit_per_minute ||
    form.tool_timeout_seconds !== saved.tool_timeout_seconds ||
    form.tool_max_attempts !== saved.tool_max_attempts ||
    form.tool_retry_delay_seconds !== saved.tool_retry_delay_seconds ||
    form.llm_provider !== saved.llm_provider ||
    form.llm_temperature !== saved.llm_temperature ||
    form.llm_model !== saved.llm_model ||
    form.llm_api_base !== saved.llm_api_base ||
    form.eval_llm_model !== saved.eval_llm_model ||
    form.eval_llm_api_base !== saved.eval_llm_api_base ||
    apiKeyInput.value.trim() !== '' ||
    evalApiKeyInput.value.trim() !== '',
)

async function save() {
  // 占位符地址不可能可用，先在本地拦一次（后端也会 400，这里只是省一次往返）
  if (apiBasePlaceholder.value) {
    ElMessage.warning(
      `接口地址里的 ${apiBasePlaceholder.value} 是占位符，请替换成真实值再保存`,
    )
    return
  }
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      ai_rate_limit_per_minute: form.ai_rate_limit_per_minute,
      tool_timeout_seconds: form.tool_timeout_seconds,
      tool_max_attempts: form.tool_max_attempts,
      tool_retry_delay_seconds: form.tool_retry_delay_seconds,
      llm_provider: form.llm_provider,
      llm_temperature: form.llm_temperature,
      llm_model: form.llm_model,
      llm_api_base: form.llm_api_base,
      // 评测模型：空串提交 = 清除覆盖值（回退复用对话模型），后端已按此语义处理
      eval_llm_model: form.eval_llm_model.trim(),
      eval_llm_api_base: form.eval_llm_api_base.trim(),
    }
    // 仅在用户输入了新密钥时提交（后端对空值 / 掩码视为未修改）
    if (apiKeyInput.value.trim()) payload.llm_api_key = apiKeyInput.value.trim()
    if (evalApiKeyInput.value.trim()) payload.eval_llm_api_key = evalApiKeyInput.value.trim()
    const data = await updateSystemConfig(payload)
    applyConfig(data as unknown as Record<string, unknown>)
    ElMessage.success('配置已保存并实时生效，无需重启服务')
  } finally {
    saving.value = false
  }
}

async function restoreDefaults() {
  const targets = [...pageOverrides.value]
  if (!targets.length) return
  try {
    await ElMessageBox.confirm(
      `将删除以下 ${targets.length} 项的服务端覆盖值，恢复为 .env / 系统默认值：\n\n${targets.join('\n')}`,
      '恢复默认配置',
      { type: 'warning', confirmButtonText: '确认恢复', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  restoring.value = true
  try {
    let latest: Record<string, unknown> | null = null
    // 逐键删除（后端 DELETE /system/config/{key} 幂等，失败会抛错中断）
    for (const key of targets) {
      latest = (await deleteSystemConfigKey(key)) as unknown as Record<string, unknown>
    }
    if (latest) applyConfig(latest)
    ElMessage.success(`已恢复 ${targets.length} 项默认值（现取自 .env）`)
  } finally {
    restoring.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-head">
      <div>
        <div class="page-title">系统配置</div>
        <div class="page-desc">AI 接口限流、工具重试 / 超时、运行模式与 LLM / 评测判分模型在线配置，保存后实时生效</div>
      </div>
      <div class="head-actions">
        <el-button
          v-if="editable"
          :loading="restoring"
          :disabled="pageOverrides.length === 0"
          @click="restoreDefaults"
        >
          恢复默认<span v-if="pageOverrides.length">（{{ pageOverrides.length }}）</span>
        </el-button>
        <el-button v-if="editable" type="primary" :loading="saving" :disabled="!dirty" @click="save">
          保存配置
        </el-button>
      </div>
    </div>

    <!-- 提示条 -->
    <el-alert
      :type="editable ? 'info' : 'warning'"
      show-icon
      :closable="false"
      :title="
        editable
          ? '配置持久化在服务端 data/system_config.json，保存后立即生效，无需重启服务'
          : '仅管理员可修改配置，当前为只读查看模式'
      "
      style="margin-bottom: 16px"
    />

    <div v-if="pageOverrides.length" class="small muted" style="margin: -8px 0 16px">
      当前有 {{ pageOverrides.length }} 项使用「服务端覆盖值」（优先于 .env）：{{ pageOverrides.join('、') }}
      —— 点击「恢复默认」可删除覆盖值，回退读取 .env 配置。
    </div>

    <div class="cfg-grid">
      <div class="card">
        <div class="card-header"><span class="card-title">🤖 AI 接口限流</span></div>
        <div class="card-body">
          <el-form label-position="top" :disabled="!editable">
            <el-form-item label="每用户每分钟请求上限（次/分钟）">
              <el-input-number v-model="form.ai_rate_limit_per_minute" :min="1" :max="10000" style="width: 100%" />
            </el-form-item>
          </el-form>
          <div class="small muted">令牌桶按用户限流（Redis 原子执行），超过阈值返回 429 并提示稍后重试，防止 AI 接口被过度调用。</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><span class="card-title">🔁 工具重试与超时</span></div>
        <div class="card-body">
          <el-form label-position="top" :disabled="!editable">
            <el-row :gutter="16">
              <el-col :span="8">
                <el-form-item label="工具执行超时（秒）">
                  <el-input-number v-model="form.tool_timeout_seconds" :min="5" :max="300" style="width: 100%" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="最大尝试次数">
                  <el-input-number v-model="form.tool_max_attempts" :min="1" :max="10" style="width: 100%" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="重试间隔（秒）">
                  <el-input-number v-model="form.tool_retry_delay_seconds" :min="0" :max="60" :step="0.5" style="width: 100%" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <div class="small muted">工具失败后按重试间隔自动重试（每次失败写入 Trace 链路日志），超过次数终止；单次执行超过超时时间将被中断。</div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top: 16px">
      <div class="card-header">
        <span class="card-title">🧠 LLM 模型参数</span>
        <span class="ui-tag tag-gray">对话 + 评测共用默认</span>
      </div>
      <div class="card-body">
        <el-form label-position="top" :disabled="!editable">
          <el-form-item label="运行模式 Provider">
            <el-select v-model="form.llm_provider" style="width: 100%; max-width: 380px">
              <el-option label="mock — 离线演示（内置规则模型）" value="mock" />
              <el-option
                label="openai — OpenAI 兼容接口（DeepSeek / 硅基流动 / 自建）"
                value="openai"
              />
              <el-option label="anthropic — Claude" value="anthropic" />
            </el-select>
            <div class="small muted" style="margin-top: 4px">
              决定请求走哪条链路。mock 直接用内置规则模型，下方模型 / 地址 / Key 均不参与调用。
            </div>
          </el-form-item>
        </el-form>

        <!-- 运行模式状态说明：把「为什么填了模型名却不生效」讲清楚 -->
        <el-alert
          :type="isMock ? 'warning' : 'success'"
          :closable="false"
          show-icon
          :title="providerHint"
          style="margin-bottom: 12px"
        />
        <el-alert
          v-if="missingKey"
          type="error"
          :closable="false"
          show-icon
          title="已选择真实模型模式但还没有可用的 API Key：请填写下方 API Key（或先切回 mock），否则对话与评测请求会直接报错。"
          style="margin-bottom: 12px"
        />

        <div v-if="presets.length" class="preset-bar">
          <span class="small muted">服务商预设：</span>
          <el-button
            v-for="p in presets"
            :key="p.id"
            size="small"
            :type="currentPreset?.id === p.id ? 'primary' : 'default'"
            :plain="currentPreset?.id !== p.id"
            :disabled="!editable"
            @click="applyPreset(p)"
          >
            {{ p.name }}
          </el-button>
          <span class="small muted preset-tip">一键填充接口地址与推荐模型，填充后仍可手动改写</span>
        </div>

        <el-form label-position="top" :disabled="!editable">
          <el-row :gutter="16">
            <el-col :span="6">
              <el-form-item label="模型">
                <el-autocomplete
                  v-model="form.llm_model"
                  :fetch-suggestions="queryModels"
                  :trigger-on-focus="true"
                  clearable
                  :disabled="isMock"
                  placeholder="选择候选或直接输入模型名"
                  style="width: 100%"
                >
                  <template #default="{ item }">
                    <span>{{ item.value }}</span>
                    <span v-if="item.hint" class="small muted" style="margin-left: 6px">{{ item.hint }}</span>
                  </template>
                </el-autocomplete>
                <div class="small muted" style="margin-top: 4px">
                  可直接输入任意模型名（如 qwen3.8-max、自部署模型），不受上方预设限制
                </div>
                <div
                  v-if="modelProviderMismatch"
                  class="small model-mismatch"
                  style="margin-top: 2px"
                >
                  该模型名属于「{{ modelProviderMismatch.owner.name }}」，但当前接口地址是「{{
                    modelProviderMismatch.current.name
                  }}」——保存后调用会报「model not exist」。请改填后者的模型名（{{
                    modelProviderMismatch.current.llm_models.join(' / ')
                  }}），或点上方的「{{ modelProviderMismatch.owner.name }}」连地址一起切过去。
                </div>
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="Temperature">
                <el-input-number
                  v-model="form.llm_temperature"
                  :min="0"
                  :max="2"
                  :step="0.05"
                  :disabled="isMock"
                  style="width: 100%"
                />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="接口地址 API Base">
                <el-input
                  v-model="form.llm_api_base"
                  :disabled="isMock"
                  placeholder="默认 https://api.deepseek.com/v1"
                  clearable
                />
                <div v-if="apiBasePlaceholder" class="small" style="margin-top: 4px; color: var(--warning)">
                  地址里的 {{ apiBasePlaceholder }} 是占位符，需替换成真实值（否则保存会被拒绝，调用也必然失败）
                </div>
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="API Key">
                <el-input
                  v-model="apiKeyInput"
                  type="password"
                  show-password
                  :disabled="isMock"
                  :placeholder="saved.llm_api_key_set ? '已配置，留空保持不变' : '未配置，请输入 API Key'"
                  autocomplete="new-password"
                />
              </el-form-item>
            </el-col>
          </el-row>
        </el-form>
        <div class="small muted">
          支持任意 OpenAI 兼容接口（DeepSeek / 通义千问 / Kimi / vLLM 自部署等）；API Key 保存后不再回显（仅显示是否已配置）。
          修改运行模式 / 模型 / 温度 / 地址 / 密钥后，下一次 LLM 调用自动生效，无需重启。对话页的检索条数与工具开关为请求级参数，在「智能对话」页右侧调整。
        </div>
      </div>
    </div>

    <!-- v1.7 新增：评测判分模型（可选），留空即复用上方对话模型 -->
    <div class="card" style="margin-top: 16px">
      <div class="card-header">
        <span class="card-title">🧪 评测判分模型（可选）</span>
        <span class="ui-tag" :class="evalModelConfigured ? 'tag-green' : 'tag-gray'">
          {{ evalModelConfigured ? '独立判分模型' : '复用对话模型' }}
        </span>
      </div>
      <div class="card-body">
        <div class="small muted" style="margin-bottom: 10px">
          留空时默认复用上方对话模型（与改造前行为一致）。填写后 RAGAS 判分改用独立模型，避免「生成与评分是同一个模型」的自评偏差；
          接口地址与 Key 可只填一项，未填的那项自动回退上方配置。
        </div>
        <el-form label-position="top" :disabled="!editable">
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="判分模型">
                <el-autocomplete
                  v-model="form.eval_llm_model"
                  :fetch-suggestions="queryModels"
                  :trigger-on-focus="true"
                  clearable
                  placeholder="留空 = 复用上方对话模型"
                  style="width: 100%"
                >
                  <template #default="{ item }">
                    <span>{{ item.value }}</span>
                    <span v-if="item.hint" class="small muted" style="margin-left: 6px">{{ item.hint }}</span>
                  </template>
                </el-autocomplete>
                <div class="small muted" style="margin-top: 4px">可输入任意模型名；留空即复用对话模型</div>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="判分接口地址 API Base">
                <el-input v-model="form.eval_llm_api_base" placeholder="留空 = 复用上方 API Base" clearable />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="判分 API Key">
                <el-input
                  v-model="evalApiKeyInput"
                  type="password"
                  show-password
                  :placeholder="saved.eval_llm_api_key_set ? '已配置，留空保持不变' : '留空 = 复用上方 API Key'"
                  autocomplete="new-password"
                />
              </el-form-item>
            </el-col>
          </el-row>
        </el-form>
        <div class="small muted">
          判分温度固定 0.0（保证同一份答案可复现），不读上方 Temperature；超时与重试沿用 EVAL_LLM_TIMEOUT / EVAL_LLM_MAX_RETRIES。
          <span v-if="isMock">当前为 Mock 模式，模型名不参与调用，独立判分模型需切换到真实 Provider 后才会真正生效。</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cfg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.head-actions { display: flex; gap: 8px; flex-shrink: 0; }
.preset-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 0 0 12px; }
/* 说明文字独占一行：预设多了（4 家）之后，否则会挤到第二行与「模型」标签贴在一起 */
.preset-bar .preset-tip { flex-basis: 100%; line-height: 18px; }
/* 模型名与接口地址不同源时的黄色提示：只提醒，不禁用保存 */
.model-mismatch { color: var(--el-color-warning); line-height: 18px; }
</style>
