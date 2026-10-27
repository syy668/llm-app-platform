/**
 * RAG 实验参数展示逻辑。
 *
 * 上游已支持 rag_config 持久化（POST /eval/runs 可选参数，存 eval_runs.rag_config），
 * 因此任务配置展示以后端返回的 run.rag_config 为准；
 * 本地 localStorage 记录仅作为「后端记录缺失时的兜底」（如历史任务）。
 *
 * 生效范围说明：top_k / search_mode 由问答检索链路请求级生效（search_mode：
 * vector_only 纯向量 / bm25_vector_hybrid 混合，默认混合）；chunk_size 属于
 * 入库切片参数，查询期无法生效，仅作为实验记录展示。
 */
import type { EvalRun, RagConfig } from '@/api/types'

export interface ExpParams {
  chunk_size: number
  search_mode: 'vector_only' | 'bm25_vector_hybrid'
  top_k: number
}

const KEY = 'evalagent_exp_params'

type ExpParamsMap = Record<string, ExpParams>

function load(): ExpParamsMap {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? '{}') as ExpParamsMap
  } catch {
    return {}
  }
}

/** 本地兜底记录（历史任务/后端记录缺失时使用） */
export function saveExpParams(runId: number, params: ExpParams): void {
  const map = load()
  map[String(runId)] = params
  localStorage.setItem(KEY, JSON.stringify(map))
}

/** 生成列表/报告用的实验配置摘要文本：优先后端 rag_config，缺省回退本地记录 */
export function formatExpLabel(run: EvalRun): string {
  const rc = run.rag_config as RagConfig | null | undefined
  if (rc && (rc.chunk_size != null || rc.search_mode != null || rc.top_k != null)) {
    return `chunk_size=${rc.chunk_size ?? '默认'}, search_mode=${rc.search_mode ?? '默认'}, top_k=${rc.top_k ?? '默认'}`
  }
  const p = load()[String(run.id)]
  if (p) return `chunk_size=${p.chunk_size}, search_mode=${p.search_mode}, top_k=${p.top_k}`
  return '未记录（沿用知识库默认配置）'
}
