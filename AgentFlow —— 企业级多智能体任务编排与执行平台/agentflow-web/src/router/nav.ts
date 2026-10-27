/**
 * 侧边栏导航配置 —— 与原型规范 §2 信息架构一致。
 *
 * roles 为空表示全部角色可见；后端仍会做二次校验（前端隐藏只是体验优化）。
 */
import type { UserRole } from '@/types/enums'

export interface NavItem {
  key: string
  /** 展示名 */
  label: string
  /** 路由 path */
  path: string
  /** Arco 图标组件名 */
  icon: string
  /** 可见角色；空数组 = 全部 */
  roles: UserRole[]
  /** 是否显示待审批徽标 */
  badge?: 'approvals'
}

export interface NavGroup {
  title: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    title: '业务',
    items: [
      { key: 'dashboard', label: '工作台', path: '/dashboard', icon: 'IconDashboard', roles: [] },
      { key: 'task-create', label: '新建任务', path: '/tasks/new', icon: 'IconPlusCircle', roles: ['admin'] },
      {
        key: 'approvals',
        label: '审批收件箱',
        path: '/approvals',
        icon: 'IconCheckCircle',
        roles: ['admin', 'approver'],
        badge: 'approvals',
      },
      {
        key: 'eval',
        label: '评测与实验',
        path: '/eval',
        icon: 'IconExperiment',
        // PRD 旅程三：访客只读汇总指标；admin/approver 完整视图
        roles: [],
      },
      { key: 'memory', label: '记忆管理', path: '/memories', icon: 'IconStorage', roles: ['admin'] },
    ],
  },
  {
    title: '系统',
    items: [
      // v1.0 对齐 PRD：不提供用户管理页面，账号由管理员预置数据库；
      // 角色权限通过路由守卫 + 按钮显隐落地（见 stores/auth.ts 与各页面 canXxx 计算属性）。
      {
        // v1.1：MCP 工具注册表（内置工具元信息 + 启停开关）
        key: 'tools',
        label: '工具注册表',
        path: '/tools',
        icon: 'IconApps',
        roles: ['admin', 'approver'],
      },
      { key: 'settings', label: '系统设置', path: '/settings', icon: 'IconSettings', roles: ['admin'] },
    ],
  },
]

/** 扁平化，供路由守卫与面包屑使用 */
export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((group) => group.items)

/** 根据 path 前缀找当前导航项（详情页归属到"工作台"） */
export function findNavByPath(path: string): NavItem | undefined {
  return (
    NAV_ITEMS.find((item) => item.path === path) ??
    NAV_ITEMS.find((item) => item.path !== '/dashboard' && path.startsWith(`${item.path}/`))
  )
}
