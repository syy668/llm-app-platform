<script setup lang="ts">
// 用户管理页（仅管理员）：列表筛选 + 新建 + 编辑 + 重置密码 + 删除（软删除）。
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import { useUserStore } from '@/store/useUserStore'
import {
  createUser,
  deleteUser,
  listUsers,
  resetPassword,
  updateUser,
} from '@/api/user'
import type { User } from '@/types'
import EmptyState from '@/components/EmptyState.vue'
import PagePagination from '@/components/PagePagination.vue'
import SkeletonLoading from '@/components/SkeletonLoading.vue'

const store = useUserStore()

const filterKeyword = ref('')
const filterRole = ref('')
const users = ref<User[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const loading = ref(false)

// 新建 / 编辑共用对话框
const dialogVisible = ref(false)
const editing = ref<User | null>(null)
const form = reactive({
  username: '',
  password: '',
  display_name: '',
  email: '',
  role: 'user' as 'admin' | 'user',
  status: 'active' as 'active' | 'disabled',
})
const saving = ref(false)

async function loadUsers() {
  loading.value = true
  try {
    const params: Record<string, unknown> = { page: page.value, size: size.value }
    if (filterKeyword.value.trim()) params.keyword = filterKeyword.value.trim()
    if (filterRole.value) params.role = filterRole.value
    const res = await listUsers(params)
    users.value = res.items
    total.value = res.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { username: '', password: '', display_name: '', email: '', role: 'user', status: 'active' })
  dialogVisible.value = true
}

function openEdit(u: User) {
  editing.value = u
  Object.assign(form, {
    username: u.username,
    password: '',
    display_name: u.display_name || '',
    email: u.email || '',
    role: (u.role === 'admin' ? 'admin' : 'user') as 'admin' | 'user',
    status: (u.status === 'disabled' ? 'disabled' : 'active') as 'active' | 'disabled',
  })
  dialogVisible.value = true
}

async function save() {
  if (!editing.value) {
    if (form.username.trim().length < 3) return ElMessage.warning('用户名至少 3 个字符')
    if (form.password.length < 8) return ElMessage.warning('初始密码至少 8 位')
  }
  if (form.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email)) return ElMessage.warning('邮箱格式不正确')
  saving.value = true
  try {
    if (editing.value) {
      await updateUser(editing.value.id, {
        display_name: form.display_name.trim() || null,
        email: form.email.trim() || null,
        role: form.role,
        status: form.status,
      })
      ElMessage.success('已保存')
    } else {
      await createUser({
        username: form.username.trim(),
        password: form.password,
        display_name: form.display_name.trim() || null,
        email: form.email.trim() || null,
        role: form.role,
      })
      ElMessage.success('用户已创建')
    }
    dialogVisible.value = false
    await loadUsers()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    saving.value = false
  }
}

async function onResetPassword(u: User) {
  try {
    const { value } = await ElMessageBox.prompt(
      `为用户「${u.display_name || u.username}」设置新密码（至少 8 位）`,
      '重置密码',
      {
        inputType: 'password',
        inputValidator: (v: string) => (v && v.length >= 8 ? true : '密码至少 8 位'),
        confirmButtonText: '重置',
        cancelButtonText: '取消',
      },
    )
    await resetPassword(u.id, value)
    ElMessage.success('密码已重置')
  } catch {
    /* 用户取消 */
  }
}

async function onDelete(u: User) {
  try {
    await ElMessageBox.confirm(
      `删除采用软删除：账号「${u.display_name || u.username}」将被禁用而不可登录，历史数据保留。确定删除吗？`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await deleteUser(u.id)
    ElMessage.success('已禁用该账号')
    await loadUsers()
  } catch {
    /* 错误已由拦截器提示（含 409 不能禁用自己） */
  }
}

onMounted(loadUsers)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">用户管理</div>
        <div class="page-desc">账号的创建、角色分配、启用禁用与密码重置</div>
      </div>
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon>新建用户</el-button>
    </div>

    <div class="filter-bar card">
      <div class="filter-item">
        <span class="field-label">关键字</span>
        <el-input
          v-model="filterKeyword"
          placeholder="用户名 / 显示名"
          style="width: 180px"
          clearable
          @keyup.enter="page = 1; loadUsers()"
        />
      </div>
      <div class="filter-item">
        <span class="field-label">角色</span>
        <el-select v-model="filterRole" clearable placeholder="全部" style="width: 130px">
          <el-option label="管理员" value="admin" />
          <el-option label="普通用户" value="user" />
        </el-select>
      </div>
      <el-button type="primary" @click="page = 1; loadUsers()"><el-icon><Search /></el-icon>查询</el-button>
      <el-button @click="filterKeyword = ''; filterRole = ''; page = 1; loadUsers()">重置</el-button>
    </div>

    <div class="card">
      <SkeletonLoading v-if="loading" :rows="6" />
      <template v-else>
        <el-table :data="users" style="width: 100%">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="username" label="用户名" min-width="120" show-overflow-tooltip />
          <el-table-column label="显示名" min-width="110">
            <template #default="{ row }">{{ row.display_name || '—' }}</template>
          </el-table-column>
          <el-table-column label="邮箱" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.email || '—' }}</template>
          </el-table-column>
          <el-table-column label="角色" width="100">
            <template #default="{ row }">
              <span class="ui-tag" :class="row.role === 'admin' ? 'tag-blue' : 'tag-gray'">
                {{ row.role === 'admin' ? '管理员' : '普通用户' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <span class="ui-tag" :class="row.status === 'active' ? 'tag-green' : 'tag-red'">
                {{ row.status === 'active' ? '启用' : '禁用' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ (row.created_at || '').slice(0, 19).replace('T', ' ') }}</template>
          </el-table-column>
          <el-table-column label="操作" width="240" fixed="right">
            <template #default="{ row }">
              <div class="row-actions">
                <el-button text type="primary" size="small" @click="openEdit(row)">编辑</el-button>
                <el-button text size="small" @click="onResetPassword(row)">重置密码</el-button>
                <el-button text type="danger" size="small" :disabled="row.id === store.user?.id" @click="onDelete(row)">删除</el-button>
              </div>
            </template>
          </el-table-column>
          <template #empty><EmptyState title="暂无用户" desc="调整筛选条件或新建用户" icon="👤" /></template>
        </el-table>
        <PagePagination
          :total="total"
          :page="page"
          :size="size"
          @update:page="(p: number) => { page = p; loadUsers() }"
          @update:size="(s: number) => { size = s; page = 1; loadUsers() }"
        />
      </template>
    </div>

    <!-- 新建 / 编辑用户对话框 -->
    <el-dialog v-model="dialogVisible" :title="editing ? `编辑用户：${editing.username}` : '新建用户'" width="440px">
      <el-form label-position="top" @submit.prevent>
        <el-form-item v-if="!editing" label="用户名">
          <el-input v-model="form.username" placeholder="至少 3 个字符" />
        </el-form-item>
        <el-form-item v-if="!editing" label="初始密码">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" placeholder="选填" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" placeholder="选填" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option label="普通用户" value="user" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="editing" label="状态">
          <el-switch
            v-model="form.status"
            active-value="active"
            inactive-value="disabled"
            active-text="启用"
            inactive-text="禁用"
          />
          <div v-if="editing.id === store.user?.id" class="small muted" style="margin-top: 4px">不能禁用当前登录的自己</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ editing ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 操作列：三个文字按钮等距对齐，避免默认间距不均导致的拥挤/错位 */
.row-actions { display: flex; align-items: center; }
.row-actions .el-button { padding: 5px 10px; margin: 0; }
.row-actions .el-button + .el-button { margin-left: 0; }
</style>
