import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'
import { chromium } from 'playwright-core'

test('uploads media, reads notes, follows citations, attaches an image and opens readiness', { timeout: 240000 }, async t => {
  const base = process.env.CUTSIEVE_EDIT_UI || 'http://127.0.0.1:5175'
  const sample = process.env.CUTSIEVE_EDIT_SAMPLE
  const quote = process.env.CUTSIEVE_TEST_QUOTE
  const image = process.env.CUTSIEVE_QUERY_IMAGE
  const output = process.env.CUTSIEVE_EDIT_EVIDENCE
  assert.ok(sample && quote, 'Set CUTSIEVE_EDIT_SAMPLE and CUTSIEVE_TEST_QUOTE.')
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname))
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : { channel: process.platform === 'win32' ? 'msedge' : undefined })
  t.after(() => browser.close())
  const viewport = { width: 1360, height: 900 }
  const demo = process.env.CUTSIEVE_DEMO_VIDEO
  const page = await browser.newPage({ viewport, recordVideo: demo ? { dir: path.dirname(demo), size: viewport } : undefined })
  const button = name => page.getByRole('button', { name, exact: true })
  const player = page.getByLabel('监视器播放')
  const playback = {}

  async function checkPlayback(name, citation) {
    const clock = await citation.locator('.mono').innerText()
    const times = clock.split(' – ').map(time => {
      const [minutes, seconds] = time.split(':').map(Number)
      return minutes * 60 + seconds
    })
    await citation.click()
    await page.waitForFunction(({ clock, from, to }) => {
      const video = document.querySelector('video[aria-label="监视器播放"]')
      const monitorTime = document.querySelector('[aria-label="监视器时间码"]')
      return video?.readyState >= 2 && monitorTime?.textContent.trim() === clock &&
        video.currentTime >= from - .05 && video.currentTime <= to + .5
    }, { clock, from: times[0], to: times[1] ?? times[0] })
    playback[name] = { clock, actual_seconds: await player.evaluate(video => video.currentTime) }
  }

  async function send(text) {
    await page.getByLabel('问题或剪辑要求').fill(text)
    const [response] = await Promise.all([
      page.waitForResponse(res => res.url().endsWith('/conversation-runs') && res.request().method() === 'POST'),
      button('发送').click(),
    ])
    assert.ok(response.ok(), 'Conversation request returned HTTP ' + response.status())
    const { run_id } = await response.json()
    return { id: run_id, request: response.request().postDataJSON(), turn: page.locator('[data-run-id="' + run_id + '"]') }
  }

  await page.goto(base + '/#edit')
  await button('+ 新建项目').click()
  await page.getByLabel('新项目名称').fill('片段检索验收')
  await button('创建项目').click()
  await page.getByLabel('添加视频').setInputFiles(sample)
  await button('素材与笔记').click()
  const drawer = page.getByRole('dialog', { name: '素材与笔记' })
  await drawer.getByRole('button', { name: '打开观影笔记' }).waitFor({ timeout: 120000 })
  await drawer.getByRole('button', { name: '打开观影笔记' }).click()
  const note = drawer.locator('.notes-body li').filter({ hasText: quote }).filter({ visible: true }).first()
  const noteTimecode = note.locator('button.mono')
  await checkPlayback('notes', noteTimecode)
  await drawer.waitFor({ state: 'hidden' })

  const question = await send('哪一段说了「' + quote + '」？请给出原片时间码。')
  assert.equal(question.request.media_revision_id, null)
  const citation = question.turn.locator('.citation-chips button').first()
  await citation.waitFor({ timeout: 60000 })
  await checkPlayback('dialogue', citation)
  const questionUrl = page.url()
  await page.reload()
  await citation.waitFor({ timeout: 60000 })
  assert.equal(page.url(), questionUrl)
  assert.equal(await page.locator('.conversation-log video').count(), 0)

  let imageRun = null
  if (image) {
    await button('新对话').click()
    await page.getByLabel('附带截图').setInputFiles(image)
    await page.locator('.attachment-chip img').waitFor()
    imageRun = await send('查找与附带截图最接近的画面，给出原片时间码。')
    assert.ok(imageRun.request.image_artifact_id)
    await imageRun.turn.locator('.conversation-user img').waitFor()
    const imageCitation = imageRun.turn.locator('.citation-chips button').first()
    await imageCitation.waitFor({ timeout: 60000 })
    await checkPlayback('screenshot', imageCitation)
  }

  const instruction = process.env.CUTSIEVE_TEST_EDIT || '剪出原片第 1 到 3 秒并导出 MP4。'
  const exported = await send(instruction)
  const direct = exported.turn.getByRole('link', { name: '下载 MP4' })
  await direct.waitFor({ timeout: 60000 })
  await page.locator('.edit-clips li').first().waitFor()
  const [download] = await Promise.all([page.waitForEvent('download'), direct.click()])
  assert.match(download.suggestedFilename(), /^cutsieve-.*\.mp4$/)
  assert.equal(await download.failure(), null)
  const bytes = await readFile(await download.path())
  assert.equal(bytes.subarray(4, 8).toString(), 'ftyp')

  await button('系统状态').click()
  const status = page.getByRole('dialog', { name: '系统状态' })
  await status.locator('.queue-list li').first().waitFor()
  await status.locator('.usage-metrics').waitFor()
  assert.match(await status.innerText(), /Tokens/)
  const receipt = {
    playback, question_run: question.id, screenshot_run: imageRun?.id, edit_run: exported.id,
    export_size: bytes.length, export_sha256: createHash('sha256').update(bytes).digest('hex'),
  }
  if (output) {
    await mkdir(output, { recursive: true })
    await page.screenshot({ path: path.join(output, 'system-status.png'), fullPage: true })
    await writeFile(path.join(output, 'project-journey.json'), JSON.stringify(receipt, null, 2))
  }
  if (demo) {
    const video = page.video()
    await page.close()
    await video.saveAs(demo)
  }
  t.diagnostic(JSON.stringify(receipt))
})
