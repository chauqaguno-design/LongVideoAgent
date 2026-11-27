import assert from 'node:assert/strict'
import test from 'node:test'
import { access, mkdir, open } from 'node:fs/promises'
import path from 'node:path'
import { chromium } from 'playwright-core'

// Run against an isolated local workspace. Each run creates a test project.
test('upload, trim two ranges, preview, reorder and export a pinned version', { timeout: 180000 }, async t => {
  const file = process.env.CUTSIEVE_EDIT_SAMPLE
  assert.ok(file, 'Set CUTSIEVE_EDIT_SAMPLE to a video at least four seconds long.')
  await access(file)
  const base = process.env.CUTSIEVE_EDIT_UI || 'http://127.0.0.1:5175'
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use an isolated local application.')
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : { channel: process.platform === 'win32' ? 'msedge' : undefined })
  t.after(() => browser.close())
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } })
  page.setDefaultTimeout(15000)
  const button = name => page.getByRole('button', { name, exact: true })
  const waitVersion = version => page.waitForFunction(label =>
    Array.from(document.querySelectorAll('select')).some(s => s.selectedOptions[0]?.textContent.trim() === label), version)
  const verifyVideo = async (label, duration) => {
    const video = page.getByLabel(label, { exact: true })
    await video.waitFor()
    await page.waitForFunction(({ label, duration }) => {
      const node = Array.from(document.querySelectorAll('video')).find(v => v.getAttribute('aria-label') === label)
      return node && node.readyState >= 1 && Math.abs(node.duration - duration) < .2
    }, { label, duration })
    await video.evaluate(node => { node.muted = true; return node.play() })
    await page.waitForFunction(label => Array.from(document.querySelectorAll('video')).some(
      node => node.getAttribute('aria-label') === label && node.currentTime > .1), label)
    await video.evaluate(node => node.pause())
  }
  const completed = profile => page.getByText(profile + ' · 已完成', { exact: true }).waitFor({ timeout: 60000 })
  const output = process.env.CUTSIEVE_EDIT_EVIDENCE
  let stage = '打开本地工作区'
  try {
    await page.goto(base)
    await page.getByText('本地工作区', { exact: true }).waitFor()
    stage = '创建项目并上传'
    await page.getByLabel('新项目名称').fill('样片验收')
    await button('创建项目').click()
    await button('素材处理').click()
    const complete = page.waitForResponse(r => r.url().includes('/complete') && r.request().method() === 'POST')
    await page.getByLabel('添加视频').setInputFiles(file)
    const uploaded = await complete
    assert.ok(uploaded.ok(), 'Upload completion HTTP ' + uploaded.status())
    await button('时间线').click()
    await page.getByLabel('新方案名称').fill('两段精剪')
    await button('新建剪辑').click()
    const addRange = async (from, to, count) => {
      await page.getByLabel('入点（秒）').fill(String(from))
      await page.getByLabel('出点（秒）').fill(String(to))
      await button('添加到时间线').click()
      await page.locator('.edit-clips li').nth(count - 1).waitFor()
    }
    stage = '选择时间段'
    await addRange(.25, 1.75, 1)
    await addRange(2, 4, 2)
    stage = '预览'
    await button('生成预览').click()
    await completed('预览')
    await button('打开成片').click()
    await verifyVideo('剪辑成片', 3.5)
    await button('查看源片对应').click()
    const firstSource = page.locator('.edit-results ol button').first()
    const time = (await firstSource.innerText()).match(/原片 (\d+):([\d.]+)/)
    assert.ok(time, 'Compiled source start must be displayed')
    await firstSource.click()
    await page.waitForFunction(expected => {
      const video = document.querySelector('video[aria-label="证据原片回看"]')
      return video?.readyState >= 1 && Math.abs(video.currentTime - expected) <= .035
    }, Number(time[1]) * 60 + Number(time[2]))
    stage = '调整顺序'
    await page.locator('.edit-clips li').nth(1).getByRole('button', { name: '上移' }).click()
    await waitVersion('V3（当前）')
    assert.match(await page.locator('.edit-clips li').first().innerText(), /2 – 4 秒/)
    stage = '刷新后导出'
    await page.reload()
    await waitVersion('V3（当前）')
    assert.equal(await page.locator('.edit-job-list li').count(), 0)
    await button('导出视频').click()
    await completed('导出')
    await button('打开成片').click()
    await verifyVideo('剪辑成片', 3.5)
    const link = page.getByRole('link', { name: '下载 MP4' })
    const [download] = await Promise.all([page.waitForEvent('download'), link.click()])
    const mp4 = await open(await download.path(), 'r')
    try {
      const { buffer } = await mp4.read(Buffer.alloc(8), 0, 8, 0)
      assert.equal(buffer.subarray(4).toString(), 'ftyp')
    } finally { await mp4.close() }
    if (output) {
      await mkdir(output, { recursive: true })
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
      await page.screenshot({ path: path.join(output, 'edit-export.png'), fullPage: true })
      await page.setViewportSize({ width: 390, height: 844 })
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Mobile horizontal overflow')
      await page.screenshot({ path: path.join(output, 'edit-mobile.png'), fullPage: true })
    }
    stage = '替换、移除并恢复旧版'
    await page.locator('.edit-clips li').first().getByRole('button', { name: '替换区间' }).click()
    await page.getByLabel('出点（秒）').fill('3.5')
    await button('保存替换').click()
    await waitVersion('V4（当前）')
    await page.locator('.edit-clips li').first().getByRole('button', { name: '移除' }).click()
    await waitVersion('V5（当前）')
    assert.equal(await page.locator('.edit-clips li').count(), 1)
    await page.getByLabel('时间线版本').selectOption({ label: 'V2' })
    await completed('预览')
    await button('恢复为新版本').click()
    await waitVersion('V6（当前）')
    assert.match(await page.locator('.edit-clips li').first().innerText(), /0.25 – 1.75 秒/)
    t.diagnostic('Real preview and export played; output duration 3.5 s. Page ' + page.url())
  } catch (cause) {
    const errors = await page.getByRole('alert').allTextContents()
    throw new Error(stage + (errors.length ? ' · ' + errors.join(' / ') : ''), { cause })
  }
})
