/**
 * 裁判（考官）配置接口 —— 走评测域 8200（与「问答模型配置」不同，那张卡走 8010）。
 *
 * 后端行为（app/api/v1/system.py）：
 * - GET  登录可读，api_key 恒返掩码
 * - PUT  仅 admin；**保存前先用候选配置探活**，探活失败返回 422 且不落库
 * - POST preflight 单独探活当前生效配置
 * - 温度不在可写字段里：可为复现性强制锁定 0.0
 */
import http from './http'
import type { JudgeConfigPayload, JudgeConfigView, JudgePreflightResult } from './types'

export interface JudgeConfigUpdateResult {
  changed: boolean
  applied_keys?: string[]
  preflight?: { ok: boolean; latency_ms?: number }
  config: JudgeConfigView
}

/** 读取当前生效的裁判配置（密钥已掩码）。 */
export function getJudgeConfig(): Promise<JudgeConfigView> {
  return http.get('/system/judge-config') as Promise<JudgeConfigView>
}

/** 更新裁判配置：后端探活通过才落库，失败抛错（上层提示，不保留输入）。 */
export function updateJudgeConfig(payload: JudgeConfigPayload): Promise<JudgeConfigUpdateResult> {
  return http.put('/system/judge-config', payload) as Promise<JudgeConfigUpdateResult>
}

/** 单独探活当前配置（不修改任何东西）。 */
export function preflightJudgeConfig(): Promise<JudgePreflightResult> {
  return http.post('/system/judge-config/preflight') as Promise<JudgePreflightResult>
}
