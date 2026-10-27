/**
 * 文件下载工具（导出类接口专用）。
 *
 * 后端导出接口返回的是**文件流**（`Content-Disposition: attachment`），
 * 不是统一的 `{code, data}` 信封，因此不能走 `api.get`（它会把 data 解包掉），
 * 这里直接用 axios 实例取 `Blob` 并从响应头里取文件名。
 */
import { http } from '@/api/http'

/** 从 Content-Disposition 里取 filename（后端统一用 ASCII 文件名） */
function filenameFrom(header: unknown): string | null {
  if (typeof header !== 'string') return null
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (star) {
    try {
      return decodeURIComponent(star[1])
    } catch {
      return star[1]
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header)
  return plain ? plain[1] : null
}

/**
 * 下载纯文本（前端本地拼装，不经过后端）。
 *
 * 用于"报告另存为 Markdown"这类前端就能组装的交付物：
 * 加 BOM 是为了让 Windows 上的记事本 / Excel 正确识别 UTF-8 中文。
 */
export function downloadText(
  filename: string,
  text: string,
  mime = 'text/markdown;charset=utf-8',
): void {
  const blob = new Blob(['\ufeff', text], { type: mime })
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
}

/**
 * 下载后端返回的 JSON 文件。
 *
 * @param url      以 `/api/v1` 前缀为基准的相对路径，例如 `/tasks/12/export`
 * @param fallback 响应头没有文件名时使用的默认文件名
 * @returns 实际保存的文件名
 */
export async function downloadJson(url: string, fallback: string): Promise<string> {
  const response = await http.get<Blob>(url, { responseType: 'blob', timeout: 60000 })
  const filename = filenameFrom(response.headers?.['content-disposition']) || fallback
  const blob = response.data instanceof Blob ? response.data : new Blob([response.data])
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // 释放略延后，避免部分浏览器还没开始下载就被回收
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
  return filename
}
