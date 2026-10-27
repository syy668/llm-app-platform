// 系统配置接口封装（读取需登录，修改仅管理员，修改实时生效）
import request from '@/utils/request'

/**
 * OpenAI 兼容服务商预设（只读元数据，由后端 runtime_config._PROVIDER_PRESETS 下发）。
 * 用于配置页「一键填充」API Base 与候选模型，不属于可写配置项。
 */
export interface ProviderPreset {
  /** 预设标识：deepseek / siliconflow / modelscope */
  id: string
  /** 展示名 */
  name: string
  /** 对话接口地址 */
  llm_api_base: string
  /** 该服务商推荐的对话模型名 */
  llm_models: string[]
  /** Embedding 接口地址（空串 = 该厂商无公开 Embedding 接口） */
  embedding_api_base: string
  /** 该服务商推荐的 Embedding 模型名 */
  embedding_models: string[]
}

export interface SystemConfig {
  /** 每用户每分钟 AI 请求上限 */
  ai_rate_limit_per_minute: number
  /** 工具执行超时（秒） */
  tool_timeout_seconds: number
  /** 工具最大尝试次数（含首次） */
  tool_max_attempts: number
  /** 工具重试间隔（秒） */
  tool_retry_delay_seconds: number
  /** LLM 采样温度 */
  llm_temperature: number
  /** 运行模式 / 提供方：mock=离线演示，openai=OpenAI 兼容，anthropic=Claude（v1.7 新增） */
  llm_provider: 'mock' | 'openai' | 'anthropic'
  /** LLM 模型名（任意 OpenAI 兼容模型） */
  llm_model: string
  /** LLM 接口地址（OpenAI 兼容） */
  llm_api_base: string
  /** LLM API Key：读取接口只返回掩码 ****** 或空串 */
  llm_api_key: string
  /** 评测判分模型（可选）：留空表示复用 llm_model（v1.7 新增） */
  eval_llm_model?: string
  /** 评测判分接口地址（可选）：留空表示复用 llm_api_base */
  eval_llm_api_base?: string
  /** 评测判分 API Key（可选）：读取只回掩码，留空表示复用 llm_api_key */
  eval_llm_api_key?: string
  /** 当前存在「服务端覆盖值」的键名（v1.6 新增，用于判断哪些项可恢复默认） */
  overridden_keys?: string[]
  /** OpenAI 兼容服务商预设（只读，后端下发，用于模型下拉与一键填充） */
  provider_presets?: ProviderPreset[]
}

export const getSystemConfig = () => request.get<unknown, SystemConfig>('/system/config')

export const updateSystemConfig = (payload: Partial<SystemConfig>) =>
  request.put<unknown, SystemConfig>('/system/config', payload)

/**
 * 删除单个配置项的覆盖值（即「恢复默认」，仅管理员）。
 * 删除后该键立即回退到 .env / 系统默认值，返回最新完整配置。
 */
export const deleteSystemConfigKey = (key: string) =>
  request.delete<unknown, SystemConfig>(`/system/config/${key}`)
