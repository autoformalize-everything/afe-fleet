import type { EngineInterface, PluginOptions } from 'claude-code'

export type Config = {
  container: string
  image: string
  workdir: string
  network: string
  mounts: string[]
  user: string
  permissions: 'session' | 'allow'
  extraTools: string[]
  tmpdir: string
  startAttempts: number
}

const list = (s: string): string[] =>
  s
    .split(',')
    .map(x => x.trim())
    .filter(Boolean)

let cached: Config | undefined

/** The plugin's options, each overridden by its CONTAINER_TOOLS_* variable when set. */
export async function loadConfig($: EngineInterface, options: PluginOptions): Promise<Config> {
  if (cached) return cached
  const opt = (k: string, d: string): string => {
    const v = options[k]
    return v === undefined || v === null ? d : String(v)
  }
  const env = {
    container: await $.env.get('CONTAINER_TOOLS_CONTAINER'),
    image: await $.env.get('CONTAINER_TOOLS_IMAGE'),
    workdir: await $.env.get('CONTAINER_TOOLS_WORKDIR'),
    network: await $.env.get('CONTAINER_TOOLS_NETWORK'),
    mounts: await $.env.get('CONTAINER_TOOLS_MOUNTS'),
    user: await $.env.get('CONTAINER_TOOLS_USER'),
    permissions: await $.env.get('CONTAINER_TOOLS_PERMISSIONS'),
    extraTools: await $.env.get('CONTAINER_TOOLS_EXTRA_TOOLS'),
    tmpdir: await $.env.get('CONTAINER_TOOLS_TMPDIR'),
    startAttempts: await $.env.get('CONTAINER_TOOLS_START_ATTEMPTS'),
  }
  const pick = (k: keyof typeof env, d: string): string => env[k] ?? opt(k, d)
  const permissions = pick('permissions', 'session')
  const attempts = Number(pick('startAttempts', '3'))
  cached = {
    container: pick('container', 'claude-sandbox'),
    image: pick('image', ''),
    workdir: pick('workdir', ''),
    network: pick('network', ''),
    mounts: list(pick('mounts', '')),
    user: pick('user', ''),
    permissions: permissions === 'allow' ? 'allow' : 'session',
    extraTools: list(pick('extraTools', '')),
    tmpdir: pick('tmpdir', '/tmp').replace(/\/+$/, '') || '/tmp',
    startAttempts: Number.isFinite(attempts) && attempts >= 1 ? Math.floor(attempts) : 3,
  }
  return cached
}
