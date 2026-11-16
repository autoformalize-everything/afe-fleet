// Bash, Read, Write, Edit and NotebookEdit, answered from inside the container.

import type { BuiltinToolInputs, BuiltinToolResults, EngineInterface } from 'claude-code'
import type { Config } from './config'
import { type Box, ready, run, upload } from './docker'
import { ToolFailure, base64Bytes, base64Text, cleanBase64, makePatch, randomHex, resolvePath, utf8Length } from './util'

type $ = EngineInterface

// ---------------------------------------------------------------- session state

/** The shell's directory between Bash calls (null until the first call: the container's working directory). */
let shellCwd: string | null = null

/** What each file looked like when the model last read or wrote it: `mtime:size`. */
const seen = new Map<string, string>()

async function cwdOf($: $, cfg: Config): Promise<string> {
  return shellCwd ?? (await ready($, cfg)).workdir
}

async function absolute($: $, cfg: Config, p: string): Promise<string> {
  return resolvePath(p, await cwdOf($, cfg))
}

// ---------------------------------------------------------------- Bash

// Per stream; stdout and stderr together stay under the size at which Claude Code
// saves a result to a file on the host, where the model could not read it.
const HEAD_CAP = 8_000
const TAIL_CAP = 3_000

// $1 run dir, $2 cwd, $3 fallback cwd, $4 timeout (s), $5 head cap, $6 tail cap,
// $7 command ('' when uploaded to $1/cmd), $8 shell, $9 1 to use setsid, $10 background
const BASH = `
d=$1; hc=$5; tc=$6; sh_=$8
mkdir -p "$d" || { emit fatal; echo "cannot create $d"; emit end; exit 0; }
[ -n "$7" ] && printf '%s\\n' "$7" > "$d/cmd"
cd "$2" 2>/dev/null || cd "$3" 2>/dev/null || cd /
if [ "\${10}" = 1 ]; then
  # This shell exits at once and the tty's hangup could reach the job before
  # setsid detaches it: the job ignores SIGHUP from the start, as under nohup.
  trap '' HUP
  inner="trap 'echo \\$? > \\"$d/exitcode\\"' EXIT
. \\"$d/cmd\\""
  if [ "$9" = 1 ]; then setsid "$sh_" -c "$inner" </dev/null >"$d/output" 2>&1 &
  else "$sh_" -c "$inner" </dev/null >"$d/output" 2>&1 & fi
  emit pid; echo $!
  emit end
  exit 0
fi
inner="trap 'pwd -P > \\"$d/cwd\\"' EXIT
. \\"$d/cmd\\""
if [ "$9" = 1 ]; then setsid "$sh_" -c "$inner" </dev/null >"$d/out" 2>"$d/err" &
else set -m 2>/dev/null; "$sh_" -c "$inner" </dev/null >"$d/out" 2>"$d/err" & fi
pid=$!
echo "$pid" > "$d/pid"
( trap 'kill $sp 2>/dev/null; exit 0' TERM
  sleep "$4" & sp=$!; wait $sp
  echo 1 > "$d/timedout"
  kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  sleep 2
  kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null ) </dev/null >/dev/null 2>&1 &
wd=$!
wait "$pid"; rc=$?
kill "$wd" 2>/dev/null
emit rc; echo "$rc"
emit timedout; [ -e "$d/timedout" ] && echo 1
emit interrupted; [ -e "$d/interrupted" ] && echo 1
emit cwd; cat "$d/cwd" 2>/dev/null
keep="$(dirname "$d")/output"
for s in out err; do
  n=$(wc -c < "$d/$s" | tr -d ' ')
  emit "\${s}size"; echo "$n"
  if [ "$n" -gt $((hc + tc)) ]; then
    emit "$s"; head -c "$hc" "$d/$s" | base64
    emit "\${s}tail"; tail -c "$tc" "$d/$s" | base64
    f="$keep/$(basename "$d").$s"
    mkdir -p "$keep" && mv "$d/$s" "$f" && { emit "\${s}path"; echo "$f"; }
  else
    emit "$s"; base64 < "$d/$s"
  fi
done
rm -rf "$d"
emit end
`

// $1 run dir
const KILL = `
p=$(cat "$1/pid" 2>/dev/null)
if [ -n "$p" ]; then
  echo 1 > "$1/interrupted"
  kill -TERM -"$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null
  sleep 1
  kill -KILL -"$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null
fi
emit end
`

function stream(s: Record<string, string>, name: 'out' | 'err'): string {
  const head = base64Text(s[name] ?? '')
  if (!(`${name}tail` in s)) return head
  const size = Number(s[`${name}size`])
  const omitted = size - HEAD_CAP - TAIL_CAP
  const where = s[`${name}path`] ? `; the whole output is in the container at ${s[`${name}path`]}` : ''
  return `${head}\n\n[... ${omitted} bytes left out${where} ...]\n\n${base64Text(s[`${name}tail`] ?? '')}`
}

export async function bash($: $, cfg: Config, e: BuiltinToolInputs['Bash'], signal: AbortSignal): Promise<BuiltinToolResults['Bash']> {
  const b = await ready($, cfg)
  const runDir = `${b.scratch}/run-${randomHex(6)}`
  const timeoutMs = Math.min(Math.max(e.timeout ?? 120_000, 1_000), 600_000)
  let command = e.command
  if (utf8Length(command) > 100_000) {
    await upload($, b, `${runDir}/cmd`, command + '\n')
    command = ''
  }
  const cwd = await cwdOf($, cfg)
  const args = [runDir, cwd, b.workdir, String(Math.ceil(timeoutMs / 1000)), String(HEAD_CAP), String(TAIL_CAP), command, b.shell, b.setsid ? '1' : '0']

  if (e.run_in_background) {
    const { s } = await run($, cfg, BASH, [...args, '1'])
    if (s.fatal) throw new ToolFailure(s.fatal)
    return {
      stdout:
        `Started in the background inside the container (pid ${s.pid}, its own process group).\n` +
        `Output (stdout and stderr): ${runDir}/output\nExit status, once it ends: ${runDir}/exitcode\n` +
        `Read the output with Read or tail; stop it with kill -TERM -${s.pid}.`,
      stderr: '',
      interrupted: false,
    }
  }

  const onAbort = () => void run($, cfg, KILL, [runDir]).catch(() => {})
  signal.addEventListener('abort', onAbort, { once: true })
  let s: Record<string, string>
  try {
    s = (await run($, cfg, BASH, [...args, '0'])).s
  } finally {
    signal.removeEventListener('abort', onAbort)
  }
  if (s.fatal) throw new ToolFailure(s.fatal)
  if (s.cwd) shellCwd = s.cwd
  const rc = Number(s.rc)
  const stdout = stream(s, 'out').replace(/\n+$/, '')
  let stderr = stream(s, 'err').replace(/\n+$/, '')
  const timedOut = s.timedout === '1'
  const interrupted = timedOut || s.interrupted === '1'
  if (timedOut) stderr += `${stderr ? '\n' : ''}Command timed out after ${timeoutMs} ms and was killed.`
  if (rc !== 0 && !interrupted) stderr += `${stderr ? '\n' : ''}Exit code ${rc}`
  return { stdout, stderr, interrupted }
}

// ---------------------------------------------------------------- reading

// $1 path, $2 mode (lines|bytes), $3 first line, $4 last line (0: to the end), $5 byte cap
const READ = `
f=$1
emit kind
if [ -d "$f" ]; then echo dir; elif [ ! -e "$f" ]; then echo missing; elif [ ! -r "$f" ]; then echo unreadable; else echo file; fi
if [ -f "$f" ] && [ -r "$f" ]; then
  emit stamp; stat -c '%Y:%s' "$f" 2>/dev/null || wc -c < "$f"
  emit size; wc -c < "$f" | tr -d ' '
  if [ "$2" = bytes ]; then
    emit data; head -c "$5" "$f" | base64
  else
    a=$(head -c 8000 "$f" | wc -c); b=$(head -c 8000 "$f" | tr -d '\\000' | wc -c)
    emit binary; [ "$a" -ne "$b" ] && echo 1
    emit lines; wc -l < "$f" | tr -d ' '
    emit lastnl; [ -s "$f" ] && [ "$(tail -c 1 "$f" | wc -l | tr -d ' ')" = 1 ] && echo 1
    emit data
    if [ "$4" -gt 0 ]; then sed -n "$3,$4p;$4q" "$f"; else sed -n "$3,\\$p" "$f"; fi | head -c "$5" | base64
  fi
fi
emit end
`

type Raw = { kind: string; stamp: string; size: number; data: string; s: Record<string, string> }

async function readRaw($: $, cfg: Config, path: string, mode: 'lines' | 'bytes', cap: number, first = 1, last = 0): Promise<Raw> {
  const { s } = await run($, cfg, READ, [path, mode, String(first), String(last), String(cap)])
  return { kind: s.kind ?? '', stamp: s.stamp ?? '', size: Number(s.size ?? 0), data: s.data ?? '', s }
}

function mustBeFile(path: string, kind: string): void {
  if (kind === 'missing') throw new ToolFailure(`File does not exist: ${path}`)
  if (kind === 'dir') throw new ToolFailure(`${path} is a directory, not a file. Use Bash (ls) to list it.`)
  if (kind === 'unreadable') throw new ToolFailure(`Permission denied reading ${path}`)
  if (kind !== 'file') throw new ToolFailure(`${path} is not a regular file`)
}

/** The whole file as text, for editing. */
async function readWhole($: $, cfg: Config, path: string): Promise<{ exists: boolean; text: string; stamp: string }> {
  const MAX = 16 * 1024 * 1024
  const r = await readRaw($, cfg, path, 'bytes', MAX + 1)
  if (r.kind === 'missing') return { exists: false, text: '', stamp: '' }
  mustBeFile(path, r.kind)
  if (r.size > MAX) throw new ToolFailure(`${path} is larger than 16 MiB; edit it with Bash instead`)
  return { exists: true, text: base64Text(r.data), stamp: r.stamp }
}

const IMAGE_TYPES: Record<string, 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp'> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
}

export async function read($: $, cfg: Config, e: BuiltinToolInputs['Read']): Promise<BuiltinToolResults['Read']> {
  const path = await absolute($, cfg, e.file_path)
  const ext = (path.split('/').pop() ?? '').split('.').pop()?.toLowerCase() ?? ''

  const imageType = IMAGE_TYPES[ext]
  if (imageType || ext === 'pdf') {
    if (ext === 'pdf' && e.pages) throw new ToolFailure('Page ranges are not supported for PDFs in the container; extract pages with Bash (pdftotext, pdftoppm) instead.')
    const max = ext === 'pdf' ? 20 * 1024 * 1024 : 3_750_000
    const r = await readRaw($, cfg, path, 'bytes', max + 1)
    mustBeFile(path, r.kind)
    if (r.size > max) throw new ToolFailure(`${path} is ${r.size} bytes, over the ${max}-byte limit for reading it whole from the container; shrink or split it with Bash first.`)
    seen.set(path, r.stamp)
    const base64 = cleanBase64(r.data)
    if (ext === 'pdf') return { type: 'pdf', file: { filePath: path, base64, originalSize: r.size } }
    return { type: 'image', file: { base64, type: imageType ?? 'image/png', originalSize: r.size } }
  }

  const first = Math.max(1, Math.floor(e.offset ?? 1))
  const limit = e.limit && e.limit > 0 ? Math.floor(e.limit) : 2000
  const cap = e.allow_large ? 4 * 1024 * 1024 : 256 * 1024
  const r = await readRaw($, cfg, path, 'lines', cap + 1, first, first + limit - 1)
  mustBeFile(path, r.kind)
  if (r.s.binary === '1') throw new ToolFailure(`${path} looks binary (it holds NUL bytes); inspect it with Bash (file, xxd, od) instead.`)
  const bytes = base64Bytes(r.data)
  const truncated = bytes.length > cap
  let text = new TextDecoder().decode(truncated ? bytes.subarray(0, cap) : bytes)
  if (truncated) text = text.slice(0, Math.max(0, text.lastIndexOf('\n') + 1))
  const lines = text === '' ? [] : text.replace(/\n$/, '').split('\n')
  const totalLines = Number(r.s.lines) + (r.size > 0 && r.s.lastnl !== '1' ? 1 : 0)
  seen.set(path, r.stamp)
  return {
    type: 'text',
    file: {
      filePath: path,
      content: lines.join('\n'),
      numLines: lines.length,
      startLine: first,
      totalLines,
      ...(truncated ? { truncatedByTokenCap: true } : {}),
    },
  }
}

// ---------------------------------------------------------------- writing

// $1 uploaded temp file, $2 target
const WRITE = `
t=$2
if [ -d "$t" ]; then emit error; echo "$t is a directory"; rm -f "$1"; emit end; exit 0; fi
if [ ! -f "$1" ]; then emit error; echo "the uploaded copy is missing (is $(dirname "$1") a tmpfs mount? set tmpdir to a directory on the container's filesystem)"; emit end; exit 0; fi
mkdir -p "$(dirname "$t")" 2>/dev/null
if { cat "$1" > "$t"; } 2>"$1.err"; then
  emit stamp; stat -c '%Y:%s' "$t" 2>/dev/null || wc -c < "$t"
else
  emit error; cat "$1.err"
fi
rm -f "$1" "$1.err"
emit end
`

async function writeText($: $, cfg: Config, path: string, content: string): Promise<void> {
  const b: Box = await ready($, cfg)
  const tmp = `${b.scratch}/up-${randomHex(6)}`
  await upload($, b, tmp, content)
  const { s } = await run($, cfg, WRITE, [tmp, path])
  if (s.error !== undefined) throw new ToolFailure(`Writing ${path} failed: ${s.error.trim()}`)
  seen.set(path, s.stamp ?? '')
}

function checkSeen(path: string, stamp: string): void {
  const was = seen.get(path)
  if (was === undefined) throw new ToolFailure('File has not been read yet. Read it first before writing to it.')
  if (was !== stamp)
    throw new ToolFailure('File has been modified since read, either by the user or by a linter. Read it again before attempting to write it.')
}

export async function write($: $, cfg: Config, e: BuiltinToolInputs['Write']): Promise<BuiltinToolResults['Write']> {
  const path = await absolute($, cfg, e.file_path)
  const before = await readWhole($, cfg, path)
  if (before.exists) checkSeen(path, before.stamp)
  await writeText($, cfg, path, e.content)
  return {
    type: before.exists ? 'update' : 'create',
    filePath: path,
    content: e.content,
    structuredPatch: before.exists ? makePatch(before.text, e.content) : [],
    originalFile: before.exists ? before.text : null,
  }
}

function count(haystack: string, needle: string): number {
  let n = 0
  for (let i = haystack.indexOf(needle); i !== -1; i = haystack.indexOf(needle, i + needle.length)) n++
  return n
}

export async function edit($: $, cfg: Config, e: BuiltinToolInputs['Edit']): Promise<BuiltinToolResults['Edit']> {
  const path = await absolute($, cfg, e.file_path)
  if (path.endsWith('.ipynb')) throw new ToolFailure('File is a Jupyter Notebook. Use the NotebookEdit to edit this file.')
  if (e.old_string === e.new_string)
    throw new ToolFailure('No changes to make: old_string and new_string are exactly the same.')
  const before = await readWhole($, cfg, path)
  let after: string
  if (!before.exists) {
    if (e.old_string !== '') throw new ToolFailure(`File does not exist: ${path}`)
    after = e.new_string
  } else {
    checkSeen(path, before.stamp)
    if (e.old_string === '') {
      if (before.text.trim() !== '') throw new ToolFailure('Cannot create new file - file already exists.')
      after = e.new_string
    } else {
      const n = count(before.text, e.old_string)
      if (n === 0) throw new ToolFailure(`String to replace not found in file.\nString: ${e.old_string}`)
      if (n > 1 && !e.replace_all)
        throw new ToolFailure(
          `Found ${n} matches of the string to replace, but replace_all is false. To replace all occurrences, set replace_all to true. To replace only one occurrence, please provide more context to uniquely identify the instance.\nString: ${e.old_string}`,
        )
      after = e.replace_all
        ? before.text.split(e.old_string).join(e.new_string)
        : (() => {
            const i = before.text.indexOf(e.old_string)
            return before.text.slice(0, i) + e.new_string + before.text.slice(i + e.old_string.length)
          })()
    }
  }
  await writeText($, cfg, path, after)
  return {
    filePath: path,
    oldString: e.old_string,
    newString: e.new_string,
    originalFile: before.exists ? before.text : null,
    structuredPatch: makePatch(before.text, after),
    userModified: false,
    replaceAll: !!e.replace_all,
  }
}

// ---------------------------------------------------------------- notebooks

const sourceText = (src: unknown): string => (Array.isArray(src) ? src.join('') : String(src ?? ''))

export async function notebookEdit($: $, cfg: Config, e: BuiltinToolInputs['NotebookEdit']): Promise<BuiltinToolResults['NotebookEdit']> {
  const path = await absolute($, cfg, e.notebook_path)
  if (!path.endsWith('.ipynb')) throw new ToolFailure('File must be a Jupyter notebook (.ipynb file). For editing other file types, use the Edit tool.')
  const before = await readWhole($, cfg, path)
  if (!before.exists) throw new ToolFailure(`Notebook file does not exist: ${path}`)
  checkSeen(path, before.stamp)
  let nb: any
  try {
    nb = JSON.parse(before.text)
  } catch {
    throw new ToolFailure('Notebook is not valid JSON.')
  }
  const cells: any[] = Array.isArray(nb.cells) ? nb.cells : (nb.cells = [])
  const mode = e.edit_mode ?? 'replace'
  let index = -1
  if (e.cell_id !== undefined) {
    index = cells.findIndex(c => c.id === e.cell_id)
    const m = /^cell-(\d+)$/.exec(e.cell_id)
    if (index === -1 && m) index = Number(m[1])
    if (index < 0 || index >= cells.length) throw new ToolFailure(`Cell with ID "${e.cell_id}" not found in notebook.`)
  } else if (mode !== 'insert') {
    throw new ToolFailure('Cell ID must be specified when not inserting a new cell.')
  }
  let oldSource: string | undefined
  let cellType: 'code' | 'markdown' = e.cell_type ?? 'code'
  let cellId = e.cell_id
  if (mode === 'insert') {
    if (!e.cell_type) throw new ToolFailure('Cell type is required when using edit_mode=insert.')
    const cell: any = { cell_type: e.cell_type, metadata: {}, source: e.new_source }
    if (nb.nbformat > 4 || (nb.nbformat === 4 && nb.nbformat_minor >= 5)) cell.id = cellId = randomHex(4)
    if (e.cell_type === 'code') Object.assign(cell, { execution_count: null, outputs: [] })
    cells.splice(index + 1, 0, cell)
  } else if (mode === 'delete') {
    oldSource = sourceText(cells[index].source)
    cellType = cells[index].cell_type
    cells.splice(index, 1)
  } else {
    const cell = cells[index]
    oldSource = sourceText(cell.source)
    cell.source = e.new_source
    if (e.cell_type && e.cell_type !== cell.cell_type) cell.cell_type = e.cell_type
    cellType = cell.cell_type
    if (cell.cell_type === 'code') Object.assign(cell, { execution_count: null, outputs: [] })
    else {
      delete cell.outputs
      delete cell.execution_count
    }
  }
  const updated = JSON.stringify(nb, null, 1) + '\n'
  await writeText($, cfg, path, updated)
  return {
    new_source: e.new_source,
    old_source: oldSource,
    cell_id: cellId,
    cell_type: cellType,
    language: nb.metadata?.language_info?.name ?? 'python',
    edit_mode: mode,
    notebook_path: path,
    original_file: before.text,
    updated_file: updated,
  }
}
