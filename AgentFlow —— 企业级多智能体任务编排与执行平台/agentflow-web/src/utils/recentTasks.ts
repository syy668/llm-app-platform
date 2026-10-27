/**
 * 本机最近访问的任务（localStorage）。
 *
 * 为什么需要它：后端**没有** `GET /tasks` 列表接口（缺口 G1），
 * 大盘的"任务列表"无法从服务端取。与其塞假数据，不如只展示
 * "用户本机真实打开过的任务"，并用 `GET /tasks/{id}` 刷新这些行的最新状态 ——
 * 每一条都是真实后端返回，且数据范围限定在本机，不涉及越权。
 */
import type { TaskStatus } from '@/types/enums'

export interface RecentTask {
  taskId: number
  taskNo: string
  goal: string
  status: TaskStatus
  /** 最近一次打开时间（本机时间） */
  openedAt: string
}

const STORAGE_KEY = 'af_recent_tasks'
const MAX_ITEMS = 8

export function readRecentTasks(): RecentTask[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const list = JSON.parse(raw) as RecentTask[]
    return Array.isArray(list) ? list.filter((item) => typeof item.taskId === 'number') : []
  } catch {
    return []
  }
}

export function rememberTask(task: Pick<RecentTask, 'taskId' | 'taskNo' | 'goal' | 'status'>): void {
  const list = readRecentTasks().filter((item) => item.taskId !== task.taskId)
  list.unshift({ ...task, openedAt: new Date().toISOString() })
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(0, MAX_ITEMS)))
}

export function updateRecentStatus(taskId: number, status: TaskStatus, goal?: string): void {
  const list = readRecentTasks()
  const target = list.find((item) => item.taskId === taskId)
  if (!target) return
  target.status = status
  if (goal) target.goal = goal
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list))
}

export function clearRecentTasks(): void {
  localStorage.removeItem(STORAGE_KEY)
}
