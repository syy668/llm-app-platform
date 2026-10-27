/**
 * 系统配置接口：复用项目一已有 /system/config，不新增后端。
 * 【第四步】该配置落在上游 data/system_config.json（上游 LLM 热配置）→ 走 upstreamHttp（8010）。
 * 注意：评测中台自己的裁判模型配置在 evalagent_backend/.env 的 JUDGE_*，两者不是一个东西。
 * - GET：登录用户可读（llm_api_key 恒返回掩码）
 * - PUT：仅管理员；实时生效无需重启（热更新持久化在上游 data/system_config.json）
 */
import { upstreamHttp as http } from './http'
import type { SystemConfig, SystemConfigPayload } from './types'

/** 读取当前生效配置（密钥为掩码）。 */
export function getSystemConfig(): Promise<SystemConfig> {
  return http.get('/system/config') as Promise<SystemConfig>
}

/** 更新配置（仅提交字段；密钥空值/掩码视为未修改）。返回更新后的完整配置。 */
export function updateSystemConfig(payload: SystemConfigPayload): Promise<SystemConfig> {
  return http.put('/system/config', payload) as Promise<SystemConfig>
}
