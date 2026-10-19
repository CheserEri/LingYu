/**
 * dsh-lingyu —— bundle 的自描述插件。
 *
 * 微信能力由本包 `cordis.patch.yml` 插入的 `@deepseek-ai/dsh-mcp-client` 行提供。
 * 这个插件只做一件事：加载时校验随包内置的运行时是否存在，把问题以清晰日志暴露出来，
 * 避免用户只看到 MCP 桥接的 ENOENT 而无从下手。
 */
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-lingyu'

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BUNDLED_EXE = join(PACKAGE_ROOT, 'bin', 'lingyu', 'lingyu.exe')

export function apply(ctx) {
  if (process.env.LINGYU_EXE) return
  if (existsSync(BUNDLED_EXE)) return
  ctx.logger?.warn(
    `[dsh-lingyu] 未找到内置运行时 ${BUNDLED_EXE}；` +
      '请设置 LINGYU_EXE 指向 lingyu.exe，或从 GitHub Release 下载 lingyu-win32-x64.zip 解压后设置该变量。',
  )
}