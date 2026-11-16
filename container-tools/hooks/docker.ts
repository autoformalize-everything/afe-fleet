// The Docker Engine API over $.http.fetch: where the daemon is (the docker
// CLI's own environment variables and current context), the container's
// lifecycle, and running scripts in it.
//
// $.http.fetch hands back bodies as text, so everything read from the
// container is made ASCII first: execs run with a tty (one raw stream, no
// binary multiplexing headers) and scripts print base64 between markers.

import type { EngineInterface } from 'claude-code'
import type { Config } from './config'
import { ToolFailure, errorText, randomHex, shellQuote, tarArchive } from './util'

type $ = EngineInterface

export type Endpoint = { base: string; prefix: string; socketPath?: string; where: string }

class DockerError extends ToolFailure {
  constructor(
    message: string,
    readonly status = 0,
  ) {
    super(message)
  }
}

// ---------------------------------------------------------------- endpoint

async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s))
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
}

function parseHost(host: string, tls: boolean, origin: string): Omit<Endpoint, 'prefix'> {
  if (host.startsWith('unix://')) {
    const path = host.slice('unix://'.length)
    return { base: 'http://docker', socketPath: path, where: `${host} (${origin})` }
  }
  if (host.startsWith('tcp://') || host.startsWith('http://')) {
    if (tls) throw new DockerError(`Docker at ${host} (${origin}) needs TLS client certificates, which container-tools cannot send`)
    const rest = host.replace(/^(tcp|http):\/\//, '')
    const hostPort = /:\d+$/.test(rest.split('/')[0] ?? '') ? rest : `${rest}:2375`
    return { base: `http://${hostPort.replace(/\/+$/, '')}`, where: `${host} (${origin})` }
  }
  throw new DockerError(`Docker host ${host} (${origin}) is not supported; use unix:// or tcp://`)
}

let endpoint: Endpoint | undefined

/** DOCKER_HOST, else DOCKER_CONTEXT or config.json's currentContext, else the default socket. */
export async function resolveEndpoint($: $): Promise<Endpoint> {
  if (endpoint) return endpoint
  const version = await $.env.get('DOCKER_API_VERSION')
  const prefix = version ? `/v${version.replace(/^v/, '')}` : ''
  const tls = !!(await $.env.get('DOCKER_TLS_VERIFY'))
  const host = await $.env.get('DOCKER_HOST')
  if (host) return (endpoint = { ...parseHost(host, tls, 'DOCKER_HOST'), prefix })

  const home = (await $.env.get('HOME')) ?? ''
  const configDir = (await $.env.get('DOCKER_CONFIG')) ?? `${home}/.docker`
  let context = await $.env.get('DOCKER_CONTEXT')
  let origin = 'DOCKER_CONTEXT'
  if (!context) {
    origin = `${configDir}/config.json`
    try {
      context = JSON.parse(await $.fs.read(`${configDir}/config.json`))?.currentContext
    } catch {
      context = undefined
    }
  }
  if (context && context !== 'default') {
    const dir = `${configDir}/contexts/meta/${await sha256Hex(context)}`
    let meta: any
    try {
      meta = JSON.parse(await $.fs.read(`${dir}/meta.json`))
    } catch (err) {
      throw new DockerError(`Docker context "${context}" (from ${origin}) could not be read at ${dir}/meta.json: ${errorText(err)}`)
    }
    const h = meta?.Endpoints?.docker?.Host
    if (typeof h !== 'string') throw new DockerError(`Docker context "${context}" names no docker endpoint`)
    const ctxTls = await $.fs.exists(`${configDir}/contexts/tls/${await sha256Hex(context)}/docker`)
    return (endpoint = { ...parseHost(h, tls || ctxTls, `context ${context}`), prefix })
  }
  return (endpoint = { ...parseHost('unix:///var/run/docker.sock', false, 'default'), prefix })
}

// ---------------------------------------------------------------- requests

type Body = string | object | undefined

async function request($: $, method: string, path: string, body?: Body, contentType = 'application/json') {
  const ep = await resolveEndpoint($)
  const init: Parameters<$['http']['fetch']>[1] = { method, headers: {} }
  if (ep.socketPath) init.socketPath = ep.socketPath
  if (body !== undefined) {
    init.headers = { 'Content-Type': contentType }
    init.body = typeof body === 'string' ? body : JSON.stringify(body)
  }
  try {
    return await $.http.fetch(ep.base + ep.prefix + path, init)
  } catch (err) {
    throw new DockerError(`cannot reach the Docker daemon at ${ep.where}: ${errorText(err)}`)
  }
}

function failure(what: string, res: { status: number; text: string }): DockerError {
  let msg = res.text.trim()
  try {
    msg = JSON.parse(res.text).message ?? msg
  } catch {}
  return new DockerError(`${what}: ${msg || `HTTP ${res.status}`}`, res.status)
}

async function json($: $, what: string, method: string, path: string, body?: Body): Promise<any> {
  const res = await request($, method, path, body)
  if (!res.ok) throw failure(what, res)
  return res.text ? JSON.parse(res.text) : {}
}

// ---------------------------------------------------------------- lifecycle

/** What a ready container is known to have, probed once per start. */
export type Box = {
  id: string
  startedAt: string
  user: string
  uid: number
  gid: number
  shell: string
  setsid: boolean
  workdir: string
  scratch: string
}

let box: Box | undefined

const enc = encodeURIComponent

async function inspect($: $, name: string): Promise<any | null> {
  const res = await request($, 'GET', `/containers/${enc(name)}/json`)
  if (res.status === 404) return null
  if (!res.ok) throw failure(`inspecting container ${name}`, res)
  return JSON.parse(res.text)
}

const KEEPALIVE = "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"

async function create($: $, cfg: Config): Promise<void> {
  const spec = {
    Image: cfg.image,
    Entrypoint: ['/bin/sh', '-c', KEEPALIVE],
    Cmd: [],
    WorkingDir: cfg.workdir || undefined,
    User: cfg.user || undefined,
    Labels: { 'afe-fleet.container-tools': '1' },
    HostConfig: { Init: true, NetworkMode: cfg.network || 'none', Binds: cfg.mounts.length ? cfg.mounts : undefined },
  }
  const path = `/containers/create?name=${enc(cfg.container)}`
  let res = await request($, 'POST', path, spec)
  if (res.status === 404) {
    // The image is missing: pull it once (the daemon's network, not the container's).
    const [repo, tag] = splitImage(cfg.image)
    const pull = await request($, 'POST', `/images/create?fromImage=${enc(repo)}&tag=${enc(tag)}`, '')
    const last = pull.text.trim().split('\n').pop() ?? ''
    if (!pull.ok || /"error"/.test(last)) throw failure(`pulling image ${cfg.image}`, { status: pull.status, text: last })
    res = await request($, 'POST', path, spec)
  }
  if (res.status === 409) return // created meanwhile by someone else
  if (!res.ok) throw failure(`creating container ${cfg.container} from ${cfg.image}`, res)
}

function splitImage(image: string): [string, string] {
  if (image.includes('@')) return [image, '']
  const slash = image.lastIndexOf('/')
  const colon = image.lastIndexOf(':')
  return colon > slash ? [image.slice(0, colon), image.slice(colon + 1)] : [image, 'latest']
}

/** Brings the container up: create (if an image is configured), unpause, start; retried. */
async function ensureRunning($: $, cfg: Config): Promise<any> {
  let last = ''
  for (let attempt = 1; attempt <= cfg.startAttempts; attempt++) {
    try {
      let info = await inspect($, cfg.container)
      if (!info) {
        if (!cfg.image)
          throw new ToolFailure(`container ${cfg.container} does not exist and no image is configured to create it from`)
        await create($, cfg)
        info = await inspect($, cfg.container)
        if (!info) throw new DockerError(`container ${cfg.container} vanished right after it was created`)
      }
      const st = info.State ?? {}
      if (st.Running && !st.Paused && !st.Restarting) return info
      if (st.Paused) {
        await json($, `unpausing container ${cfg.container}`, 'POST', `/containers/${enc(info.Id)}/unpause`)
      } else if (!st.Running || st.Restarting) {
        const res = await request($, 'POST', `/containers/${enc(info.Id)}/start`)
        if (!res.ok && res.status !== 304) throw failure(`starting container ${cfg.container}`, res)
      }
      info = await inspect($, cfg.container)
      const now = info?.State ?? {}
      if (now.Running && !now.Paused && !now.Restarting) return info
      last = `container ${cfg.container} is not running after start (status ${now.Status}, exit code ${now.ExitCode}${now.Error ? `, ${now.Error}` : ''})`
    } catch (err) {
      if (err instanceof ToolFailure && !(err instanceof DockerError)) throw err // not worth retrying
      last = errorText(err)
    }
    if (attempt < cfg.startAttempts) await $.clock.sleep(400 * attempt)
  }
  throw new ToolFailure(`container ${cfg.container} could not be started after ${cfg.startAttempts} attempts: ${last}`)
}

// ---------------------------------------------------------------- scripts

export type Sections = Record<string, string>

/**
 * Runs a /bin/sh script in the container with a tty and parses its sections:
 * `emit NAME` prints a marker line, and what follows up to the next marker is
 * that section (carriage returns removed). The script must `emit end` last.
 */
async function execScript($: $, id: string, user: string, script: string, args: string[]): Promise<Sections> {
  const nonce = randomHex(6)
  const prelude = `N=${nonce}\nemit() { printf '\\n@@%s:%s@@\\n' "$N" "$1"; }\n`
  const created = await json($, 'creating an exec', 'POST', `/containers/${enc(id)}/exec`, {
    AttachStdin: false,
    AttachStdout: true,
    AttachStderr: true,
    Tty: true,
    User: user || undefined,
    Cmd: ['/bin/sh', '-c', prelude + script, 'sh', ...args],
  })
  const res = await request($, 'POST', `/exec/${enc(created.Id)}/start`, { Detach: false, Tty: true })
  if (!res.ok) throw failure('starting an exec', res)
  const raw = res.text.replace(/\r/g, '')
  const marker = new RegExp(`\\n@@${nonce}:([a-z0-9_]+)@@\\n`, 'g')
  const out: Sections = {}
  const hits = [...raw.matchAll(marker)]
  hits.forEach((m, i) => {
    const from = (m.index ?? 0) + m[0].length
    const to = hits[i + 1]?.index ?? raw.length
    out[m[1] ?? ''] = raw.slice(from, to).replace(/\n$/, '')
  })
  if (!('end' in out)) {
    const shown = raw.trim().slice(0, 2000)
    throw new ToolFailure(`the helper script in the container did not finish${shown ? `; it printed:\n${shown}` : ''}`)
  }
  return out
}

const PROBE = `
emit uid; id -u
emit gid; id -g
emit bash; command -v bash || true
emit base64; command -v base64 || true
emit setsid; command -v setsid || true
emit wd; if [ -n "$1" ]; then mkdir -p "$1" 2>&1 && [ -d "$1" ] && echo ok; else pwd; fi
sc="$2/.container-tools-$(id -u)"
emit scratch; echo "$sc"
emit scratchok; mkdir -p "$sc" 2>&1 && chmod 700 "$sc" 2>&1 && [ -w "$sc" ] && echo ok
emit end
`

/** The running container, probed; started (with retries) when it is not. */
export async function ready($: $, cfg: Config): Promise<Box> {
  const info = await ensureRunning($, cfg)
  const startedAt = String(info.State?.StartedAt ?? '')
  if (box && box.id === info.Id && box.startedAt === startedAt) return box
  const user = cfg.user || info.Config?.User || ''
  const s = await execScript($, info.Id, user, PROBE, [cfg.workdir || info.Config?.WorkingDir || '', cfg.tmpdir])
  if (!s.base64) throw new ToolFailure(`container ${cfg.container} has no base64 command, which container-tools needs`)
  if (s.scratchok !== 'ok') throw new ToolFailure(`cannot use ${s.scratch} in container ${cfg.container}: ${s.scratchok}`)
  const workdir = cfg.workdir || info.Config?.WorkingDir || s.wd || '/'
  if (cfg.workdir && s.wd !== 'ok') throw new ToolFailure(`working directory ${cfg.workdir} is unusable in container ${cfg.container}: ${s.wd}`)
  const probed: Box = {
    id: info.Id,
    startedAt,
    user,
    uid: Number(s.uid),
    gid: Number(s.gid),
    shell: s.bash ? 'bash' : 'sh',
    setsid: !!s.setsid,
    workdir,
    scratch: s.scratch ?? '',
  }
  box = probed
  return probed
}

/** Runs a script in the container, bringing it up first and once more if it stopped meanwhile. */
export async function run($: $, cfg: Config, script: string, args: string[]): Promise<{ box: Box; s: Sections }> {
  let b = await ready($, cfg)
  try {
    return { box: b, s: await execScript($, b.id, b.user, script, args) }
  } catch (err) {
    if (!(err instanceof DockerError) || (err.status !== 409 && err.status !== 404)) throw err
    box = undefined
    b = await ready($, cfg)
    return { box: b, s: await execScript($, b.id, b.user, script, args) }
  }
}

/** Writes `content` (UTF-8) to a new file at `path` (absolute) in the container, owned by the tools' user. */
export async function upload($: $, b: Box, path: string, content: string): Promise<void> {
  const archive = tarArchive([{ name: path.replace(/^\/+/, ''), content, mode: 0o600, uid: b.uid, gid: b.gid }])
  const res = await request($, 'PUT', `/containers/${enc(b.id)}/archive?path=%2F&copyUIDGID=1`, archive, 'application/x-tar')
  if (!res.ok) throw failure(`copying into container`, res)
}

export { shellQuote }
