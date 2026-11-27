import { afterEach, expect, test, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import EditStudio from '../src/editing/EditStudio.vue'
import RenderJobs from '../src/editing/RenderJobs.vue'
import WorkflowTurn from '../src/editing/WorkflowTurn.vue'
import WorkflowPanel from '../src/editing/WorkflowPanel.vue'
import { clipDraft, makeCommand, uploadAsset } from '../src/editing/editApi.js'

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const page = items => ({ items, next_cursor: null })
const asset = { asset_id: 'a', filename: 'film.mp4', media_revision_id: 'm', playback_artifact_id: 'source', status: 'AVAILABLE', duration_us: 5_000_000 }
const edit = { edit_project_id: 'e', title: '剪辑', head_revision_id: 'r0' }
const zero = { edit_project_id: 'e', revision_id: 'r0', revision_no: 0, clips: [], total_duration_us: 0 }
const first = { ...zero, revision_id: 'r1', revision_no: 1, clips: [{ clip_id: 'c', ...clipDraft(asset, .25, 1.75, 'keep') }], total_duration_us: 1_500_000 }
const mounted = []
const render = (component, props) => {
  const wrapper = mount(component, { props })
  mounted.push(wrapper)
  return wrapper
}
const click = (wrapper, label) => wrapper.findAll('button').find(b => b.text() === label).trigger('click')
afterEach(() => { mounted.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); history.replaceState(null, '', '/') })

test('restores a deep-linked conversation and keeps earlier answers attached to their turns', async () => {
  history.replaceState(null, '', '#edit?project=p&view=workflow&run=old')
  const previous = { run_id: 'old', message: '第一个问题', media_revision_id: 'm', status: 'SUCCEEDED', answer: '旧任务的回答' }
  let acceptRun
  const pendingRun = new Promise(resolve => { acceptRun = resolve })
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const path = new URL(url, 'http://local').pathname
    if (path.endsWith('/projects/p/workflow-runs')) return json(page([{ run_id: 'old' }, { run_id: 'new' }]))
    if (path === '/v1/projects/p/conversation-runs' && options.method === 'POST') {
      expect(JSON.parse(options.body)).toMatchObject({ parent_run_id: 'old', media_revision_id: 'm', message: '新问题' })
      expect(JSON.parse(options.body)).not.toHaveProperty('intent')
      return pendingRun
    }
    if (path.endsWith('/conversation')) return json({ turns: path.includes('/old/') ? [previous] :
      [previous, { ...previous, run_id: 'new', parent_run_id: 'old', message: '新问题', answer: '新任务的回答' }] })
    if (path.endsWith('/events')) return new Response('')
    if (path.endsWith('/result')) return json({ answer: path.includes('/old/') ? '旧任务的回答' : '新任务的回答' })
    return json({ status: 'SUCCEEDED', result_artifact_id: 'answer' })
  }))
  const wrapper = render(WorkflowPanel, { projectId: 'p', assets: [asset] })
  await flushPromises()
  expect(wrapper.text()).toContain('旧任务的回答')
  await wrapper.find('textarea').setValue('新问题')
  await wrapper.find('form').trigger('submit')
  expect(wrapper.find('[data-run-id="old"]').text()).toContain('旧任务的回答')
  expect(wrapper.find('[data-run-id="sending"]').text()).not.toContain('旧任务的回答')
  acceptRun(json({ run_id: 'new' }))
  await flushPromises()
  expect(wrapper.find('[data-run-id="new"]').text()).toContain('新任务的回答')
  expect(wrapper.findAll('.conversation-turn')).toHaveLength(2)
  expect(location.hash).toContain('run=new')
})

test('shows the failed stage and unsettled costs while retrying', async () => {
  history.replaceState(null, '', '#edit?project=p&view=workflow&run=run')
  let resumed = null
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const path = new URL(url, 'http://local').pathname
    if (path.endsWith('/projects/p/workflow-runs')) return json(page([{ run_id: 'run' }]))
    if (path.endsWith('/conversation')) return json({ turns: [{ run_id: 'run', media_revision_id: 'm', message: '剪片', status: 'FAILED' }] })
    if (path.endsWith('/events')) return new Response('')
    if (path.endsWith('/resume')) { resumed = JSON.parse(options.body); return json({ run_id: 'run' }) }
    return json({ status: resumed ? 'SUCCEEDED' : 'FAILED', phase: 'PROPOSE', state_version: 9,
      error: resumed ? null : { message: '临时故障', retryable: true }, budget_usage: {
      model_calls: 1, tool_calls: 2, images: 0, committed_microunits: null, uncertain_microunits: 3500,
      currency: 'CNY', limits: {}, input_tokens: 0, output_tokens: 0,
    } })
  }))
  const wrapper = render(WorkflowPanel, { projectId: 'p', assets: [asset] })
  await flushPromises()
  expect(wrapper.text()).toContain('失败 · 拟定剪辑方案')
  expect(wrapper.text()).toContain('临时故障')
  expect(wrapper.text()).toContain('模型费用待核算')
  expect(wrapper.text()).toContain('0.0035 CNY 预占费用待核账')
  await click(wrapper, '重试本轮')
  await flushPromises()
  expect(resumed).toMatchObject({ expected_state_version: 9, idempotency_key: expect.any(String), message: '重试本轮任务' })
  expect(wrapper.text()).not.toContain('重试本轮')
})

test('a new chat never inherits an arbitrary run; changing video clears candidates and the parent id', async () => {
  history.replaceState(null, '', '#edit?project=p')
  const sends = []
  const second = { ...asset, asset_id: 'b', media_revision_id: 'n', filename: 'other.mp4' }
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const path = new URL(url, 'http://local').pathname
    if (path.endsWith('/projects/p/workflow-runs')) return json(page([{ run_id: 'unrelated', status: 'SUCCEEDED' }]))
    if (path.endsWith('/conversation-runs')) {
      sends.push(JSON.parse(options.body))
      return json({ run_id: 'turn' + sends.length })
    }
    if (path.endsWith('/conversation')) {
      const sent = sends.at(-1)
      return json({ turns: [{ run_id: 'turn' + sends.length, message: sent.message, media_revision_id: sent.media_revision_id,
        route: { plan: ['SEARCH', 'ANSWER'], reason: '查找台词' }, status: 'SUCCEEDED',
        candidates: [{ text: '第一处台词', support: 'UNCERTAIN', range: { start_us: 1000000, end_us: 1300000 }, media_revision_id: sent.media_revision_id }] }] })
    }
    return json({ status: 'SUCCEEDED', phase: 'REVIEW' })
  }))
  const wrapper = render(WorkflowPanel, { projectId: 'p', assets: [asset, second] })
  await flushPromises()
  expect(fetch.mock.calls.some(([u]) => u.includes('/workflow-runs/unrelated'))).toBe(false)
  const choose = wrapper.find('.composer-controls select')
  await choose.setValue('m')
  await wrapper.find('textarea').setValue('找一句话')
  await wrapper.find('.conversation-composer').trigger('submit')
  await flushPromises()
  expect(wrapper.get('.conversation-plan').text()).toContain('查找片段')
  expect(wrapper.get('.turn-candidates').text()).toContain('候选 1')
  expect(wrapper.get('.turn-candidates').text()).toContain('待确认')
  await choose.setValue('n')
  expect(wrapper.find('.conversation-turn').exists()).toBe(false)
  await wrapper.find('textarea').setValue('剪前三秒')
  await wrapper.find('.conversation-composer').trigger('submit')
  await flushPromises()
  expect(sends.map(({ media_revision_id }) => media_revision_id)).toEqual(['m', 'n'])
  expect(sends.every(body => !('parent_run_id' in body) && !('edit_project_id' in body))).toBe(true)
})

test('clarification goes to the waiting turn and its exchange survives reload', async () => {
  history.replaceState(null, '', '#edit?project=p&view=workflow&run=q')
  let reply = ''
  const question = '你想查找片段还是剪辑？'
  vi.stubGlobal('fetch', vi.fn(async (url, opt) => {
    const path = new URL(url, 'http://local').pathname
    if (path.endsWith('/resume')) {
      const body = JSON.parse(opt.body)
      expect(body.expected_state_version).toBe(7); reply = body.message
      return json({ run_id: 'q' })
    }
    if (path.endsWith('/conversation')) return json({ turns: [{ run_id: 'q', message: '处理一下', media_revision_id: 'm',
      status: reply ? 'SUCCEEDED' : 'AWAITING_INPUT', clarifications: reply ? [{ question, reply }] : [] }] })
    return json(reply ? { status: 'SUCCEEDED', phase: 'REVIEW' } :
      { status: 'AWAITING_INPUT', phase: 'PLAN', state_version: 7, awaiting_input: { question, options: ['查找片段'] } })
  }))
  const wrapper = render(WorkflowPanel, { projectId: 'p', assets: [asset] })
  await flushPromises()
  await click(wrapper, '查找片段')
  await wrapper.find('.conversation-composer').trigger('submit')
  await flushPromises()
  expect(reply).toBe('查找片段')
  expect(wrapper.get('.clarified').text()).toContain(question + reply)
  expect(fetch.mock.calls.some(([u]) => u.endsWith('/conversation-runs'))).toBe(false)
})

test('displays a worker or database outage when readiness returns 503', async () => {
  const worker = { ready: false, queues: { outbox: {
    online: false, counts: { PENDING: 3, DEAD: 1 }, failed: 1, failure_rate: .25,
  } }, uncertain_model_requests: 0 }
  let status = worker
  vi.stubGlobal('fetch', vi.fn(async url => new URL(url, 'http://local').pathname === '/v1/readiness'
    ? json(status, 503) : json(page([]))))
  const wrapper = render(EditStudio, { library: [] })
  await flushPromises()
  await click(wrapper, '更新服务状态')
  await flushPromises()
  expect(wrapper.text()).toContain('事件投递 · 离线 · 排队 3 · 累计失败 1（25.0%）')
  status = { ready: false, dependency: 'database' }
  await click(wrapper, '更新服务状态')
  await flushPromises()
  expect(wrapper.text()).toContain('数据库暂时不可用')
  expect(wrapper.text()).not.toContain('排队 3')
})

test('restores the project, appends source microseconds and pins renders to a revision', async () => {
  const historyItems = [zero]
  const operations = [], renders = []
  history.replaceState(null, '', '#edit?project=p&edit=e')
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const path = new URL(url, 'http://local').pathname
    if (path.endsWith('/operations')) {
      operations.push(JSON.parse(options.body))
      historyItems.push(first)
      return json({ revision_id: 'r1', revision_no: 1 })
    }
    if (path === '/v1/render-jobs') {
      renders.push(JSON.parse(options.body))
      return json({ render_job_id: 'j' }, 202)
    }
    const responses = {
      '/v1/projects': page([{ project_id: 'other', title: '其他' }, { project_id: 'p', title: '我的项目' }]),
      '/v1/projects/p/assets': page([asset]), '/v1/projects/p/edit-projects': page([edit]),
      '/v1/edit-projects/e': { ...edit, head_revision_id: historyItems.at(-1).revision_id },
      '/v1/edit-projects/e/revisions': page(historyItems), '/v1/artifacts/source': { url: '/signed-film' },
      '/v1/edit-projects/e/render-jobs': page([{ render_job_id: 'old', revision_id: 'r0', profile: 'PREVIEW',
        status: 'SUCCEEDED', artifact_id: 'old-artifact' }]),
    }
    if (!(path in responses)) throw new Error('Unexpected ' + path)
    return json(responses[path])
  }))
  const wrapper = render(EditStudio, { library: [] })
  await flushPromises()
  expect(wrapper.find('video').attributes('src')).toBe('/signed-film')
  const [start, end] = wrapper.findAll('input[type=number]')
  await start.setValue('0.25')
  await end.setValue('1.75')
  await start.element.form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await flushPromises()
  expect(operations[0]).toMatchObject({ expected_revision: 0, operations: [{ op: 'append_clip', clip: {
    media_revision_id: 'm', source_start_us: 250000, source_end_us: 1750000, audio_policy: 'keep',
  } }] })
  expect(wrapper.find('.edit-clips').text()).toContain('0.25 – 1.75 秒')
  expect(wrapper.find('.edit-job-list').exists()).toBe(false)
  await click(wrapper, '导出视频')
  await flushPromises()
  expect(renders[0]).toMatchObject({ revision_id: 'r1', profile: 'EXPORT' })
  expect(window.location.hash).toBe('#edit?project=p&edit=e')
})

test('old revision results never replace the displayed revision', async () => {
  const jobs = [{ render_job_id: 'old', revision_id: 'r0', status: 'SUCCEEDED', profile: 'EXPORT', artifact_id: 'old-file' },
    { render_job_id: 'new', revision_id: 'r1', status: 'SUCCEEDED', profile: 'EXPORT', artifact_id: 'new-file' }]
  vi.stubGlobal('fetch', vi.fn(async url => json(url.includes('/render-jobs') ? page(jobs) : {
    url: '/signed-new-file', mime_type: 'video/mp4', timeline_revision_id: 'r1', kind: 'EXPORT' })))
  const wrapper = render(RenderJobs, { revision: first })
  await flushPromises()
  expect(wrapper.findAll('.edit-job-list li')).toHaveLength(1)
  await click(wrapper, '打开成片')
  await flushPromises()
  expect(wrapper.find('video').attributes('src')).toBe('/signed-new-file')
  expect(wrapper.find('a[download]').attributes('href')).toBe('/signed-new-file')
  expect(wrapper.find('a[download]').attributes('download')).toBe('cutsieve-r1.mp4')
  expect(fetch.mock.calls.some(([url]) => url.includes('old-file'))).toBe(false)
})

test.each([['webm', 'WebM', 'video/webm'], ['mkv', 'MKV', 'video/x-matroska']])(
  'exports the selected %s format and restores its download in a conversation', async (extension, label, mime) => {
    const job = { render_job_id: 'job', revision_id: 'r1', status: 'SUCCEEDED', profile: 'EXPORT', artifact_id: 'file' }
    const result = { artifact_id: 'file', kind: 'EXPORT', timeline_revision_id: 'r1', mime_type: mime, url: '/download' }
    const requests = []
    vi.stubGlobal('fetch', vi.fn(async (url, options) => {
      if (options.method === 'POST') {
        requests.push({ url, body: JSON.parse(options.body) })
        return json(job, 202)
      }
      if (url.includes('/render-jobs')) return json(page(requests.length ? [job] : []))
      if (url.includes('/revisions/')) return json(first)
      return json(result)
    }))
    const timeline = render(RenderJobs, { revision: first })
    await flushPromises()
    await timeline.get('select').setValue(extension)
    await click(timeline, '导出视频')
    await flushPromises()
    expect(requests[0].url).toMatch(new RegExp('/render-jobs\\?format=' + extension + '$'))
    expect(requests[0].body).toMatchObject({ revision_id: 'r1', profile: 'EXPORT', idempotency_key: expect.any(String) })
    await click(timeline, '打开成片')
    await flushPromises()
    expect(timeline.get('a[download]').text()).toBe('下载 ' + label)
    expect(timeline.get('a[download]').attributes('download')).toBe('cutsieve-r1.' + extension)
    expect(timeline.find('video').exists()).toBe(extension === 'webm')
    await click(timeline, '生成预览')
    await flushPromises()
    expect(requests[1].url).toMatch(/\/render-jobs$/)
    expect(requests[1].body.profile).toBe('PREVIEW')

    const chat = render(WorkflowTurn, { turn: { run_id: 'turn' }, result: { export_artifact_id: 'file',
      edit_project_id: 'e', revision_id: 'r1' }, active: true })
    await flushPromises()
    expect(chat.get('a[download]').text()).toBe('下载本次导出的 ' + label)
    expect(chat.get('a[download]').attributes('download')).toBe('cutsieve-r1.' + extension)
  })

test('upload sends bytes only to its signed URL, then completes with the returned ETag', async () => {
  const signed = 'https://storage.test/one-shot?signature=test'
  localStorage.setItem('authToken', 'session')
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url === signed) return new Response(null, { headers: { ETag: '"digest"' } })
    if (url.endsWith('/assets')) return json({ upload_mode: 'SINGLE_PUT', upload_id: 'up', parts: [{ part_number: 1, url: signed, headers: { 'Content-Type': 'video/mp4' } }] })
    return json(asset)
  }))
  const file = new File(['data'], 'test.mp4', { type: 'video/mp4' })
  await uploadAsset('p', file)
  const [create, put, complete] = fetch.mock.calls
  expect(JSON.parse(create[1].body)).toMatchObject({ filename: 'test.mp4', size_bytes: 4 })
  expect(put[1]).toMatchObject({ method: 'PUT', body: file, credentials: 'omit', headers: { 'Content-Type': 'video/mp4' } })
  expect(new Headers(put[1].headers).has('Authorization')).toBe(false)
  expect(JSON.parse(complete[1].body).parts).toEqual([{ part_number: 1, etag: 'digest' }])
  localStorage.clear()
})

test('shows the repaired revision and separates preview observations from export checks', async () => {
  const range = (start_us, end_us) => ({ start_us, end_us })
  const before = { media_revision_id: 'm', range: range(2_000_000, 3_000_000) }
  const after = { media_revision_id: 'm', range: range(4_000_000, 5_000_000) }
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url.endsWith('/harness')) return json({
      plan_generation: 1, index_generation_id: 'frozen', reference_count: 6, repair_count: 1, repair_limit: 2,
    })
    if (url.includes('/revisions/')) return json({ ...first, revision_no: 3 })
    return json({ url: '/signed-mkv', kind: 'EXPORT', timeline_revision_id: 'r1', mime_type: 'video/x-matroska' })
  }))
  const wrapper = render(WorkflowTurn, {
    turn: { run_id: 'repair', repair_count: 1 }, assets: [asset], active: true,
    result: {
      edit_project_id: 'e', revision_id: 'r1', export_artifact_id: 'final',
      timeline_change: { position: 2, before, after, preserved_count: 1 },
      shot_plan: { shots: [{ ...after, candidate_id: 'blue', reason: '核实到蓝色画面' }] },
      review_report: {
        output_kind: 'EXPORT', structural_checks: [{ status: 'PASSED' }], semantic_checks: [{ status: 'NOT_CHECKED' }],
        coverage: { checked_times_us: [] }, issues: [],
      },
      preview_review: { semantic_checks: [{ status: 'PASSED' }], coverage: { checked_times_us: [5_000_000] } },
    },
  })
  await flushPromises()
  expect(wrapper.text()).toContain('版本 3')
  expect(wrapper.text()).toContain('局部修复 1 次')
  expect(wrapper.text()).toContain('保留其余 1 段')
  expect(wrapper.get('[aria-label="镜头方案"]').text()).toContain('核实到蓝色画面')
  expect(wrapper.get('[aria-label="审核覆盖"]').text()).toContain('语义 未检查')
  expect(wrapper.get('[aria-label="审核覆盖"]').text()).toContain('预览语义 通过')
  expect(wrapper.get('a[download]').attributes('href')).toBe('/signed-mkv')
  const details = wrapper.get('details')
  details.element.open = true
  await details.trigger('toggle')
  await flushPromises()
  expect(details.text()).toContain('frozen')
})

test('a retried edit reuses the command key until its reply arrives', async () => {
  const send = makeCommand()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(json({ ok: true })))
  const body = { expected_revision: 4, operations: [] }
  await expect(send('/edits', body)).rejects.toThrow()
  await send('/edits', body)
  expect(JSON.parse(fetch.mock.calls[0][1].body).idempotency_key).toBe(JSON.parse(fetch.mock.calls[1][1].body).idempotency_key)
})

test('range rejects an empty end, an inverted range and a source overrun', () => {
  for (const [start, end] of [[0, ''], [2, 1], [0, 5.000001]]) {
    expect(() => clipDraft(asset, start, end, 'keep')).toThrow('区间')
  }
})
