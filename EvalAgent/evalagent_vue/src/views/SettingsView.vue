<script setup lang="ts">
/**
 * 系统设置页：
 * - 上游 API 基址展示与本地覆盖（仅存浏览器 localStorage，无任何写后端配置能力）
 * - 模型服务配置（仅管理员）：调上游已有 GET/PUT /system/config 热更新 LLM key/model，
 *   实时生效无需重启；普通用户整卡隐藏
 * - 当前登录账号信息（用户名 / 角色），来自 /auth/me
 * - 跳转项目一登录页按钮；备用：手动粘贴 token
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { DEFAULT_API_BASE, getApiBase, persistApiBase, resetApiBase } from '@/api/http'
import { fetchMe } from '@/api/auth'
import { getSystemConfig, updateSystemConfig } from '@/api/system'
import { getJudgeConfig, preflightJudgeConfig, updateJudgeConfig } from '@/api/system-judge'
import { useAuthStore } from '@/store/auth'
import type { JudgeConfigView, SystemConfig } from '@/api/types'

const router = useRouter()
// 【双Token改造】退出登录改用 logout（后端登出 + 清空双 token + 跳转登录）
const { state, isAdmin, setUser, setTokenOnly, logout } = useAuthStore()

// ---------- API 地址 ----------
const apiBaseInput = ref(getApiBase())

function saveApiBase() {
  const url = apiBaseInput.value.trim().replace(/\/+$/, '')
  if (!/^https?:\/\//.test(url)) {
    ElMessage.error('请输入合法的 http(s) 地址')
    return
  }
  persistApiBase(url)
  apiBaseInput.value = url
  ElMessage.success('API 地址已保存到浏览器本地')
}

function restoreDefault() {
  resetApiBase()
  apiBaseInput.value = getApiBase()
  ElMessage.success('已恢复默认地址')
}

// ---------- 账号 ----------
const meLoading = ref(false)

async function refreshMe() {
  meLoading.value = true
  try {
    const user = await fetchMe()
    setUser(user)
    ElMessage.success('账号信息已刷新')
  } finally {
    meLoading.value = false
  }
}

function goExternalLogin() {
  window.open(import.meta.env.VITE_LOGIN_URL, '_blank')
}

/** 【双Token改造】退出登录：调用后端登出（refresh_token 入 Redis 黑名单踢下线）+ 清空本地双 token */
async function logoutLocal() {
  await logout()
  router.push({ name: 'login' })
}

// ---------- 备用：手动粘贴 token ----------
const tokenInput = ref('')

function applyToken() {
  const token = tokenInput.value.trim()
  if (!token) {
    ElMessage.warning('请粘贴 token')
    return
  }
  setTokenOnly(token)
  tokenInput.value = ''
  void refreshMe()
}

// ---------- 模型服务配置（管理员，调上游 /system/config 热更新） ----------
const MASK = '******'
const cfgLoading = ref(false)
const cfgSaving = ref(false)
const cfg = ref<SystemConfig | null>(null)

// 表单：仅这三个可编辑项；key 留空表示不修改
const llmModelInput = ref('')
const llmApiBaseInput = ref('')
const llmTemperatureInput = ref<number | null>(null)
const llmKeyInput = ref('')

async function loadConfig() {
  if (!isAdmin.value) return
  cfgLoading.value = true
  try {
    cfg.value = await getSystemConfig()
    llmModelInput.value = cfg.value.llm_model || ''
    llmApiBaseInput.value = cfg.value.llm_api_base || ''
    llmTemperatureInput.value = cfg.value.llm_temperature
    llmKeyInput.value = ''
  } finally {
    cfgLoading.value = false
  }
}

async function saveConfig() {
  const payload: Record<string, string | number> = {}
  const model = llmModelInput.value.trim()
  const apiBase = llmApiBaseInput.value.trim()
  const key = llmKeyInput.value.trim()

  if (!model) {
    ElMessage.warning('模型名不能为空')
    return
  }
  if (model !== cfg.value?.llm_model) payload.llm_model = model
  if (apiBase !== (cfg.value?.llm_api_base || '')) payload.llm_api_base = apiBase
  if (key) {
    if (key === MASK) {
      ElMessage.warning('API Key 请填入真实密钥（留空表示不修改）')
      return
    }
    payload.llm_api_key = key
  }
  const temp = llmTemperatureInput.value
  if (temp !== null && temp !== undefined && temp !== (cfg.value?.llm_temperature ?? null)) {
    if (temp < 0 || temp > 2) {
      ElMessage.warning('采样温度取值范围 0 ~ 2')
      return
    }
    payload.llm_temperature = temp
  }
  if (Object.keys(payload).length === 0) {
    ElMessage.info('配置未变化，无需保存')
    return
  }

  cfgSaving.value = true
  try {
    cfg.value = await updateSystemConfig(payload)
    llmModelInput.value = cfg.value.llm_model || ''
    llmApiBaseInput.value = cfg.value.llm_api_base || ''
    llmTemperatureInput.value = cfg.value.llm_temperature
    llmKeyInput.value = ''
    ElMessage.success('配置已保存并实时生效（无需重启服务）')
  } finally {
    cfgSaving.value = false
  }
}

// ---------- 裁判模型配置（考官 · 本中台 8200）：保存即探活，通过才落库 ----------
const judgeCfg = ref<JudgeConfigView | null>(null)
const judgeLoading = ref(false)
const judgeSaving = ref(false)
const judgeTesting = ref(false)
const judgeError = ref('')
const judgeModelInput = ref('')
const judgeApiBaseInput = ref('')
const judgeEmbeddingInput = ref('')
const judgeEmbBaseInput = ref('')
const judgeEmbKeyInput = ref('')
const judgeKeyInput = ref('')

async function loadJudgeConfig() {
  if (!isAdmin.value) return
  judgeLoading.value = true
  judgeError.value = ''
  try {
    judgeCfg.value = await getJudgeConfig()
    judgeModelInput.value = judgeCfg.value.model || ''
    judgeApiBaseInput.value = judgeCfg.value.api_base || ''
    judgeEmbeddingInput.value = judgeCfg.value.embedding_model || ''
    judgeEmbBaseInput.value = judgeCfg.value.embedding_api_base || ''
    judgeEmbKeyInput.value = ''
    judgeKeyInput.value = ''
  } catch (e) {
    judgeError.value = (e as Error).message || '读取裁判配置失败'
  } finally {
    judgeLoading.value = false
  }
}

async function saveJudgeConfig() {
  const model = judgeModelInput.value.trim()
  const apiBase = judgeApiBaseInput.value.trim()
  const embedding = judgeEmbeddingInput.value.trim()
  const embBase = judgeEmbBaseInput.value.trim()
  const embKey = judgeEmbKeyInput.value.trim()
  const key = judgeKeyInput.value.trim()

  if (!model) {
    ElMessage.warning('裁判模型名不能为空')
    return
  }
  if (apiBase && !/^https?:\/\//.test(apiBase)) {
    ElMessage.warning('接口地址必须以 http:// 或 https:// 开头')
    return
  }
  if (key === MASK) {
    ElMessage.warning('API Key 请填真实密钥（留空表示不修改）')
    return
  }
  if (embBase && !/^https?:\/\//.test(embBase)) {
    ElMessage.warning('向量模型接口地址必须以 http:// 或 https:// 开头')
    return
  }
  if (embKey === MASK) {
    ElMessage.warning('向量 API Key 请填真实密钥（留空表示不修改）')
    return
  }

  const payload: Record<string, string> = {}
  if (model !== (judgeCfg.value?.model || '')) payload.model = model
  if (apiBase !== (judgeCfg.value?.api_base || '')) payload.api_base = apiBase
  if (embedding && embedding !== (judgeCfg.value?.embedding_model || '')) payload.embedding_model = embedding
  if (key) payload.api_key = key
  // 向量地址要能"清空"（回落同源），所以只要与当前值不同就提交，哪怕是空串
  if (embBase !== (judgeCfg.value?.embedding_api_base || '')) payload.embedding_api_base = embBase
  if (embKey) payload.embedding_api_key = embKey

  if (Object.keys(payload).length === 0) {
    ElMessage.info('配置未变化，无需保存')
    return
  }

  judgeSaving.value = true
  judgeError.value = ''
  try {
    // 后端流程：候选配置 → 结构探活 + 向量探活 → 通过才写库；失败返回 422 且不落库
    const res = await updateJudgeConfig(payload)
    judgeCfg.value = res.config
    judgeKeyInput.value = ''
    judgeEmbKeyInput.value = ''
    ElMessage.success(
      `配置已保存并实时生效（结构+向量探活通过 ${res.preflight?.latency_ms ?? '-'}ms，无需重启服务）`,
    )
  } catch (e) {
    // 探活失败：把后端原因留在卡片里，避免一闪而过的 toast 看不到根因
    judgeError.value = (e as Error).message || '保存失败（配置未落库）'
    ElMessage.error(judgeError.value)
  } finally {
    judgeSaving.value = false
  }
}

async function testJudge() {
  judgeTesting.value = true
  judgeError.value = ''
  try {
    const res = await preflightJudgeConfig()
    if (res.ok) {
      ElMessage.success(`结构探活通过（${res.latency_ms ?? '-'}ms）`)
    } else {
      judgeError.value = res.message
      ElMessage.error(res.message)
    }
  } catch (e) {
    judgeError.value = (e as Error).message || '探活失败'
  } finally {
    judgeTesting.value = false
  }
}

/**
 * 探活失败的两种性质必须分开提示：
 * - 不支持结构化输出 → 换模型（不是配置填错，改 base_url/key 都没用）；
 * - 其它（空 content / 401 / 网络）→ 按原文案排查。
 */
const judgeCompatError = computed(
  () => judgeError.value.includes('结构化输出') || judgeError.value.includes('有效解析结果'),
)

onMounted(() => {
  apiBaseInput.value = getApiBase()
  void loadConfig()
  void loadJudgeConfig()
})
</script>

<template>
  <div class="ea-content">
    <div class="ea-page-header">
      <div class="ea-page-title">系统设置</div>
      <div class="ea-page-sub">评测服务地址仅保存在浏览器本地；「裁判模型配置」落本中台数据库（保存即探活），「问答模型配置」写上游知识库问答系统</div>
    </div>

    <div class="settings-grid">
      <!-- 左列：上游 API + 账号信息 -->
      <div class="settings-col">
      <!-- 上游 API -->
      <div class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">评测服务地址</div>
        </div>
        <div class="ea-card-body">
          <div class="field-label">VITE_EVAL_API_BASE_URL（默认，来自环境变量）</div>
          <div class="ea-mono default-url">{{ DEFAULT_API_BASE }}</div>
          <div class="field-label" style="margin-top: 14px">当前生效地址（可本地覆盖）</div>
          <div style="display: flex; gap: 8px">
            <el-input v-model="apiBaseInput" placeholder="http://127.0.0.1:8200/api/v1" />
            <el-button type="primary" @click="saveApiBase">保存</el-button>
            <el-button @click="restoreDefault">恢复默认</el-button>
          </div>
          <el-alert
            style="margin-top: 12px"
            type="info"
            :closable="false"
            show-icon
            title="评测域走本中台 8200（/eval/*）；身份与知识库目录仍走知识库问答系统 8010（/auth/*、/kbs、/system/config）"
          />
        </div>
      </div>

      <!-- 账号信息 -->
      <div class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">当前账号</div>
          <el-button link type="primary" :loading="meLoading" @click="refreshMe">刷新</el-button>
        </div>
        <div class="ea-card-body">
          <template v-if="state.user">
            <div class="account-row"><span class="account-label">用户名</span><span>{{ state.user.username }}</span></div>
            <div class="account-row"><span class="account-label">昵称</span><span>{{ state.user.display_name || '—' }}</span></div>
            <div class="account-row">
              <span class="account-label">角色</span>
              <el-tag :type="isAdmin ? 'primary' : 'info'" size="small">
                {{ isAdmin ? '管理员 admin' : `普通用户 ${state.user.role}` }}
              </el-tag>
            </div>
            <div class="account-row"><span class="account-label">邮箱</span><span>{{ state.user.email || '—' }}</span></div>
            <div class="account-row"><span class="account-label">状态</span><span>{{ state.user.status }}</span></div>
          </template>
          <el-empty v-else description="未获取到账号信息" :image-size="80" />

          <div style="margin-top: 16px; display: flex; gap: 8px">
            <el-button @click="goExternalLogin">前往知识库问答系统登录页</el-button>
            <el-button type="danger" plain @click="logoutLocal">退出登录（清除本地凭据）</el-button>
          </div>
        </div>
      </div>
      </div>

      <!-- 右列：裁判配置 + 问答模型 + 备用 token -->
      <div class="settings-col">
      <!-- 裁判模型配置（考官）：本中台自有，保存即探活，通过才落库 -->
      <div v-if="isAdmin" class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">
            裁判模型配置
          </div>
          <div>
            <el-button link type="primary" :loading="judgeTesting" @click="testJudge">探活</el-button>
            <el-button link type="primary" :loading="judgeLoading" @click="loadJudgeConfig">刷新</el-button>
          </div>
        </div>
        <div class="ea-card-body" v-loading="judgeLoading">
          <el-alert
            style="margin-bottom: 12px"
            type="success"
            :closable="false"
            show-icon
            title="本中台自有配置（落 runtime_configs 表）：保存前先探活，通过才写库并热生效，无需重启 8200"
          />
          <el-alert
            v-if="judgeError"
            style="margin-bottom: 12px"
            type="error"
            :closable="false"
            show-icon
            :title="`配置未生效：${judgeError}`"
          >
            <template v-if="judgeCompatError" #default>
              <div style="line-height: 1.7">
                该模型能聊天，但<strong>吐不出 RAGAS 需要的 JSON 结构</strong>——探活已拦下，配置未落库。<br />
                请换一个支持结构化输出的模型（如 <code>deepseek-v4.1-flash</code> / <code>qwen3.8-flash</code>），
                改 base_url 或 Key 都解决不了这个问题。
              </div>
            </template>
          </el-alert>

          <div class="field-label">裁判模型名（RAGAS 打分用）</div>
          <el-input v-model="judgeModelInput" placeholder="deepseek-v4.1-flash-1017" style="margin-top: 4px" />
          <div class="ea-hint">{{ judgeCfg?.descriptions?.JUDGE_MODEL }}</div>

          <div class="field-label" style="margin-top: 12px">接口地址（OpenAI 兼容 base_url）</div>
          <el-input
            v-model="judgeApiBaseInput"
            placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1"
            style="margin-top: 4px"
          />

          <div class="field-label" style="margin-top: 12px">
            API Key（{{ judgeCfg?.api_key_configured ? '已配置，留空表示不修改' : '未配置' }}）
          </div>
          <el-input
            v-model="judgeKeyInput"
            type="password"
            show-password
            :placeholder="judgeCfg?.api_key_configured ? `当前：${judgeCfg.api_key}（填写新值以更换）` : 'sk-...'"
            style="margin-top: 4px"
          />

          <div class="field-label" style="margin-top: 12px">向量模型（answer_relevancy 用；建议与知识库问答系统检索同模型）</div>
          <el-input v-model="judgeEmbeddingInput" placeholder="BAAI/bge-m3" style="margin-top: 4px" />
          <div class="ea-hint">{{ judgeCfg?.descriptions?.JUDGE_EMBEDDING_MODEL }}</div>

          <div class="field-label" style="margin-top: 12px">
            向量接口地址
            <el-tag v-if="judgeCfg?.embedding_shares_judge_endpoint" size="small" type="info" effect="plain" style="margin-left: 6px">
              与裁判同源
            </el-tag>
          </div>
          <el-input
            v-model="judgeEmbBaseInput"
            placeholder="留空 = 沿用裁判接口地址；bge-m3 填 https://api.siliconflow.cn/v1"
            style="margin-top: 4px"
          />
          <div class="ea-hint">
            当前生效：<span class="ea-mono">{{ judgeCfg?.effective_embedding_api_base || '—' }}</span>
            —— 向量服务商和裁判 LLM 不同时必须填，否则请求会打到裁判那边 404
          </div>

          <div class="field-label" style="margin-top: 12px">
            向量 API Key（{{ judgeCfg?.embedding_api_key_configured ? '已单独配置，留空表示不修改' : '未单独配置，沿用裁判 Key' }}）
          </div>
          <el-input
            v-model="judgeEmbKeyInput"
            type="password"
            show-password
            placeholder="留空 = 沿用裁判 API Key"
            style="margin-top: 4px"
          />
          <div class="ea-hint">{{ judgeCfg?.descriptions?.JUDGE_EMBEDDING_API_KEY }}</div>

          <div class="field-label" style="margin-top: 12px">
            裁判温度
            <el-tag size="small" type="info" effect="plain" style="margin-left: 6px">已锁定</el-tag>
          </div>
          <el-input :model-value="'0.0'" disabled style="margin-top: 4px; max-width: 120px" />
          <div class="ea-hint">为保证评测结果的可复现性，裁判温度强制锁定为 0</div>

          <div style="margin-top: 16px; display: flex; gap: 8px; align-items: center">
            <el-button type="primary" :loading="judgeSaving" @click="saveJudgeConfig">保存并探活</el-button>
            <span v-if="judgeCfg?.updated_at" class="ea-hint" style="margin: 0">
              生效于 {{ judgeCfg.updated_at }}<template v-if="judgeCfg?.updated_by">（{{ judgeCfg.updated_by }}）</template>
            </span>
            <span v-else class="ea-hint" style="margin: 0">当前沿用 .env 配置</span>
          </div>
        </div>
      </div>

      <!-- 模型服务配置（仅管理员）：热更新，实时生效 -->
      <div v-if="isAdmin" class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">
            问答模型配置
          </div>
          <el-button link type="primary" :loading="cfgLoading" @click="loadConfig">刷新</el-button>
        </div>
        <div class="ea-card-body" v-loading="cfgLoading">
          <el-alert
            style="margin-bottom: 12px"
            type="info"
            :closable="false"
            show-icon
            title="调用知识库问答系统配置接口热更新：改的是被评测的问答模型，不影响上面裁判的判分口径"
          />
          <div class="field-label">模型名（任意 OpenAI 兼容模型，如 deepseek-chat / qwen-max）</div>
          <el-input v-model="llmModelInput" placeholder="deepseek-chat" style="margin-top: 4px" />
          <div class="field-label" style="margin-top: 12px">接口地址（OpenAI 兼容 base_url，留空用服务端默认）</div>
          <el-input v-model="llmApiBaseInput" placeholder="https://api.deepseek.com/v1" style="margin-top: 4px" />
          <div class="field-label" style="margin-top: 12px">API Key（{{ cfg?.llm_api_key ? '已配置，留空表示不修改' : '未配置' }}）</div>
          <el-input
            v-model="llmKeyInput"
            type="password"
            show-password
            :placeholder="cfg?.llm_api_key ? `当前：${cfg.llm_api_key}（填写新值以更换）` : 'sk-...'"
            style="margin-top: 4px"
          />
          <div class="field-label" style="margin-top: 12px">采样温度（0 ~ 2）</div>
          <el-input-number v-model="llmTemperatureInput" :min="0" :max="2" :step="0.1" style="margin-top: 4px" />
          <div style="margin-top: 16px">
            <el-button type="primary" :loading="cfgSaving" @click="saveConfig">保存配置</el-button>
          </div>
        </div>
      </div>

      <!-- 备用 token -->
      <div class="ea-card">
        <div class="ea-card-header">
          <div class="ea-card-title">备用：手动粘贴 Token</div>
        </div>
        <div class="ea-card-body">
          <el-alert
            style="margin-bottom: 12px"
            type="warning"
            :closable="false"
            show-icon
            title="EvalAgent 与知识库问答系统前端是不同源站点，无法自动读取其 localStorage 中的 token；如不便在下方内嵌登录，可从知识库问答系统前端（F12 → Application → Local Storage）复制 token 粘贴到此处"
          />
          <div style="display: flex; gap: 8px">
            <el-input v-model="tokenInput" placeholder="粘贴 access_token" show-password />
            <el-button type="primary" @click="applyToken">应用</el-button>
          </div>
        </div>
      </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 单列竖排：所有卡片自上而下堆叠，彻底消除左右两列内容不均导致的「一边多出一截、另一边空着」 */
.settings-grid {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 880px;
}

/* 列包裹仅作分组语义，单列下自然堆叠 */
.settings-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

/* 单列下卡片自身不要再留 margin，间距统一交给 gap（.ea-card 全局有 margin-bottom:16px，会与 gap 叠加成 32px） */
.settings-grid .ea-card {
  margin-bottom: 0;
}

/* 单列时表单字段不要撑满 880px，限制可读宽度 */
.settings-grid .ea-card-body :deep(.el-input),
.settings-grid .ea-card-body :deep(.el-input-number) {
  max-width: 560px;
}

.default-url {
  margin-top: 4px;
  padding: 8px 12px;
  background: var(--ea-bg);
  border: 1px solid var(--ea-border-light);
  border-radius: 6px;
  color: var(--ea-text-2);
}

.field-label {
  font-size: 12px;
  color: var(--ea-text-3);
}

/* 字段下方的说明文字（模型名口径、温度锁死原因等） */
.ea-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--ea-text-3);
  min-height: 16px;
}

.account-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 0;
  font-size: 13px;
  color: var(--ea-text-1);
}

.account-label {
  width: 56px;
  flex: none;
  color: var(--ea-text-3);
}
</style>
