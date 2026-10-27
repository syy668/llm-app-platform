/**
 * 工具注册表接口 —— 对应后端 `app/routers/tools.py`。
 *
 *  - `GET   /tools`        工具目录（元信息 + 启停状态 + 最近变更）
 *  - `GET   /tools/{name}` 单工具详情
 *  - `PATCH /tools/{name}` 启用 / 禁用（仅 ADMIN）
 *
 * 开关只影响"后续新建任务"的工具集：被禁用的工具不会出现在 Planner 的可用工具里，
 * 已挂起任务的断点续跑不受影响（连接池不摘除进程）。
 */
import { api } from './http'
import type { ToolInfo, ToolListData, ToolToggleRequest } from '@/types/tool'

/** GET /tools —— 工具目录（ADMIN / APPROVER 可读） */
export function listTools(): Promise<ToolListData> {
  return api.get<ToolListData>('/tools')
}

/** GET /tools/{name} —— 单工具详情 */
export function getTool(name: string): Promise<ToolInfo> {
  return api.get<ToolInfo>(`/tools/${encodeURIComponent(name)}`)
}

/** PATCH /tools/{name} —— 启用 / 禁用（仅 ADMIN） */
export function toggleTool(name: string, payload: ToolToggleRequest): Promise<ToolInfo> {
  return api.patch<ToolInfo>(`/tools/${encodeURIComponent(name)}`, payload)
}
