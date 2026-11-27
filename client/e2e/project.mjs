import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'
import { chromium } from 'playwright-core'

// The selected local fixture must contain the supplied dialogue. Uses its own test account.
test('upload, read notes, seek by dialogue, search by screenshot and ask with evidence', { timeout: 240000 }, async t => {
  const base = process.env.CUTSIEVE_EDIT_UI || 'http://127.0.0.1:5175'
  const file = process.env.CUTSIEVE_EDIT_SAMPLE, quote = process.env.CUTSIEVE_TEST_QUOTE
  assert.ok(file && quote)
  assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname))
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : { channel: process.platform === 'win32' ? 'msedge' : undefined })
  t.after(() => browser.close())
  const viewport = { width: 1360, height: 900 }
  const demo = process.env.CUTSIEVE_DEMO_VIDEO
  const page = await browser.newPage({ viewport, recordVideo: demo ? { dir: path.dirname(demo), size: viewport } : undefined })
  const user = { username: 'journey_' + randomUUID().slice(0, 8), password: randomUUID() }
  const registered = await page.request.post(base + '/user/register', { data: { ...user, nickname: '本地验收' } })
  assert.ok(registered.ok())
  const button = name => page.getByRole('button', { name, exact: true })
  const evidenceResponse = id => page.waitForResponse(response => {
    const path = new URL(response.url()).pathname
    return (id ? path === '/v1/evidence/' + id : path.startsWith('/v1/evidence/')) && response.ok()
  }).then(response => response.json())
  async function firstHit() {
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/v1/search' && response.ok()),
      button('搜索片段').click(),
    ])
    return (await response.json()).hits[0]
  }
  const playback = {}
  async function checkPlayback(name, evidence) {
    const target = evidence.range.start_us / 1e6
    await page.waitForFunction(seconds => {
      const video = document.querySelector('video[aria-label="证据原片回看"]')
      return video && !video.error && !video.seeking && video.readyState >= 2 && Math.abs(video.currentTime - seconds) < .1
    }, target)
    playback[name] = { expected_seconds: target, actual_seconds: await page.getByLabel('证据原片回看').evaluate(video => video.currentTime) }
  }
  await page.goto(base + '/#edit')
  await button('登录工作台').click()
  await page.getByLabel('用户名', { exact: true }).fill(user.username)
  await page.getByLabel('密码', { exact: true }).fill(user.password)
  await button('立即登录').click()
  await page.getByLabel('新项目名称').fill('证据检索验收')
  await button('创建项目').click()
  await button('去添加视频').click()
  await page.getByLabel('添加视频').setInputFiles(file)
  await page.getByText('正在处理项目…').waitFor({ state: 'hidden' })
  await button('素材处理').click()
  await button('打开观影笔记').waitFor({ timeout: 120000 })
  await button('打开观影笔记').click()
  await page.getByText(quote, { exact: false }).filter({ visible: true }).first().waitFor()
  await button('检索片段').click()
  await page.getByLabel('台词、动作或场景').fill(quote)
  const dialogueHit = await firstHit()
  const hit = page.locator('.research-results .run-list > li').first()
  const [dialogue] = await Promise.all([evidenceResponse(dialogueHit.evidence_ids[0]), hit.getByRole('button', { name: '查看依据' }).click()])
  await hit.getByText(quote, { exact: false }).first().waitFor()
  await hit.getByRole('button', { name: /回看/ }).click()
  await checkPlayback('dialogue', dialogue)
  await hit.locator('.hit-thumb img').waitFor()
  await page.getByLabel('搜索方式').selectOption('COUNT')
  await page.getByLabel('按台词原文核查').check()
  await button('搜索片段').click()
  await button('按本次查询核查原片').click()
  await button('查看全部匹配清单').waitFor({ timeout: 45000 })
  await button('查看全部匹配清单').click()
  await page.locator('.workflow-output ol li').first().waitFor()
  await button('检索片段').click()
  if (process.env.CUTSIEVE_QUERY_IMAGE) {
    await page.getByLabel('台词、动作或场景').fill('')
    await page.getByLabel('查询截图').setInputFiles(process.env.CUTSIEVE_QUERY_IMAGE)
    const imageHit = await firstHit()
    await page.getByText(/image_spatial|vision_dense/).first().waitFor()
    const [image] = await Promise.all([evidenceResponse(imageHit.evidence_ids[0]), hit.getByRole('button', { name: /回看/ }).click()])
    await checkPlayback('screenshot', image)
  }
  await button('对话与任务').click()
  await button('新对话').click()
  await page.getByLabel('问题或剪辑要求').fill('哪一段说了「' + quote + '」？')
  await button('发送').click()
  await page.getByRole('button', { name: /回看依据/ }).first().waitFor({ timeout: 60000 })
  const [claim] = await Promise.all([evidenceResponse(), page.getByRole('button', { name: /回看依据/ }).first().click()])
  await checkPlayback('answer', claim)
  const questionUrl = page.url()
  await button('检索片段').click()
  await button('对话与任务').click()
  await page.locator('.workflow-output').waitFor()
  assert.equal(page.url(), questionUrl)
  await page.reload()
  await page.locator('.workflow-output').waitFor()
  if (process.env.CUTSIEVE_EDIT_EVIDENCE) {
    await mkdir(process.env.CUTSIEVE_EDIT_EVIDENCE, { recursive: true })
    await page.screenshot({ path: path.join(process.env.CUTSIEVE_EDIT_EVIDENCE, 'grounded-question.png'), fullPage: true })
  }
  const editInstruction = process.env.CUTSIEVE_TEST_EDIT || '剪出第 1 到 3 秒并导出'
  await page.getByLabel('问题或剪辑要求').fill(editInstruction)
  const exportMetadata = page.waitForResponse(async response =>
    new URL(response.url()).pathname.startsWith('/v1/artifacts/') && response.ok() && (await response.json()).kind === 'EXPORT',
    { timeout: 60000 })
  await button('发送').click()
  const direct = page.getByRole('link', { name: '下载本次导出的 MP4' })
  await direct.waitFor({ timeout: 60000 })
  const [fileDownload] = await Promise.all([page.waitForEvent('download'), direct.click()])
  assert.match(fileDownload.suggestedFilename(), /\.mp4$/)
  assert.equal(await fileDownload.failure(), null)
  const fileMeta = await (await exportMetadata).json()
  const downloaded = await readFile(await fileDownload.path())
  const sha256 = createHash('sha256').update(downloaded).digest('hex')
  assert.equal(downloaded.subarray(4, 8).toString(), 'ftyp')
  assert.equal(downloaded.length, fileMeta.size_bytes)
  assert.equal(sha256, fileMeta.sha256)
  const receipt = { playback, download: { size_bytes: downloaded.length, sha256, revision_id: fileMeta.timeline_revision_id } }
  if (process.env.CUTSIEVE_TEST_CONFLICT) {
    await page.getByLabel('问题或剪辑要求').fill(process.env.CUTSIEVE_TEST_CONFLICT)
    await button('发送').click()
    const clarification = page.getByText(/候选无法拼出目标.*秒/)
    await clarification.waitFor()
    receipt.edit_instruction = editInstruction
    receipt.conflict = await clarification.innerText()
  }
  t.diagnostic(JSON.stringify(receipt))
  if (process.env.CUTSIEVE_EDIT_EVIDENCE) await writeFile(path.join(process.env.CUTSIEVE_EDIT_EVIDENCE, 'project-journey.json'), JSON.stringify(receipt, null, 2))
  if (demo) {
    const recording = page.video()
    await page.close()
    await recording.saveAs(demo)
    await recording.delete()
  }
})
