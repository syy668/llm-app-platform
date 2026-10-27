<script setup lang="ts">
// 智能对话三栏页面：左会话列表 / 中聊天窗口(SSE 打字机) / 右参数面板。
// 页面守卫：未选中会话时仅渲染空状态，不发起任何消息相关请求。
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, EditPen, CopyDocument, Star } from '@element-plus/icons-vue'
import { useUserStore } from '@/store/useUserStore'
import router from '@/router'
import { createConversation, deleteConversation, listConversations, listMessages, renameConversation } from '@/api/conversation'
import { listKbs } from '@/api/kb'
import { streamChat, SSEError } from '@/utils/sse'
import { addBadcaseToEval, sendFeedback } from '@/api/features'
import { renderMarkdown } from '@/utils/markdown'
import {
  matchSourceByDocChunk,
  nearestSourceByDoc,
  pickSourceByContent,
  renderAnswerHtml,
  siblingChunkNos,
  sourceChunkNo,
} from '@/utils/citation'
import type { Conversation, KnowledgeBase } from '@/types'
import EmptyState from '@/components/EmptyState.vue'
import RouteTag from '@/components/RouteTag.vue'

/** 后端随回答下发的结构化来源（与答案里的 [来源i] 一一对应） */
interface SourceItem {
  index: number
  chunk_id?: number
  document_id?: number
  document_title?: string
  /** 存储序号（0-based），仅用于排查 */
  chunk_index?: number
  /** 展示序号（1-based） */
  chunk_no?: number
  score?: number
  content?: string
  /** 并行对比路由的来源分组 */
  group?: 'A' | 'B'
}

interface ChatMessage {
  id?: number
  role: 'user' | 'assistant'
  content: string
  agent_route?: string | null
  status?: string
  trace_id?: string | null
  feedback?: number | null
  /** 已加入「用户反馈Badcase」评测集 */
  badcase?: boolean
  /** 结构化来源快照（v1.11 起随消息落库，历史消息也有；更早的老消息为 undefined） */
  sources?: SourceItem[]
  /** 引用校验未通过：答案含资料中找不到的数字/日期 */
  suspect?: boolean
  /** 【v1.12】本次路由继承自上一轮（短追问意图继承） */
  route_inherited?: boolean
  /**
   * 【v1.18】分类原本想走、但被右栏参数开关关掉的那一路。
   * 有关这个字段才能解释"为什么这次没检索"，否则用户只看到一个与开关
   * 无关的「直接回答」，会认为开关没生效。
   */
  route_disabled?: string | null
}

const store = useUserStore()

const conversations = ref<Conversation[]>([])
const kbs = ref<KnowledgeBase[]>([])
const currentCid = ref<number | null>(null)
const currentConv = computed(() => conversations.value.find((c) => c.id === currentCid.value) ?? null)
const currentKbId = computed(() => currentConv.value?.kb_id ?? null)
// 顶部标签展示「本次实际检索的知识库」：请求级临时指定优先，否则跟随会话绑定
const effectiveKbId = computed(() => chatKbId.value ?? currentKbId.value)
const messages = ref<ChatMessage[]>([])

const newDialogVisible = ref(false)
const newKbId = ref<number | null>(null)

const streaming = ref(false)
const streamingRoute = ref<string | null>(null)
// 【v1.18】本次被参数开关关掉的那一路（route 事件下发，用于徽标说明）
const streamingRouteDisabled = ref<string | null>(null)
// 【v1.12】本轮路由是否继承自上一轮（短追问意图继承）
const streamingInherited = ref(false)
const streamingText = ref('')
const inputText = ref('')

// 右栏「参数配置」：随每次发送传给后端（请求级参数，实时生效，不落库）
const chatKbId = ref<number | null>(null)      // 检索知识库；null = 跟随会话绑定
const chatTopK = ref<number | null>(null)       // 检索条数；null = 使用知识库配置
const enableRag = ref(true)
const enableDbQuery = ref(true)
const enableReport = ref(true)
const enableParallelCompare = ref(true)

function buildChatOptions() {
  const route_enabled: Record<string, boolean> = {
    rag: enableRag.value,
    db_query: enableDbQuery.value,
    report: enableReport.value,
    parallel_compare: enableParallelCompare.value,
  }
  const allEnabled = Object.values(route_enabled).every(Boolean)
  return {
    top_k: chatTopK.value,
    route_enabled: allEnabled ? null : route_enabled,
  }
}

const chatBodyRef = ref<HTMLElement | null>(null)

// ---------------- 引用卡片（溯源） ----------------
// 两种形态都做成可点击标签（渲染与匹配逻辑在 utils/citation.ts，纯函数、可单测）：
//   ① 结构化序号  [来源2]              —— 对应后端 sources 的 index
//   ② 纯文本溯源  来源：《文档》第4块    —— 模型经常不输出 ①，只写文档名 + 块号
// 点击统一打开右侧抽屉；v-html 内容无法直接绑事件，走气泡上的事件委托。
const drawerVisible = ref(false)
const activeSource = ref<SourceItem | null>(null)

/**
 * 块号对不上时的兜底记录：抽屉里要明说「你想看第 2 块，但这次只检索到第 1、3 块，
 * 当前展示的是第 1 块」。否则用户只会看到抽屉里写着「第 1 块」，以为点错了。
 *
 * `confidence` 区分两种兜底依据：`content` = 按内容重合度定位到了真正出处（可信），
 * `near` = 内容也没对上，只能按块号距离给个最接近的。两者文案不能混。
 */
const citeMismatch = ref<{
  doc: string
  want: number
  got: number
  avail: number[]
  confidence: 'content' | 'near'
  overlap: number
} | null>(null)

/** 打开抽屉的统一入口：每次都先清掉上一次的兜底提示，避免串味 */
function openSource(src: SourceItem, mismatch: typeof citeMismatch.value = null) {
  citeMismatch.value = mismatch
  activeSource.value = src
  drawerVisible.value = true
}

/** 渲染答案：markdown -> 纯文本溯源 chip -> [来源N] chip */
function renderAnswer(m: ChatMessage): string {
  return renderAnswerHtml(renderMarkdown(m.content), m.sources)
}

function fmtScore(score?: number): string {
  if (typeof score !== 'number' || !Number.isFinite(score)) return '—'
  return score.toFixed(4)
}

/** 事件委托：气泡内的引用标签点击 -> 打开抽屉（v-html 内容无法直接绑事件） */
function onBubbleClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  // 静态样式（无来源快照）不响应点击：渲染层已经不给 data-*，这里再兜一层
  if (target.closest('.is-static')) return

  const msgEl = target.closest('.msg') as HTMLElement | null
  const mi = Number(msgEl?.dataset.msgIndex)
  const list = messages.value[mi]?.sources ?? []

  // ① [来源N]：按序号匹配
  const citeEl = target.closest('.cite-chip') as HTMLElement | null
  if (citeEl) {
    const idx = Number(citeEl.dataset.cite)
    const src = list.find((s) => s.index === idx) ?? null
    if (!src) {
      ElMessage.info(`该条消息未保留来源快照，无法展开原文（来源${idx}）`)
      return
    }
    openSource(src)
    return
  }

  // ② 纯文本溯源：按「文档名 + 块号」匹配
  const plainEl = target.closest('.plain-cite') as HTMLElement | null
  if (!plainEl) return
  const doc = (plainEl.dataset.doc || '').trim()
  const chunk = Number(plainEl.dataset.chunk)
  const label = `《${doc}》第 ${chunk} 块`

  if (!list.length) {
    ElMessage.info(`该条回答未保留来源快照，无法展开${label}`)
    return
  }
  const { source, sameDoc } = matchSourceByDocChunk(list, doc, chunk)
  if (source) {
    openSource(source)
    return
  }

  // 块号对不上但同文档有片段：不做「弹个警告让你自己找」，直接打开能看的那一块。
  // 优先按**内容重合度**定位真正出处（模型常把章节号「## 2.」误写成「第2块」，
  // 只有内容比对能识破）；内容完全对不上时才退回块号距离。
  if (sameDoc) {
    const avail = siblingChunkNos(list, doc)
    const byContent = pickSourceByContent(list, doc, messages.value[mi]?.content ?? '')
    // 0.1 是软门槛：低于它多半是「工资条」这类高频词凑出来的假重合，不能当依据
    const trustContent = !!byContent && byContent.overlap >= 0.1
    const pick = (trustContent ? byContent!.source : null) ?? nearestSourceByDoc(list, doc, chunk)
    if (pick) {
      const got = sourceChunkNo(pick)
      openSource(pick, {
        doc,
        want: chunk,
        got,
        avail,
        confidence: trustContent ? 'content' : 'near',
        overlap: byContent?.overlap ?? 0,
      })
      ElMessage.warning(
        trustContent
          ? `模型引用的第 ${chunk} 块在本次检索结果中不存在；答案内容实际出自第 ${got} 块，已为你打开`
          : `模型引用的是《${doc}》第 ${chunk} 块，本次检索没有召回该块；` +
              `本文档可展开第 ${avail.join('、')} 块，已为你打开最接近的第 ${got} 块`,
      )
      return
    }
  }
  ElMessage.warning(`本次检索结果中没有《${doc}》的原文片段，无法展开${label}`)
}

async function loadConversations() {
  const res = await listConversations({ page: 1, size: 100 })
  conversations.value = res.items
}

async function loadKbsList() {
  kbs.value = await listKbs({ page: 1, size: 100 }).then((r) => r.items).catch(() => [])
}

async function loadMessages(cid: number) {
  const res = await listMessages(cid, { page: 1, size: 100 })
  messages.value = (res.items || []).map((m) => ({
    id: m.id,
    role: m.role === 'user' ? 'user' : 'assistant',
    content: m.content,
    agent_route: m.agent_route,
    status: m.status,
    trace_id: m.trace_id,
    feedback: m.feedback ?? null,
    // v1.11：来源快照已随消息落库，重开会话也能点开溯源
    sources: Array.isArray(m.sources) && m.sources.length ? (m.sources as SourceItem[]) : undefined,
  }))
  scrollToBottom()
}

function scrollToBottom() {
  nextTick(() => {
    const el = chatBodyRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function selectConversation(cid: number) {
  if (cid === currentCid.value) return
  currentCid.value = cid
  chatKbId.value = null // 切换会话后恢复跟随会话绑定，避免遗留的临时指定串台
  await loadMessages(cid)
}

async function createNew() {
  const conv = await createConversation(newKbId.value)
  newDialogVisible.value = false
  newKbId.value = null
  await loadConversations()
  currentCid.value = conv.id
  chatKbId.value = null
  messages.value = []
}

async function renameCurrent() {
  const conv = conversations.value.find((c) => c.id === currentCid.value)
  if (!conv) return
  try {
    const { value } = await ElMessageBox.prompt('请输入新的会话标题（1~100 字）', '重命名会话', {
      inputValue: conv.title || '',
      inputValidator: (v: string) => (v && v.trim().length <= 100 ? true : '标题不能为空且不超过 100 字'),
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    const updated = await renameConversation(conv.id, value.trim())
    // 合并更新本地列表（顶部标题取自 currentConv，自动同步）
    const idx = conversations.value.findIndex((c) => c.id === conv.id)
    if (idx >= 0) conversations.value[idx] = { ...conversations.value[idx], ...updated }
    ElMessage.success('已重命名')
  } catch {
    /* 用户取消输入，静默返回 */
  }
}

async function removeCurrent() {
  if (!currentCid.value) return
  const conv = conversations.value.find((c) => c.id === currentCid.value)
  try {
    await ElMessageBox.confirm(
      '删除后该会话的全部消息将一并清除，且不可恢复。确定要删除吗？',
      '删除确认',
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
  await deleteConversation(currentCid.value)
  ElMessage.success('已删除')
  currentCid.value = null
  messages.value = []
  await loadConversations()
}

/** 点赞/点踩 AI 回答（重复点击同一项视为取消反馈由后端忽略，此处仅做状态切换与提交） */
async function onFeedback(m: ChatMessage, rating: 1 | -1) {
  if (!m.id) return
  const next = m.feedback === rating ? rating : rating // 同值重复提交由后端 upsert 幂等处理
  try {
    await sendFeedback(m.id, next)
    m.feedback = rating
    ElMessage.success(rating === 1 ? '已点赞，感谢反馈' : '已点踩，感谢反馈')
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 一键把这条回答沉淀到「用户反馈Badcase」评测集（数据飞轮入口）。
 * 先问一句点踩原因（可留空），失败不改变任何本地状态。
 */
async function onAddBadcase(m: ChatMessage) {
  if (!m.id || m.badcase) return
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(
      '这条回答哪里不对？（可留空，会作为点踩原因保存）',
      '加入评测集',
      {
        inputPlaceholder: '例如：把旧版报销标准当成现行制度',
        inputValidator: (v: string) => (v ?? '').length <= 500 || '原因不超过 500 字',
        confirmButtonText: '加入',
        cancelButtonText: '取消',
      },
    )
    reason = (value || '').trim()
  } catch {
    return // 用户取消
  }
  try {
    const res = await addBadcaseToEval(m.id, reason || null)
    m.badcase = true
    m.feedback = -1
    if (res.duplicated) {
      ElMessage.info(`该问题已在「${res.dataset_name}」中，已更新反馈原因`)
    } else {
      ElMessage.success(`已加入「${res.dataset_name}」，到「RAGAS 评测 → 数据集管理」补全参考答案后即可参与评测`)
    }
  } catch {
    /* 错误已由拦截器提示 */
  }
}

async function copyMessage(content: string) {  try {    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(content)
    } else {
      // 非安全上下文（如局域网 http 访问）的降级复制方案
      const ta = document.createElement('textarea')
      ta.value = content
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择文本复制')
  }
}

async function send() {
  const q = inputText.value.trim()
  if (!q || !currentCid.value || streaming.value) return
  inputText.value = ''
  messages.value.push({ role: 'user', content: q })
  await streamAnswer(q, 0)
}

async function streamAnswer(question: string, attempt: number) {
  if (!currentCid.value) return
  streaming.value = true
  streamingRoute.value = null
  streamingRouteDisabled.value = null
  streamingInherited.value = false
  streamingText.value = ''
  scrollToBottom()

  try {
    for await (const ev of streamChat(currentCid.value, question, store.accessToken, chatKbId.value, buildChatOptions())) {
      const d = ev.data as Record<string, unknown>
      if (ev.event === 'route') {
        streamingRoute.value = (d.route as string) || null
        // 【v1.12】路由继承自上一轮（短追问），用于在标签上打「继承」徽标
        streamingInherited.value = d.inherited === true
        // 【v1.18】被参数开关关掉的那一路（route 已由后端改报 direct）
        streamingRouteDisabled.value = (d.disabled as string) || null
      } else if (ev.event === 'delta') {
        streamingText.value += (d.content as string) ?? ''
        scrollToBottom()
      } else if (ev.event === 'report') {
        // 报表路由：完整 Markdown 直接渲染
        streamingText.value = (d.content as string) ?? ''
        scrollToBottom()
      } else if (ev.event === 'done') {
        // 【v1.16】后端判定输出被 max_tokens 截断时，把提示补到气泡末尾。
        // delta 是逐字推送的，提示不能走 delta（会重复追加），故由 done 带文案，
        // 前端只负责拼接，不在本地硬编码提示语。
        const notice = d.truncated === true ? ((d.truncation_notice as string) ?? '') : ''
        messages.value.push({
          id: (d.message_id as number) ?? undefined,
          role: 'assistant',
          content: streamingText.value + notice,
          agent_route: (d.agent_route as string) ?? streamingRoute.value,
          status: 'done',
          trace_id: d.trace_id as string,
          // 【v1.9】引用卡片：结构化来源 + 后置校验的 suspect 标记
          sources: Array.isArray(d.sources) ? (d.sources as SourceItem[]) : undefined,
          suspect: d.suspect === true,
          // 【v1.12】本次路由是否继承自上一轮
          route_inherited: d.route_inherited === true,
          // 【v1.18】被右栏开关关掉的那一路（null = 未关闭）
          route_disabled: (d.route_disabled as string) ?? null,
        })
      } else if (ev.event === 'error') {
        messages.value.push({
          role: 'assistant',
          content: (d.message as string) ?? 'AI 服务调用失败',
          agent_route: streamingRoute.value,
          status: 'failed',
        })
      }
    }
    // 首问后后端可能自动生成了会话标题（title 为空才填），刷新侧栏与顶部标题；
    // 已命名的会话跳过，避免每轮多打一次列表接口。
    const cur = conversations.value.find((c) => c.id === currentCid.value)
    if (cur && !cur.title) loadConversations()
  } catch (e) {
    // 401：先尝试刷新 token 重试一次（与 axios 拦截器行为对齐）
    if (e instanceof SSEError && e.status === 401) {
      if (attempt === 0 && (await store.refresh())) {
        streaming.value = false
        await streamAnswer(question, 1)
        return
      }
      // 刷新也失败（会话彻底失效：Redis 被清空/重启、账号被禁用、refresh 过期）
      // → 与 axios 401 分支保持一致地清理凭据并跳登录页，避免用户卡在对话页反复 401。
      streaming.value = false
      store.clear()
      ElMessage.error('登录已过期，请重新登录')
      router.push('/login')
      return
    }
    if (e instanceof SSEError && e.status === 429) {
      // 限流：明确提示配额用尽，不污染消息列表
      ElMessage.warning(e.message || '请求过于频繁，请稍后重试')
      return
    }
    messages.value.push({
      role: 'assistant',
      content: (e as Error).message || 'AI 服务调用失败',
      agent_route: streamingRoute.value,
      status: 'failed',
    })
  } finally {
    streaming.value = false
    streamingText.value = ''
  }
}

onMounted(() => {
  loadConversations()
  loadKbsList()
})
</script>

<template>
  <div class="chat-layout">
    <!-- 左栏：会话列表 -->
    <div class="chat-panel session-panel">
      <div class="panel-head">
        <span class="panel-title">历史会话</span>
        <el-button size="small" type="primary" @click="newDialogVisible = true">
          <el-icon><Plus /></el-icon>新建
        </el-button>
      </div>
      <div class="session-list">
        <EmptyState v-if="!conversations.length" title="暂无会话" desc="点击新建会话开始对话" icon="💬" />
        <div
          v-for="c in conversations"
          :key="c.id"
          class="session-item"
          :class="{ active: c.id === currentCid }"
          @click="selectConversation(c.id)"
        >
          <div class="s-main">
            <div class="s-title">{{ c.title || `会话 #${c.id}` }}</div>
            <div class="s-time">{{ c.kb_id ? `KB#${c.kb_id}` : '未绑定知识库' }}</div>
          </div>
        </div>
      </div>
      <div class="panel-foot">
        <el-button text size="small" :disabled="!currentCid" @click="renameCurrent">
          <el-icon><EditPen /></el-icon>&nbsp;重命名
        </el-button>
        <el-button text type="danger" size="small" :disabled="!currentCid" @click="removeCurrent">
          <el-icon><Delete /></el-icon>&nbsp;删除会话
        </el-button>
      </div>
    </div>

    <!-- 中栏：聊天窗口 -->
    <div class="chat-panel">
      <div class="panel-head">
        <span class="panel-title">{{ currentCid ? currentConv?.title || `会话 #${currentCid}` : '智能对话' }}</span>
        <el-tooltip
          :content="chatKbId ? `本次发送临时指定 KB#${chatKbId}，会话绑定仍为 ${currentKbId ? 'KB#' + currentKbId : '无'}` : (currentKbId ? '会话绑定的知识库' : '未绑定知识库，可在右栏临时指定')"
          placement="bottom"
        >
          <span class="ui-tag" :class="effectiveKbId ? (chatKbId ? 'tag-orange' : 'tag-blue') : 'tag-gray'">
            {{ effectiveKbId ? `KB#${effectiveKbId}${chatKbId ? ' · 临时' : ''}` : '未绑定知识库' }}
          </span>
        </el-tooltip>
      </div>

      <div class="chat-body" ref="chatBodyRef" @click="onBubbleClick">
        <!-- 页面守卫：未选中会话仅渲染空状态，不发起消息请求 -->
        <EmptyState v-if="!currentCid" title="请左侧选择会话" desc="选择或新建一个会话后，即可开始智能问答" icon="💬" />

        <template v-else>
          <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role === 'user' ? 'msg-user' : 'msg-assistant'" :data-msg-index="i">
            <div class="avatar" :class="{ ai: m.role === 'assistant' }">{{ m.role === 'user' ? '我' : 'AI' }}</div>
            <div class="msg-main">
              <div class="msg-meta">
                <RouteTag v-if="m.role === 'assistant' && m.agent_route" :route="m.agent_route" :inherited="m.route_inherited" :disabled="m.route_disabled" />
                <el-tooltip
                  v-if="m.role === 'assistant' && m.suspect"
                  content="答案中的数字/日期未能在检索资料中找到，建议人工核验"
                  placement="top"
                >
                  <span class="ui-tag tag-orange">待核验</span>
                </el-tooltip>
                <el-tooltip v-if="m.role === 'assistant' && m.status !== 'failed' && m.content" content="复制原文" placement="top">
                  <el-button text size="small" class="copy-btn" @click="copyMessage(m.content)">
                    <el-icon><CopyDocument /></el-icon>
                  </el-button>
                </el-tooltip>
                <template v-if="m.role === 'assistant' && m.id && m.status === 'done'">
                  <el-tooltip content="赞，回答有帮助" placement="top">
                    <el-button
                      text size="small" class="copy-btn"
                      :class="{ 'fb-active': m.feedback === 1 }"
                      @click="onFeedback(m, 1)"
                    ><span class="fb-emoji">👍</span></el-button>
                  </el-tooltip>
                  <el-tooltip content="踩，回答不准确" placement="top">
                    <el-button
                      text size="small" class="copy-btn"
                      :class="{ 'fb-active-down': m.feedback === -1 }"
                      @click="onFeedback(m, -1)"
                    ><span class="fb-emoji">👎</span></el-button>
                  </el-tooltip>
                  <el-tooltip
                    :content="m.badcase ? '已加入评测集' : '加入评测集，沉淀为 badcase'"
                    placement="top"
                  >
                    <el-button
                      text size="small" class="copy-btn"
                      :class="{ 'fb-active-down': m.badcase }"
                      :disabled="m.badcase"
                      @click="onAddBadcase(m)"
                    ><el-icon><Star /></el-icon></el-button>
                  </el-tooltip>
                </template>
              </div>
              <div class="bubble" :class="m.role === 'user' ? 'bubble-user' : 'bubble-ai'">
                <span v-if="m.role === 'user'" style="white-space: pre-wrap">{{ m.content }}</span>
                <span v-else-if="m.status === 'failed'" style="color: var(--danger)">{{ m.content }}</span>
                <span v-else v-html="renderAnswer(m)"></span>
              </div>
            </div>
          </div>

          <!-- 流式生成占位 -->
          <div v-if="streaming" class="msg msg-assistant">
            <div class="avatar ai">AI</div>
            <div class="msg-main">
              <div class="msg-meta">
                <RouteTag v-if="streamingRoute" :route="streamingRoute" :inherited="streamingInherited" :disabled="streamingRouteDisabled" />
                <span class="msg-time">生成中…</span>
              </div>
              <div class="bubble bubble-ai">
                <span v-html="renderMarkdown(streamingText)"></span><span class="cursor">▍</span>
              </div>
            </div>
          </div>
        </template>
      </div>

      <div class="chat-foot">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="1"
          :autosize="{ minRows: 1, maxRows: 4 }"
          placeholder="输入你的问题，Enter 发送（Shift+Enter 换行）"
          :disabled="!currentCid || streaming"
          @keydown.enter.exact.prevent="send"
        />
        <el-button type="primary" :disabled="!currentCid || streaming || !inputText.trim()" @click="send">发送</el-button>
      </div>
    </div>

    <!-- 右栏：参数配置（请求级参数，随每次发送生效） -->
    <div class="chat-panel">
      <div class="panel-head"><span class="panel-title">参数配置</span></div>
      <div class="param-list">
        <div class="small muted">参数随每次发送生效，仅对当前会话的后续提问有效</div>
        <el-form label-position="top" size="small">
          <el-form-item label="检索知识库">
            <el-select v-model="chatKbId" clearable placeholder="跟随会话绑定" style="width: 100%">
              <el-option v-for="k in kbs" :key="k.id" :label="`#${k.id} ${k.name}`" :value="k.id" />
            </el-select>
            <div class="small muted" style="margin-top: 4px">默认检索会话绑定的知识库；可临时指定其他知识库，仅对本次发送生效</div>
          </el-form-item>
          <el-form-item label="检索条数 top_k">
            <el-input-number v-model="chatTopK" :min="1" :max="50" placeholder="知识库配置" style="width: 100%" />
            <div class="small muted" style="margin-top: 4px">留空使用当前会话绑定知识库的配置</div>
          </el-form-item>
          <el-divider content-position="left"><span class="small muted">工具开关</span></el-divider>
          <el-form-item label="知识库检索"><el-switch v-model="enableRag" /></el-form-item>
          <el-form-item label="业务库查询"><el-switch v-model="enableDbQuery" /></el-form-item>
          <el-form-item label="报表生成"><el-switch v-model="enableReport" /></el-form-item>
          <el-form-item label="并行对比"><el-switch v-model="enableParallelCompare" /></el-form-item>
          <div class="small muted">每次提问由 Agent 按问题意图自动路由到其中一路执行（回答标签显示实际命中的路由，非随机）；关闭某路后，相关问题将直接由 AI 回答，不再调用该工具</div>
          <el-divider content-position="left"><span class="small muted">全局参数</span></el-divider>
          <div class="small muted">限流、工具重试 / 超时、LLM 模型与密钥等全局参数请在「系统配置」页调整（管理员，实时生效）</div>
        </el-form>
      </div>
    </div>

    <!-- 引用溯源抽屉：展示被引用片段的原文 -->
    <el-drawer v-model="drawerVisible" title="引用来源" size="420px">
      <template v-if="activeSource">
        <!-- 块号对不上时的兜底说明：模型引用号 ≠ 本次检索到的块号 -->
        <el-alert
          v-if="citeMismatch"
          class="cite-mismatch"
          type="warning"
          :closable="false"
          show-icon
          :title="
            citeMismatch.confidence === 'content'
              ? `模型引用的第 ${citeMismatch.want} 块不存在，答案内容实际出自第 ${citeMismatch.got} 块`
              : `模型引用的是《${citeMismatch.doc}》第 ${citeMismatch.want} 块，本次检索未召回该块`
          "
          :description="
            citeMismatch.confidence === 'content'
              ? `已按内容重合度 ${(citeMismatch.overlap * 100).toFixed(0)}% 定位；文档里的章节号「${citeMismatch.want}.」常被模型误当成块号。本文档本次可展开第 ${citeMismatch.avail.join('、')} 块`
              : `已为你展示最接近的第 ${citeMismatch.got} 块；本文档本次可展开第 ${citeMismatch.avail.join('、')} 块`
          "
        />
        <div class="src-head">
          <div class="src-title">《{{ activeSource.document_title || '未命名文档' }}》</div>
          <div class="src-meta">
            第 {{ sourceChunkNo(activeSource) || '-' }} 块 · 相似度 {{ fmtScore(activeSource.score) }}
            <span v-if="activeSource.group"> · 对比路 {{ activeSource.group }}</span>
          </div>
        </div>
        <div class="src-body">{{ activeSource.content || '（该片段无正文）' }}</div>
      </template>
    </el-drawer>

    <!-- 新建会话弹窗 -->
    <el-dialog v-model="newDialogVisible" title="新建会话" width="420px">
      <el-form label-position="top">
        <el-form-item label="绑定知识库">
          <el-select v-model="newKbId" clearable placeholder="不绑定知识库" style="width: 100%">
            <el-option v-for="k in kbs" :key="k.id" :label="`#${k.id} ${k.name}`" :value="k.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="newDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="createNew">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-layout { display: grid; grid-template-columns: 264px 1fr 264px; gap: 16px; height: calc(100vh - 56px - 60px); min-height: 560px; }
.chat-panel { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow-sm); display: flex; flex-direction: column; overflow: hidden; }
.panel-head { padding: 14px 16px; border-bottom: 1px solid var(--border-light); display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--text-1); }
.session-list { flex: 1; overflow-y: auto; padding: 8px; }
.session-item { display: flex; align-items: center; padding: 10px 12px; border-radius: var(--radius-sm); cursor: pointer; border: 1px solid transparent; transition: 0.14s; }
.session-item:hover { background: #f7f9fc; }
.session-item.active { background: var(--primary-weak); border-color: #b8d0ff; }
.s-main { flex: 1; min-width: 0; }
.s-title { font-size: 13px; color: var(--text-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.s-time { font-size: 11px; color: var(--text-3); }
.panel-foot { border-top: 1px solid var(--border-light); padding: 8px 12px; display: flex; align-items: center; justify-content: space-between; }

.chat-body { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; background: #fafbfc; }
.chat-foot { border-top: 1px solid var(--border-light); padding: 12px 14px; display: flex; gap: 10px; align-items: flex-end; }

.msg { display: flex; gap: 10px; max-width: 88%; }
.msg-user { align-self: flex-end; flex-direction: row-reverse; }
.msg-assistant { align-self: flex-start; }
.avatar { width: 32px; height: 32px; border-radius: 9px; background: var(--primary-weak); color: var(--primary); display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600; flex: none; }
.avatar.ai { background: linear-gradient(135deg, #165dff, #6aa1ff); color: #fff; }
.msg-main { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.msg-meta { display: flex; align-items: center; gap: 8px; }
.copy-btn { color: var(--text-3); padding: 2px; height: auto; }
.copy-btn:hover { color: var(--primary); }
.fb-emoji { font-size: 15px; line-height: 1; padding: 1px 3px; border-radius: 5px; }
.fb-active { background: var(--success-bg) !important; border-radius: 6px; }
.fb-active-down { background: var(--danger-bg) !important; border-radius: 6px; }
.msg-user .msg-meta { justify-content: flex-end; }
.msg-time { font-size: 11px; color: var(--text-3); }
.bubble { padding: 10px 14px; border-radius: 10px; font-size: 13.5px; line-height: 1.7; word-break: break-word; }
.bubble-user { background: var(--primary); color: #fff; border-top-right-radius: 2px; }
.bubble-ai { background: #fff; border: 1px solid var(--border); border-top-left-radius: 2px; box-shadow: var(--shadow-sm); }
.cursor { display: inline-block; color: var(--primary); font-weight: 700; animation: blink 1s steps(1) infinite; margin-left: 1px; }
@keyframes blink { 50% { opacity: 0; } }

.param-list { padding: 16px; overflow-y: auto; }

/* 引用标签：v-html 内容，样式需 :deep 才能命中 */
.bubble :deep(.cite-chip) { display: inline-block; margin: 0 2px; padding: 0 6px; border-radius: 9px; background: var(--primary-weak); color: var(--primary); font-size: 12px; line-height: 18px; cursor: pointer; vertical-align: baseline; }
.bubble :deep(.cite-chip:hover) { background: #d6e4ff; }
/* 纯文本溯源 chip：比 [来源N] 长，限制文档名宽度并省略，避免撑破气泡 */
.bubble :deep(.plain-cite) { display: inline-flex; align-items: center; max-width: 100%; margin: 0 2px; padding: 0 7px; border-radius: 9px; background: #eef4ff; color: #2b5fd9; border: 1px solid #d3e2ff; font-size: 12px; line-height: 18px; cursor: pointer; vertical-align: baseline; transition: background 0.14s, border-color 0.14s; }
.bubble :deep(.plain-cite:hover) { background: #dbe8ff; border-color: #b8d0ff; }
.bubble :deep(.plain-cite-t) { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 无来源快照时的静态样式：灰底 + 虚线边框 + 无 pointer + hover 无变化。
   虚线是关键：实心灰块会被当成「禁用按钮」，虚线更像「这是个占位/不可用标注」。
   选择器要带上 .cite-chip / .plain-cite 两级类，否则特异性打不过上面的可点样式 */
.bubble :deep(.cite-chip.is-static),
.bubble :deep(.plain-cite.is-static) { background: #fafafa; color: var(--text-3); border: 1px dashed #dcdfe6; cursor: default; }
.bubble :deep(.cite-chip.is-static:hover),
.bubble :deep(.plain-cite.is-static:hover) { background: #fafafa; border-color: #dcdfe6; }
.src-head { padding-bottom: 10px; border-bottom: 1px solid var(--border-light); margin-bottom: 12px; }
.cite-mismatch { margin-bottom: 12px; }
.src-title { font-size: 14px; font-weight: 600; color: var(--text-1); }
.src-meta { margin-top: 4px; font-size: 12px; color: var(--text-3); }
.src-body { font-size: 13px; line-height: 1.8; color: var(--text-1); white-space: pre-wrap; word-break: break-word; }
</style>
