// container-tools: the model's tools see only a Docker container.
//
// - tool.list keeps the default tool set (plus extraTools) and drops every
//   other tool, MCP tools included, for the model and every subagent.
// - tool.call answers Bash, Read, Write, Edit and NotebookEdit itself, from
//   inside the container, and never calls next() for them: core would run
//   them on the host. Every failure, ours or the engine's (a throw, a blown
//   budget), becomes a refusal through .catch, so nothing falls through.
// - prompt.compose tells the model where its tools run.

import type { EngineInterface, Register } from 'claude-code'
import { type Config, loadConfig } from './config'
import { ready } from './docker'
import { bash, edit, notebookEdit, read, write } from './tools'
import { ToolFailure, errorText } from './util'

const CONTAINED = new Set(['Bash', 'Read', 'Write', 'Edit', 'NotebookEdit'])

/** Tools that touch neither the host's files nor its processes. */
const KEPT = [
  'TodoWrite',
  'TaskCreate',
  'TaskGet',
  'TaskList',
  'TaskUpdate',
  'Agent',
  'AskUserQuestion',
  'EnterPlanMode',
  'ExitPlanMode',
]

const allowed = (cfg: Config): Set<string> => new Set([...CONTAINED, ...KEPT, ...cfg.extraTools])

const ENVELOPE = new Set(['tool', 'tool_use_id', 'consent', 'agentId', 'requestMeta'])

function summary(e: Record<string, any>): string {
  const s = e.tool === 'Bash' ? String(e.command) : String(e.file_path ?? e.notebook_path ?? '')
  return s.length > 300 ? s.slice(0, 300) + '…' : s
}

/** The session's verdict on the call; an ask goes to the person, or fails where nobody can answer. */
async function permit($: EngineInterface, cfg: Config, e: Record<string, any>): Promise<{ deny: string } | null> {
  const input = Object.fromEntries(Object.entries(e).filter(([k]) => !ENVELOPE.has(k)))
  const verdict = await $.tool.check({ tool: e.tool, input } as any)
  if (verdict.decision === 'allow') return null
  if (verdict.decision === 'deny') return { deny: verdict.reason ?? `${e.tool} is denied by the session's permission rules.` }
  if (cfg.permissions === 'allow') return null
  let answer: string
  try {
    answer = await $.ui.ask(`Run ${e.tool} in container ${cfg.container}?\n${summary(e)}`, {
      header: 'Container',
      options: ['Yes', 'No'],
    } as any)
  } catch {
    return { deny: `${e.tool} needs permission and nobody could be asked${verdict.reason ? ` (${verdict.reason})` : ''}.` }
  }
  return answer === 'Yes' ? null : { deny: `The user refused this ${e.tool} call.` }
}

export const register: Register = (on, options) => {
  // Failing open here would hand the model host tools (tool.call still refuses
  // them), so the fallback filters with the defaults.
  const pool = async (e: unknown, next: (e: any) => Promise<any>, keep: Set<string>) => {
    const r = await next(e)
    if (r.deny !== undefined || !r.value) return r
    return { value: (r.value as { name: string; mcp: boolean }[]).filter(t => !t.mcp && keep.has(t.name)) }
  }
  on('tool.list', async ($, e, next) => pool(e, next, allowed(await loadConfig($, options)))).catch(($, e, next) =>
    pool(e, next, new Set([...CONTAINED, ...KEPT])),
  )

  on('tool.call', async ($, e, next) => {
    const cfg = await loadConfig($, options)
    if (!CONTAINED.has(e.tool)) {
      if (allowed(cfg).has(e.tool)) {
        // hooks.json's PreToolUse backstop refuses every tool unless this is set,
        // so a module that failed to load leaves nothing running on the host.
        await $.env.set('CONTAINER_TOOLS_ACTIVE', '1')
        return next(e)
      }
      return { deny: `${e.tool} is not available here: this session's tools run inside container ${cfg.container}.` }
    }
    const refused = await permit($, cfg, e)
    if (refused) return refused
    try {
      switch (e.tool) {
        case 'Bash':
          return { result: await bash($, cfg, e, next.signal) }
        case 'Read':
          return { result: await read($, cfg, e) }
        case 'Write':
          return { result: await write($, cfg, e) }
        case 'Edit':
          return { result: await edit($, cfg, e) }
        case 'NotebookEdit':
          return { result: await notebookEdit($, cfg, e) }
      }
    } catch (err) {
      if (err instanceof ToolFailure) return { deny: err.message }
      return { deny: `container-tools failed: ${errorText(err)}` }
    }
    return { deny: `${e.tool} is not handled by container-tools.` }
  }).catch(($, e, next) => ({
    deny: `container-tools could not run ${e.tool} in the container (${next.error.kind}${next.error.message ? `: ${next.error.message}` : ''}).`,
  }))

  on('prompt.compose', async ($, e, next) => {
    const r = await next(e)
    const cfg = await loadConfig($, options)
    let workdir = cfg.workdir
    try {
      workdir = (await ready($, cfg)).workdir
    } catch {}
    const text = [
      '# Where your tools run',
      `Your Bash, Read, Write, Edit and NotebookEdit tools run inside the Docker container \`${cfg.container}\`, not on the machine the environment section above describes. They see only the container's files and processes; paths are paths in the container.`,
      workdir ? `The container's working directory is \`${workdir}\`; Bash starts there.` : '',
      'The container may have no network access. A long-running Bash command with run_in_background writes its output to a file in the container, which you read with Read or tail.',
    ]
      .filter(Boolean)
      .join('\n')
    return { ...r, sections: [...r.sections, { id: 'container-tools:where', text, scope: 'session' as const }] }
  })
}
