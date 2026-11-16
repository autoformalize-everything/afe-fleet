// Pure helpers: bytes and text, shell quoting, ustar archives, patches.

const encoder = new TextEncoder()
const decoder = new TextDecoder('utf-8')

/** A message for the model; thrown by tool code, returned as the call's error. */
export class ToolFailure extends Error {}

export const utf8Length = (s: string): number => encoder.encode(s).length

export const errorText = (err: unknown): string => (err instanceof Error ? err.message : String(err))

export const randomHex = (bytes = 8): string =>
  Array.from(crypto.getRandomValues(new Uint8Array(bytes)), b => b.toString(16).padStart(2, '0')).join('')

/** Keeps only base64 characters (drops line breaks and the tty's carriage returns). */
export const cleanBase64 = (s: string): string => s.replace(/[^A-Za-z0-9+/=]/g, '')

export function base64Bytes(b64: string): Uint8Array {
  const bin = atob(cleanBase64(b64))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

export const base64Text = (b64: string): string => decoder.decode(base64Bytes(b64))

/** A POSIX-shell single-quoted word. */
export const shellQuote = (s: string): string => `'${s.replace(/'/g, `'\\''`)}'`

/** Joins `p` to `cwd` when relative and resolves `.` and `..`. */
export function resolvePath(p: string, cwd: string): string {
  const abs = p.startsWith('/') ? p : `${cwd.replace(/\/+$/, '')}/${p}`
  const out: string[] = []
  for (const part of abs.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') out.pop()
    else out.push(part)
  }
  return '/' + out.join('/')
}

// ---------------------------------------------------------------- ustar

function octal(n: number, width: number): string {
  return n.toString(8).padStart(width - 1, '0') + '\0'
}

function field(s: string, width: number): string {
  if (s.length > width) throw new Error(`tar field too long: ${s}`)
  return s + '\0'.repeat(width - s.length)
}

export type TarEntry = { name: string; content?: string; dir?: boolean; mode: number; uid: number; gid: number }

function header(e: TarEntry, size: number, mtime: number): string {
  if (!/^[\x20-\x7e]+$/.test(e.name)) throw new Error(`tar entry names must be printable ASCII: ${e.name}`)
  const parts = [
    field(e.name, 100),
    octal(e.mode, 8),
    octal(e.uid, 8),
    octal(e.gid, 8),
    octal(size, 12),
    octal(mtime, 12),
    '        ', // checksum, counted as spaces
    e.dir ? '5' : '0',
    field('', 100),
    'ustar\0',
    '00',
    field('', 32),
    field('', 32),
    field('', 8),
    field('', 8),
    field('', 155),
    field('', 12),
  ]
  let h = parts.join('')
  let sum = 0
  for (let i = 0; i < h.length; i++) sum += h.charCodeAt(i)
  h = h.slice(0, 148) + sum.toString(8).padStart(6, '0') + '\0 ' + h.slice(156)
  return h
}

/**
 * A tar archive as a string: headers are ASCII and padding is NUL, so the
 * archive is valid UTF-8 whenever the contents are, and the HTTP body (a
 * string, sent as UTF-8) carries it byte for byte. Sizes count UTF-8 bytes.
 */
export function tarArchive(entries: TarEntry[]): string {
  const mtime = Math.floor(Date.now() / 1000)
  const out: string[] = []
  for (const e of entries) {
    const content = e.dir ? '' : (e.content ?? '')
    const size = utf8Length(content)
    out.push(header(e, size, mtime), content, '\0'.repeat((512 - (size % 512)) % 512))
  }
  out.push('\0'.repeat(1024))
  return out.join('')
}

// ---------------------------------------------------------------- patches

export type Hunk = { oldStart: number; oldLines: number; newStart: number; newLines: number; lines: string[] }

const toLines = (s: string): string[] => {
  const lines = s.split('\n')
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return s === '' ? [] : lines
}

/** One hunk spanning the changed region (common head and tail trimmed), with context. */
export function makePatch(oldText: string, newText: string, context = 3): Hunk[] {
  if (oldText === newText) return []
  const a = toLines(oldText)
  const b = toLines(newText)
  let head = 0
  while (head < a.length && head < b.length && a[head] === b[head]) head++
  let tail = 0
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++
  const from = Math.max(0, head - context)
  const aEnd = a.length - tail
  const bEnd = b.length - tail
  const after = Math.min(tail, context)
  const lines = [
    ...a.slice(from, head).map(l => ' ' + l),
    ...a.slice(head, aEnd).map(l => '-' + l),
    ...b.slice(head, bEnd).map(l => '+' + l),
    ...a.slice(aEnd, aEnd + after).map(l => ' ' + l),
  ]
  const oldLines = head - from + (aEnd - head) + after
  const newLines = head - from + (bEnd - head) + after
  return [{ oldStart: oldLines ? from + 1 : from, oldLines, newStart: newLines ? from + 1 : from, newLines, lines }]
}
