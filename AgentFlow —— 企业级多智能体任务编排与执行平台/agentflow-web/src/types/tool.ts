/**
 * 工具注册表类型 —— 对应后端 `app/schemas/tool.py`。
 *
 * 入参是 JSON Schema 子集（type/required/properties），出参是「字段名 -> 说明」映射；
 * 后端刻意用展示形态返回，前端不再二次解析 Pydantic 模型。
 */
export type ToolCategory = 'retrieval' | 'database' | 'visualization' | 'notification' | (string & {})
export type ToolRiskLevel = 'low' | 'medium' | 'high' | (string & {})

export interface ToolConfigHint {
  key: string
  value: string
  purpose: string
  sensitive: boolean
}

export interface ToolInputProperty {
  type?: string
  description?: string
  enum?: string[]
  default?: unknown
  minimum?: number
  maximum?: number
  minLength?: number
  maxLength?: number
  minItems?: number
  maxItems?: number
  items?: { type?: string }
}

export interface ToolInputSchema {
  type?: string
  required?: string[]
  properties?: Record<string, ToolInputProperty>
}

export interface ToolInfo {
  name: string
  display_name: string
  server: string
  category: ToolCategory
  description: string
  capability: string
  risk_level: ToolRiskLevel
  external: boolean
  input_schema: ToolInputSchema
  output_schema: Record<string, string>
  config_hints: ToolConfigHint[]
  notes: string[]
  enabled: boolean
  /** 加固任务 2：熔断器状态（连续失败 5 次 → 冷却 60s 内快速拒绝），与人工启停开关独立 */
  circuit_open: boolean
  updated_at?: string | null
  updated_by?: string | null
}

export interface ToolAuditItem {
  at?: string
  tool?: string
  enabled?: boolean
  operator?: string
  reason?: string
}

export interface ToolListData {
  items: ToolInfo[]
  total: number
  enabled: number
  disabled: number
  generated_at: string
  audit: ToolAuditItem[]
  /**
   * true = 当前全部工具均被禁用，运行时已自动回退为全量工具（fail-open 安全兜底，有意设计）。
   * 缺陷 D-02 建议 b：如实提示该状态，避免运维误判为「已无工具可用」。
   */
  fail_open: boolean
}

export interface ToolToggleRequest {
  enabled: boolean
  reason?: string
}
