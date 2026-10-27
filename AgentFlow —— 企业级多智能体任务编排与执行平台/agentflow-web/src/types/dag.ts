/**
 * DAG 画布状态（前端派生，不对应后端枚举）。
 *
 * 由两路输入合成：
 *  1. `GET /tasks/{id}` 的 `status` + `subtasks[].status`（兜底快照）
 *  2. SSE `state_update` 的 enter/exit/error（实时覆盖）
 */
export type NodeStatus = 'ok' | 'run' | 'wait' | 'err' | 'idle'
