<script setup lang="ts">
/**
 * P09 系统设置。
 *
 * 真实数据（三个探针，注意它们不在同一前缀下）：
 *  - `GET /healthz`          存活
 *  - `GET /readyz`           就绪（MySQL + Redis，未就绪返回 503）
 *  - `GET /api/v1/health`    运行态摘要（env / mock 开关 / 令牌有效期 / langgraph）
 *
 * 就绪探针探测的是「本进程实际在用的存储」：生产为 MySQL + Redis；
 * 本地 `dev_serve.py` 会把进程级单例绑到嵌入式实例（SQLite + fakeredis），
 * 因此演示环境同样返回 `ready`。若显示 `not_ready`，说明对应依赖确实不可用。
 *
 * 日志查询：`POST /logs/search`（基于 task_executions 结构化记录）已接入，
 * 面板见 `components/LogSearchPanel.vue`，检索维度与后端字段严格一一对应。
 *
 * 缺口：LLM 参数等配置来自环境变量，无读写接口（G6 配置侧仍无），
 * 因此「开关配置」区只做只读呈现，**不编造配置值**。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchHealthSummary, fetchHealthz, fetchReadyz } from '@/api/health'
import { isApiError } from '@/api/http'
import { formatFullTime } from '@/utils/format'
import JsonViewer from '@/components/JsonViewer.vue'
import LogSearchPanel from '@/components/LogSearchPanel.vue'
import type { HealthSummaryData, HealthzData, ReadyzData } from '@/types/health'

const loading = ref(false)
const checkedAt = ref<string | null>(null)
const healthz = ref<HealthzData | null>(null)
const healthzError = ref<string | null>(null)
const readyz = ref<{ ok: boolean; data: ReadyzData } | null>(null)
const readyzError = ref<string | null>(null)
const summary = ref<HealthSummaryData | null>(null)
const summaryError = ref<string | null>(null)

let timer: number | null = null

async function probe(): Promise<void> {
  loading.value = true
  checkedAt.value = new Date().toISOString()

  try {
    healthz.value = await fetchHealthz()
    healthzError.value = null
  } catch (error) {
    healthz.value = null
    healthzError.value = isApiError(error) ? error.message : '探针不可达'
  }

  try {
    readyz.value = await fetchReadyz()
    readyzError.value = null
  } catch (error) {
    readyz.value = null
    readyzError.value = isApiError(error) ? error.message : '探针不可达'
  }

  try {
    summary.value = await fetchHealthSummary()
    summaryError.value = null
  } catch (error) {
    summary.value = null
    summaryError.value = isApiError(error) ? error.message : '探针不可达'
  }

  loading.value = false
}

onMounted(() => {
  void probe()
  timer = window.setInterval(() => {
    void probe()
  }, 30000)
})

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<template>
  <div class="af-page">
    <div class="af-page-head">
      <div>
        <h1>系统设置</h1>
        <div class="af-sub">
          运行时配置以只读方式呈现 · 修改需运维调整环境变量后重启
          <span v-if="checkedAt" class="af-mono"> · 探测于 {{ formatFullTime(checkedAt) }}</span>
        </div>
      </div>
      <a-space>
        <a-button :loading="loading" @click="probe">重新探测</a-button>
        <span class="af-muted">每 30s 自动刷新</span>
      </a-space>
    </div>

    <!-- 三探针 -->
    <a-row :gutter="12">
      <a-col :span="8">
        <a-card :bordered="true" class="probe-card">
          <div class="probe-head">
            <span class="probe-dot" :class="healthz ? 'is-ok' : 'is-err'" />
            存活探针 <span class="af-mono af-muted">/healthz</span>
          </div>
          <div v-if="healthz" class="probe-body">
            <div class="probe-kv"><span>status</span><b>{{ healthz.status }}</b></div>
            <div class="probe-kv"><span>service</span><b class="af-mono">{{ healthz.service }}</b></div>
            <div class="probe-kv"><span>version</span><b>{{ healthz.version }}</b></div>
          </div>
          <div v-else class="probe-body af-muted">{{ healthzError }}</div>
        </a-card>
      </a-col>

      <a-col :span="8">
        <a-card :bordered="true" class="probe-card">
          <div class="probe-head">
            <span class="probe-dot" :class="readyz?.ok ? 'is-ok' : 'is-warn'" />
            就绪探针 <span class="af-mono af-muted">/readyz</span>
          </div>
          <div v-if="readyz" class="probe-body">
            <div class="probe-kv">
              <span>status</span>
              <b :class="readyz.ok ? 'is-ok-text' : 'is-err-text'">{{ readyz.data.status }}</b>
            </div>
            <template v-if="readyz.data.checks">
              <div v-for="(value, key) in readyz.data.checks" :key="key" class="probe-kv">
                <span>{{ key }}</span>
                <b>{{ value }}</b>
              </div>
            </template>
            <JsonViewer v-else :value="readyz.data" :max-height="120" />
            <div v-if="!readyz.ok" class="af-muted probe-note">
              探针检测到依赖不可用。生产环境请检查 MySQL / Redis 连通性；
              本地演示若出现此项，说明启动脚本未把单例绑定到嵌入式实例。
            </div>
          </div>
          <div v-else class="probe-body af-muted">{{ readyzError }}</div>
        </a-card>
      </a-col>

      <a-col :span="8">
        <a-card :bordered="true" class="probe-card">
          <div class="probe-head">
            <span class="probe-dot" :class="summary ? 'is-ok' : 'is-err'" />
            运行态摘要 <span class="af-mono af-muted">/api/v1/health</span>
          </div>
          <div v-if="summary" class="probe-body">
            <div class="probe-kv"><span>env</span><b>{{ summary.env }}</b></div>
            <div class="probe-kv"><span>MOCK RAG</span><b>{{ summary.mock.external_rag ? '开' : '关' }}</b></div>
            <div class="probe-kv"><span>MOCK 评测</span><b>{{ summary.mock.external_eval ? '开' : '关' }}</b></div>
            <div class="probe-kv">
              <span>access 有效期</span><b>{{ summary.auth.access_expire_minutes }} 分钟</b>
            </div>
            <div class="probe-kv">
              <span>refresh 有效期</span><b>{{ summary.auth.refresh_expire_days }} 天</b>
            </div>
          </div>
          <div v-else class="probe-body af-muted">{{ summaryError }}</div>
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="12">
      <a-col :span="24">
        <a-card :bordered="true" class="block" title="开关配置（只读）">
          <template #extra><span class="af-muted">来源：环境变量</span></template>
          <div class="row"><span>分层记忆</span><b>由 ENABLE_LAYERED_MEMORY 决定</b></div>
          <div class="row"><span>SSE 进度流</span><b>由 SSE_ENABLED 决定</b></div>
          <div class="row"><span>本人可自审</span><b>由 APPROVAL_ALLOW_SELF_DECIDE 决定</b></div>
          <div class="row"><span>Mock 外部 RAG</span><b>{{ summary?.mock.external_rag ? '开启' : '关闭' }}</b></div>
          <div class="row"><span>Mock 外部评测</span><b>{{ summary?.mock.external_eval ? '开启' : '关闭' }}</b></div>
          <div class="row"><span>令牌黑名单</span><b>{{ summary?.auth.blacklist_enabled ? '启用' : '关闭' }}</b></div>
          <div class="af-muted note">
            后端没有配置读写接口（缺口 G6），实际值需看部署环境或
            <span class="af-mono">/api/v1/health</span> 暴露的部分。
          </div>
        </a-card>

        <a-card :bordered="true" class="block" title="LangGraph 运行时">
          <JsonViewer :value="summary?.langgraph" :max-height="180" empty-text="未返回 langgraph 信息" />
        </a-card>
      </a-col>
    </a-row>

    <a-card :bordered="true" class="block" title="请求日志">
      <template #extra>
        <span class="af-muted">数据源：task_executions 执行轨迹（只读）</span>
      </template>
      <LogSearchPanel />
    </a-card>
  </div>
</template>

<style scoped>
.block {
  margin-top: 12px;
}

.probe-card {
  height: 100%;
}

.probe-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 8px;
}

.probe-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.probe-dot.is-ok {
  background: rgb(var(--success-6));
}

.probe-dot.is-warn {
  background: rgb(var(--warning-6));
}

.probe-dot.is-err {
  background: rgb(var(--danger-6));
}

.probe-body {
  font-size: 12px;
  line-height: 22px;
}

.probe-kv {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}

.probe-kv span {
  color: var(--color-text-3);
}

.probe-kv b {
  font-weight: 400;
  color: var(--color-text-1);
  word-break: break-all;
  text-align: right;
}

.is-ok-text {
  color: rgb(var(--success-6));
}

.is-err-text {
  color: rgb(var(--danger-6));
}

.probe-note {
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px dashed var(--color-border-1, #f2f3f5);
  line-height: 18px;
  font-size: 11px;
}

.row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--color-border-1, #f2f3f5);
  font-size: 12px;
}

.row:last-of-type {
  border-bottom: 0;
}

.row span {
  color: var(--color-text-3);
}

.row b {
  font-weight: 400;
  text-align: right;
}

.note {
  margin-top: 8px;
  font-size: 11.5px;
  line-height: 19px;
}
</style>
