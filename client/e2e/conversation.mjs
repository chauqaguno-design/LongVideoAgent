import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { chromium } from 'playwright-core'

test('one conversation finds two quotes, clarifies the selection, previews and exports', { timeout: 180000 }, async t => {
  const base = process.env.CUTSIEVE_EDIT_UI || 'http://127.0.0.1:5175'
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname))
  const tmp = await mkdtemp(path.join(tmpdir(), 'cutsieve-conversation-'))
  t.after(() => rm(tmp, { recursive: true, force: true }))
  const movie = path.join(tmp, 'train.mp4'), subtitles = path.join(tmp, 'ticket.srt')
  await writeFile(subtitles, '1\n00:00:01,000 --> 00:00:02,000\nTicket\n\n2\n00:00:04,000 --> 00:00:05,000\nTicket\n')
  const ffmpeg = process.env.CUTSIEVE_FFMPEG || (process.env.CUTSIEVE_FFMPEG_DIR
    ? path.join(process.env.CUTSIEVE_FFMPEG_DIR, process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg') : 'ffmpeg')
  execFileSync(ffmpeg, ['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x96:r=10:d=6', '-i', subtitles,
    '-map', '0:v', '-map', '1:s', '-c:v', 'libx264', '-c:s', 'mov_text', movie])
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : { channel: process.platform === 'win32' ? 'msedge' : undefined })
  t.after(() => browser.close())
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } })
  page.setDefaultTimeout(30000)
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  const output = process.env.CUTSIEVE_EDIT_EVIDENCE
  if (output) await mkdir(output, { recursive: true })
  const button = name => page.getByRole('button', { name, exact: true })
  await page.goto(base)
  await button('+ 新建项目').click()
  await page.getByLabel('新项目名称').fill('车站对白')
  await button('创建项目').click()
  await page.getByLabel('添加视频').setInputFiles(movie)
  await page.locator('.asset-state').filter({ hasText: /就绪|部分完成/ }).waitFor({ timeout: 60000 })
  assert.equal(await page.locator('.conversation-turn').count(), 0)
  const say = async text => {
    await page.getByLabel('问题或剪辑要求', { exact: true }).fill(text)
    const response = page.waitForResponse(r => r.url().endsWith('/conversation-runs') && r.request().method() === 'POST')
    await button('发送').click()
    const res = await response
    assert.ok(res.ok(), String(res.status()))
    return (await res.json()).run_id
  }
  const found = await say('哪一段说了「Ticket」？')
  const first = page.locator('[data-run-id="' + found + '"]')
  const secondCitation = first.locator('.citation-chips button').filter({ hasText: /0:04/ }).first()
  await secondCitation.click()
  assert.ok(await first.locator('.tool-steps li').count() > 0, 'Agent tool steps should be recorded')
  await page.waitForFunction(() => {
    const v = document.querySelector('video[aria-label="监视器播放"]')
    return v && !v.seeking && v.readyState >= 2 && v.currentTime >= 3.95 && v.currentTime <= 5.05
  })
  const second = await say('用上一轮的两个台词片段，请先问我要剪哪一段，给出「第 1 段」和「第 2 段」选项。选好后生成预览。')
  await page.getByText('需要你确认', { exact: true }).waitFor()
  const option = page.locator('.conversation-clarification').getByRole('button', { name: /第\s*2\s*段/ })
  const [resumed] = await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/' + second + '/resume') && r.request().method() === 'POST'),
    option.click(),
  ])
  assert.ok(resumed.ok())
  assert.match(resumed.request().postDataJSON().message, /第\s*2\s*段/)
  const response = page.locator('[data-run-id="' + second + '"]')
  await response.getByRole('button', { name: '在监视器播放预览' }).click()
  await page.locator('.edit-clips li').first().waitFor()
  await page.waitForFunction(() => {
    const v = document.querySelector('video[aria-label="监视器播放"]')
    return v?.readyState >= 1 && Math.abs(v.duration - 1) < .15
  })
  assert.equal(await button('成片预览').getAttribute('aria-pressed'), 'true')
  const player = page.getByLabel('监视器播放')
  await player.evaluate(video => { video.muted = true; return video.play() })
  await page.waitForFunction(() => document.querySelector('video[aria-label="监视器播放"]').currentTime >= .3)
  await player.evaluate(video => video.pause())
  if (output) await response.screenshot({ path: path.join(output, 'conversation-preview.png') })
  const final = await say('把当前时间线上唯一片段的入点提前两秒，出点保持不变，然后导出 MP4。')
  const exported = page.locator('[data-run-id="' + final + '"]')
  const link = exported.getByRole('link', { name: '下载 MP4' })
  await link.waitFor()
  await page.locator('.edit-clips li').filter({ hasText: '0:02 – 0:05' }).waitFor()
  await exported.getByRole('button', { name: '查看时间线' }).click()
  await page.locator('.timeline-focused').waitFor()
  assert.equal(await page.locator('.conversation-log video').count(), 0)
  const url = page.url()
  const [download] = await Promise.all([page.waitForEvent('download'), link.click()])
  const bytes = await readFile(await download.path())
  assert.equal(bytes.subarray(4, 8).toString(), 'ftyp')
  const probe = process.env.CUTSIEVE_FFPROBE || (process.env.CUTSIEVE_FFMPEG_DIR
    ? path.join(process.env.CUTSIEVE_FFMPEG_DIR, process.platform === 'win32' ? 'ffprobe.exe' : 'ffprobe') : 'ffprobe')
  const duration = Number(execFileSync(probe, ['-v', 'error', '-show_entries', 'format=duration',
    '-of', 'csv=p=0', await download.path()], { encoding: 'utf8' }).trim())
  assert.ok(Math.abs(duration - 3) < .15)
  await page.reload()
  await link.waitFor()
  assert.deepEqual(await page.locator('.conversation-turn').evaluateAll(nodes => nodes.map(n => n.dataset.runId)), [found, second, final])
  assert.match(await page.locator('.clarified').innerText(), /第\s*2\s*段/)
  if (output) {
    await page.screenshot({ path: path.join(output, 'conversation-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.screenshot({ path: path.join(output, 'conversation-mobile.png'), fullPage: true })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
  }
  await button('新对话').click()
  assert.equal(await page.locator('.conversation-turn').count(), 0)
  assert.ok(!new URLSearchParams(new URL(page.url()).hash.split('?')[1]).has('run'))
  assert.deepEqual(errors, [])
  const receipt = { run_ids: [found, second, final], url, candidate_start_seconds: 4, preview_seconds: 1,
    export_seconds: duration, export_size: bytes.length, export_sha256: createHash('sha256').update(bytes).digest('hex'), browser_errors: errors }
  if (output) await writeFile(path.join(output, 'conversation.json'), JSON.stringify(receipt, null, 2))
  t.diagnostic(JSON.stringify(receipt))
})
