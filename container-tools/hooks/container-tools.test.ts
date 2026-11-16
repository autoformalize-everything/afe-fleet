import { expect, test } from 'claude-code/testing'
import { makePatch, resolvePath, shellQuote, tarArchive } from './util'

test('paths resolve against the shell directory', async () => {
  expect(resolvePath('a/../b/./c', '/work')).toBe('/work/b/c')
  expect(resolvePath('/etc//x', '/work')).toBe('/etc/x')
  expect(resolvePath('../..', '/a')).toBe('/')
})

test('shell quoting survives single quotes', async () => {
  expect(shellQuote("it's")).toBe(`'it'\\''s'`)
})

test('a patch spans the changed lines with context', async () => {
  const hunks = makePatch('1\n2\n3\n4\n5\n6\n7\n8\n', '1\n2\n3\n4\nFIVE\n6\n7\n8\n')
  expect(hunks).toEqual([
    { oldStart: 2, oldLines: 7, newStart: 2, newLines: 7, lines: [' 2', ' 3', ' 4', '-5', '+FIVE', ' 6', ' 7', ' 8'] },
  ])
  expect(makePatch('same', 'same')).toEqual([])
})

test('tar entries are 512-byte blocks with a valid checksum and UTF-8 sizes', async () => {
  const content = 'héllo 🚀\n' // 12 bytes in UTF-8
  const tar = tarArchive([{ name: 'tmp/x', content, mode: 0o600, uid: 1000, gid: 1000 }])
  const header = tar.slice(0, 512)
  expect(header.slice(124, 135)).toBe((12).toString(8).padStart(11, '0'))
  let sum = 0
  for (let i = 0; i < 512; i++) sum += i >= 148 && i < 156 ? 32 : header.charCodeAt(i)
  expect(parseInt(header.slice(148, 154), 8)).toBe(sum)
  // header + content padded to one block (in bytes) + two zero blocks
  expect(new TextEncoder().encode(tar).length).toBe(512 + 512 + 1024)
})

test('a missing container with no image refuses the call instead of running it', async ($, on) => {
  const urls: string[] = []
  on('env.get', () => ({ value: undefined }))
  on('tool.check', () => ({ decision: 'allow' }))
  on('http.fetch', (_, e) => {
    urls.push(e.url)
    return { value: { status: 404, ok: false, headers: {}, text: '{"message":"No such container: claude-sandbox"}' } }
  })
  on('tool.call', () => ({ result: { stdout: 'RAN ON THE HOST', stderr: '', interrupted: false } }))
  const r = await $.tool.call({ tool: 'Bash', command: 'echo hi' })
  expect(r.deny).toContain('does not exist and no image is configured')
  expect(urls[0]).toContain('/containers/claude-sandbox/json')
})

test('a tool outside the set is refused', async ($, on) => {
  on('env.get', () => ({ value: undefined }))
  on('tool.call', () => ({ result: 'RAN ON THE HOST' }))
  const r = await $.tool.call({ tool: 'WebFetch', url: 'https://example.com', prompt: 'x' })
  expect(r.deny).toContain('not available here')
})
