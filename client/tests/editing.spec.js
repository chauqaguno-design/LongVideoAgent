import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import App from '../src/App.vue'
import AgentTurn from '../src/editing/AgentTurn.vue'
import RenderJobs from '../src/editing/RenderJobs.vue'
import { clipDraft, makeCommand, uploadAsset } from '../src/editing/editApi.js'

const json = (body, status = 200) => Response.json(body, { status })
const page = (items = []) => ({ items, next_cursor: null })
const project = { project_id: 'p', title: '夜班样片' }
const asset = { asset_id: 'a', filename: 'station.mp4', media_revision_id: 'm', playback_artifact_id: 'source', status: 'AVAILABLE', duration_us: 15_000_000 }
const assetB = { ...asset, asset_id: 'b', filename: 'station-b.mp4', media_revision_id: 'm2', playback_artifact_id: 'source-b' }
const edit = { edit_project_id: 'e', title: '粗剪', head_revision_id: 'r1' }
const zero = { edit_project_id: 'e', revision_id: 'r0', revision_no: 0, clips: [], total_duration_us: 0, caption_policy: 'none' }
const clip = { clip_id: 'c1', ...clipDraft(asset, .25, 1.75, 'keep') }
const first = { ...zero, revision_id: 'r1', revision_no: 1, clips: [clip], total_duration_us: 1_500_000 }
const usage = { model_calls: 2, tool_calls: 3, images: 4, input_tokens: 1024, output_tokens: 128, estimated_cost_cny: .048 }
const savedCitation = { media_revision_id: 'm2', asset_id: 'b', start_us: 8_500_000, end_us: 11_500_000, label: '关键台词', text: '车钥匙刚才还在你口袋里', source: 'asr' }
const mounts = []

function render(component = App, props = {}) {
  const wrapper = mount(component, { attachTo: document.body, props })
  mounts.push(wrapper)
  return wrapper
}

function button(wrapper, label) {
  const target = wrapper.findAll('button').find(item => item.text() === label)
  expect(target, 'button ' + label).toBeDefined()
  return target
}

const click = (wrapper, label) => button(wrapper, label).trigger('click')
const version = wrapper => wrapper.get('[aria-label="时间线版本"]').element.selectedOptions[0].textContent.trim()
const monitor = wrapper => wrapper.get('video[aria-label="监视器播放"]')
const textarea = wrapper => wrapper.get('#conversation-message')
const requestBody = (mock, path) => JSON.parse(mock.mock.calls.find(([url, options]) => url === path && options?.body)[1].body)
const renderFile = (kind, id, revisionId = 'r1', mime = 'video/mp4') => ({
  kind, artifact_id: id, timeline_revision_id: revisionId, mime_type: mime, url: '/' + id + '.mp4',
})

function server(override = () => undefined) {
  const fetch = vi.fn(async (url, options = {}) => {
    const { pathname, searchParams } = new URL(url, location.origin)
    const custom = override(pathname, options, searchParams, url)
    if (custom !== undefined) return custom
    if (pathname === '/v1/projects') return json(page([project]))
    if (pathname === '/v1/projects/p/assets') return json(page([asset]))
    if (pathname === '/v1/projects/p/edit-projects') return json(page([edit]))
    if (pathname === '/v1/projects/p/workflow-runs') return json(page())
    if (pathname === '/v1/edit-projects/e') return json(edit)
    if (pathname === '/v1/edit-projects/e/revisions') return json(page([first, zero]))
    if (pathname.endsWith('/processing-runs') || pathname.endsWith('/render-jobs')) return json(page())
    if (pathname === '/v1/artifacts/source') return json({ artifact_id: 'source', url: '/station.mp4' })
    if (pathname === '/v1/artifacts/source-b') return json({ artifact_id: 'source-b', url: '/station-b.mp4' })
    throw new Error('Unexpected API request ' + pathname)
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

function channel(runId = 'run') {
  let controller
  let sequence = 0
  const body = new ReadableStream({ start(stream) { controller = stream } })
  return {
    response: () => new Response(body, { headers: { 'content-type': 'text/event-stream' } }),
    push(type, payload, number = ++sequence) {
      const event = { run_id: runId, sequence: number, event_id: 'event-' + number, type, payload }
      controller.enqueue(new TextEncoder().encode('id: ' + event.event_id + '\ndata: ' + JSON.stringify(event) + '\n\n'))
    },
  }
}

function setFiles(input, files) {
  Object.defineProperty(input.element, 'files', { configurable: true, value: files })
  return input.trigger('change')
}

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(1)
})

afterEach(() => {
  mounts.splice(0).forEach(wrapper => wrapper.unmount())
  vi.unstubAllGlobals()
  vi.useRealTimers()
  history.replaceState(null, '', '/')
  localStorage.clear()
})

test('renders assistant Markdown and displays raw HTML as inert text', () => {
  const answer = [
    '## Result',
    '**Bold** and `a < b`',
    'Next line',
    '',
    '- **`clip`**',
    '- Second clip',
    '1. First step',
    '2. Second step',
    '',
    '| Time | Evidence |',
    '| --- | --- |',
    '| `00:12` | **Blue** |',
    '',
    '<script>alert(1)</script>',
    '<img src=x onerror="alert(2)">',
    '&lt;img src=x onerror=alert(3)&gt;',
  ].join('\n')
  const wrapper = render(AgentTurn, { turn: { run_id: 'sending', message: '**User message**' }, result: { answer } })
  const result = wrapper.get('.turn-answer')
  expect(result.get('h2').text()).toBe('Result')
  expect(result.get('p strong').text()).toBe('Bold')
  expect(result.get('p code').text()).toBe('a < b')
  expect(result.find('p br').exists()).toBe(true)
  expect(result.get('ul li strong code').text()).toBe('clip')
  expect(result.findAll('ol li').map(item => item.text())).toEqual(['First step', 'Second step'])
  expect(result.findAll('th').map(cell => cell.text())).toEqual(['Time', 'Evidence'])
  expect(result.get('tbody td strong').text()).toBe('Blue')
  expect(result.find('script, img, [onerror]').exists()).toBe(false)
  expect(result.text()).toContain('<script>alert(1)</script>')
  expect(result.text()).toContain('<img src=x onerror="alert(2)">')
  expect(result.text()).toContain('&lt;img src=x onerror=alert(3)&gt;')
  expect(wrapper.get('.conversation-user p').text()).toBe('**User message**')
})

test('shows the three panes together, project controls and the live asset strip', async () => {
  const rows = [asset, assetB, { ...asset, asset_id: 'c', filename: 'rejected.mp4', status: 'FAILED' }]
  let renamed = ''
  const fetch = server((path, options) => {
    if (path === '/v1/projects/p/assets') return json(page(rows))
    if (path === '/v1/assets/b/processing-runs') return json(page([{ status: 'PARTIAL' }]))
    if (path === '/v1/projects/p' && options.method === 'PATCH') {
      renamed = JSON.parse(options.body).title
      return json({ ...project, title: renamed })
    }
  })
  const wrapper = render()
  await flushPromises()
  expect(wrapper.get('.studio-bar').text()).toContain('CutSieve剪筛工作台')
  expect(wrapper.findAll('.studio-grid .studio-pane').map(node => node.get('h2').text())).toEqual(['监视器', '时间线', '智能剪辑助手'])
  expect(wrapper.find('.project-tabs').exists()).toBe(false)
  expect(wrapper.findAll('.asset-state').map(node => node.text())).toEqual(['就绪', '部分完成', '失败'])
  expect(wrapper.get('[aria-label="素材项目"]').element.value).toBe('p')
  expect(version(wrapper)).toBe('V1（当前）')
  expect(wrapper.findAll('.prompt-hint')).toHaveLength(3)
  await wrapper.get('.prompt-hint').trigger('click')
  expect(textarea(wrapper).element.value).toContain('关键台词')
  await wrapper.findAll('.asset-chip')[1].trigger('click')
  await flushPromises()
  expect(monitor(wrapper).attributes('src')).toBe('/station-b.mp4')
  expect(wrapper.findAll('video')).toHaveLength(1)

  await click(wrapper, '重命名项目')
  await wrapper.get('.studio-popover input').setValue('夜景改版')
  await wrapper.get('.studio-popover').trigger('submit')
  await flushPromises()
  expect(renamed).toBe('夜景改版')
  expect(fetch).toHaveBeenCalledWith('/v1/projects/p', expect.objectContaining({ method: 'PATCH' }))
  expect(wrapper.get('[aria-label="素材项目"]').text()).toContain('夜景改版')
})

test('selects the newest scheme by creation time and keeps legacy deep links to a different scheme', async () => {
  const old = { ...edit, edit_project_id: 'old', title: '较旧', created_at: '2026-03-01T00:00Z' }
  const recent = { ...edit, edit_project_id: 'recent', title: '最新' }
  server((path, _options, query) => {
    if (path.endsWith('/edit-projects')) return json(page([old, recent]))
    if (path === '/v1/edit-projects/recent/revisions' && query.get('cursor') === '1') {
      return json(page([{ ...zero, created_at: '2026-04-01T00:00Z' }]))
    }
    if (path.endsWith('/revisions')) return json(page([first]))
    if (path.endsWith('/recent')) return json(recent)
    if (path.endsWith('/old')) return json(old)
  })
  const wrapper = render()
  await flushPromises()
  expect(wrapper.get('[aria-label="剪辑方案"]').element.value).toBe('recent')
  history.replaceState(null, '', '#edit?project=p&edit=old&view=edit')
  window.dispatchEvent(new HashChangeEvent('hashchange'))
  await flushPromises()
  expect(wrapper.get('[aria-label="剪辑方案"]').element.value).toBe('old')
  expect(location.hash).not.toContain('view=')
})

test('a citation switches from a preview to its source asset, plays the exact interval, and uses the only video', async () => {
  history.replaceState(null, '', '#edit?project=p&run=old')
  const turn = { run_id: 'old', message: '哪一段提到了车钥匙？', status: 'SUCCEEDED', citations: [savedCitation],
    answer: '找到相关台词。', edit_project_id: 'e', revision_id: 'r1', preview_artifact_id: 'preview' }
  server(path => {
    if (path === '/v1/projects/p/assets') return json(page([asset, assetB]))
    if (path === '/v1/workflow-runs/old') return json({ ...turn, result_artifact_id: 'result', usage })
    if (path.endsWith('/old/conversation')) return json({ turns: [turn] })
    if (path.endsWith('/old/result')) return json(turn)
    if (path === '/v1/artifacts/preview') return json(renderFile('PREVIEW', 'preview'))
  })
  const wrapper = render()
  await flushPromises()
  await click(wrapper, '在监视器播放预览')
  await flushPromises()
  expect(button(wrapper, '成片预览').attributes('aria-pressed')).toBe('true')
  expect(monitor(wrapper).attributes('src')).toBe('/preview.mp4')
  await monitor(wrapper).trigger('loadedmetadata')
  await wrapper.get('.citation-chips button').trigger('click')
  await flushPromises()
  expect(button(wrapper, '原片').attributes('aria-pressed')).toBe('true')
  expect(monitor(wrapper).attributes('src')).toBe('/station-b.mp4')
  await monitor(wrapper).trigger('loadedmetadata')
  expect(monitor(wrapper).element.currentTime).toBe(8.5)
  expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2)
  expect(wrapper.get('output[aria-label="监视器时间码"]').text()).toBe('0:08.5 – 0:11.5')
  monitor(wrapper).element.currentTime = 11.7
  await monitor(wrapper).trigger('timeupdate')
  expect(monitor(wrapper).element.currentTime).toBe(11.5)
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled()
  expect(wrapper.findAll('video')).toHaveLength(1)
  expect(wrapper.find('.conversation-log video').exists()).toBe(false)
})

test('SSE tools display live, and timeline and artifact events refresh the left pane before the run ends', async () => {
  const events = channel()
  const second = {
    ...first, revision_id: 'r2', revision_no: 2, total_duration_us: 4_500_000,
    clips: [...first.clips, { ...clip, clip_id: 'c2', source_start_us: 8_500_000, source_end_us: 11_500_000 }],
  }
  let revisions = [first, zero]
  let state = { run_id: 'run', status: 'RUNNING', state_version: 1 }
  let result = null
  const fetch = server((path, options) => {
    if (path.endsWith('/conversation-runs')) return json({ run_id: 'run' }, 202)
    if (path === '/v1/workflow-runs/run') return json(state)
    if (path.endsWith('/run/conversation')) return json({ turns: [{ run_id: 'run', message: '更新粗剪', status: state.status }] })
    if (path.endsWith('/run/result')) return json(result)
    if (path.endsWith('/run/events')) return events.response()
    if (path.endsWith('/e/revisions')) return json(page(revisions))
    if (path.endsWith('/artifacts/live-preview')) return json(renderFile('PREVIEW', 'live-preview', 'r2'))
    if (path.endsWith('/artifacts/live-export')) return json(renderFile('EXPORT', 'live-export', 'r2'))
  })
  const wrapper = render()
  await flushPromises()
  await textarea(wrapper).setValue('更新粗剪')
  await textarea(wrapper).trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  expect(requestBody(fetch, '/v1/projects/p/conversation-runs')).toMatchObject({
    message: '更新粗剪', media_revision_id: null, edit_project_id: 'e', image_artifact_id: null, parent_run_id: null,
    idempotency_key: expect.any(String),
  })
  events.push('TOOL_STARTED', { tool_call_id: 'tool', name: 'edit_timeline', summary: 'Running edit_timeline' })
  await flushPromises()
  expect(wrapper.get('.tool-steps').attributes()).toHaveProperty('open')
  expect(wrapper.get('.tool-steps li').attributes('data-status')).toBe('RUNNING')
  expect(wrapper.get('.tool-steps li').text()).toContain('正在保存时间线版本')

  revisions = [second, first, zero]
  events.push('TOOL_FINISHED', { tool_call_id: 'tool', name: 'edit_timeline', summary: '已保存两段剪辑', status: 'SUCCEEDED', ok: true })
  events.push('TIMELINE_UPDATED', { edit_project_id: 'e', revision_id: 'r2', revision_no: 2 })
  await flushPromises()
  expect(wrapper.get('.tool-steps li').attributes('data-status')).toBe('SUCCEEDED')
  expect(wrapper.get('.tool-steps li').text()).toContain('已保存两段剪辑')
  expect(version(wrapper)).toBe('V2（当前）')
  expect(wrapper.findAll('.edit-clips li')).toHaveLength(2)
  expect(wrapper.findAll('.track-clip').map(node => node.element.style.flexGrow)).toEqual(['1500000', '3000000'])
  await wrapper.findAll('.track-clip')[1].trigger('click')
  expect(wrapper.get('output').text()).toBe('0:08.5 – 0:11.5')
  expect(monitor(wrapper).element.currentTime).toBe(8.5)

  events.push('ARTIFACT_READY', { kind: 'PREVIEW', artifact_id: 'live-preview', edit_project_id: 'e', revision_id: 'r2' })
  events.push('ARTIFACT_READY', { kind: 'EXPORT', artifact_id: 'live-export', edit_project_id: 'e', revision_id: 'r2' })
  await flushPromises()
  expect(wrapper.get('.render-bar a[download]').attributes('download')).toBe('cutsieve-r2.mp4')
  await click(wrapper, '播放预览')
  await flushPromises()
  expect(monitor(wrapper).attributes('src')).toBe('/live-preview.mp4')
  expect(button(wrapper, '成片预览').attributes('aria-pressed')).toBe('true')
  expect(button(wrapper, '取消').exists()).toBe(true)

  state = { ...state, status: 'SUCCEEDED', result_artifact_id: 'result', usage }
  result = { answer: '已保存并导出。', edit_project_id: 'e', revision_id: 'r2', preview_artifact_id: 'live-preview', export_artifact_id: 'live-export' }
  events.push('RUN_COMPLETED', {})
  await flushPromises()
  expect(wrapper.get('.turn-answer').text()).toBe('已保存并导出。')
  expect(wrapper.get('.turn-usage .usage-metrics').text()).toContain('¥0.048')
  expect(wrapper.findAll('.tool-steps li')).toHaveLength(1)
})

test('a fast finished run selects the returned scheme even without receiving SSE', async () => {
  const result = { answer: '完成。', edit_project_id: 'agent-edit', revision_id: 'agent-revision', export_artifact_id: 'archive' }
  const nextEdit = { ...edit, edit_project_id: 'agent-edit', title: '助手方案' }
  const nextRevision = { ...first, edit_project_id: 'agent-edit', revision_id: 'agent-revision' }
  const fetch = server(path => {
    if (path.endsWith('/conversation-runs')) return json({ run_id: 'instant' }, 202)
    if (path.endsWith('/instant')) return json({ run_id: 'instant', status: 'SUCCEEDED', result_artifact_id: 'result' })
    if (path.endsWith('/instant/conversation')) return json({ turns: [{ run_id: 'instant', message: '导出', status: 'SUCCEEDED' }] })
    if (path.endsWith('/instant/result')) return json(result)
    if (path.endsWith('/agent-edit')) return json(nextEdit)
    if (path.endsWith('/agent-edit/revisions')) return json(page([nextRevision]))
    if (path.endsWith('/archive')) return json(renderFile('EXPORT', 'archive', 'agent-revision', 'video/x-matroska'))
  })
  const wrapper = render()
  await flushPromises()
  await textarea(wrapper).setValue('导出')
  await click(wrapper, '发送')
  await flushPromises()
  expect(wrapper.get('[aria-label="剪辑方案"]').element.value).toBe('agent-edit')
  expect(wrapper.get('.conversation-response a[download]').text()).toBe('下载 MKV')
  expect(wrapper.get('.render-bar a[download]').attributes('download')).toBe('cutsieve-agent-revision.mkv')
  expect(fetch.mock.calls.some(([url]) => url.endsWith('/instant/events'))).toBe(false)
})

test('one click on ask_user submits the versioned resume and reconnects with the last SSE event ID', async () => {
  const initial = channel()
  const resumed = channel()
  let state = { run_id: 'run', status: 'RUNNING', state_version: 4 }
  let reply
  let stop
  const fetch = server((path, options) => {
    if (path.endsWith('/conversation-runs')) return json(state, 202)
    if (path.endsWith('/run')) return json(state)
    if (path.endsWith('/run/conversation')) return json({ turns: [{ run_id: 'run', message: '剪出这两段', status: state.status,
      tool_steps: [{ tool_call_id: 'ask', name: 'ask_user', summary: 'Running ask_user', status: 'RUNNING' }],
      clarifications: reply ? [{ question: '你想保留哪一段？', reply: reply.message }] : [],
    }] })
    if (path.endsWith('/run/events')) return (reply ? resumed : initial).response()
    if (path.endsWith('/run/resume')) {
      reply = JSON.parse(options.body)
      state = { ...state, status: 'RUNNING', awaiting_input: null }
      return json(state, 202)
    }
    if (path.endsWith('/run/cancel')) {
      stop = JSON.parse(options.body)
      state = { ...state, status: 'CANCELLED' }
      return json(state, 202)
    }
  })
  const wrapper = render()
  await flushPromises()
  await textarea(wrapper).setValue('剪出这两段')
  await click(wrapper, '发送')
  await flushPromises()
  state = { ...state, status: 'AWAITING_INPUT', state_version: 5, awaiting_input: { question: '你想保留哪一段？', options: ['第一段', '第二段'] } }
  initial.push('NEEDS_INPUT', state.awaiting_input)
  await flushPromises()
  expect(wrapper.get('.conversation-clarification').text()).toContain('你想保留哪一段？')
  expect(wrapper.get('.tool-steps li').attributes('data-status')).toBe('WAITING')
  await click(wrapper, '第二段')
  await flushPromises()
  expect(reply).toMatchObject({ expected_state_version: 5, message: '第二段', idempotency_key: expect.any(String) })
  expect(fetch).toHaveBeenCalledWith('/v1/workflow-runs/run/events', expect.objectContaining({ headers: expect.objectContaining({ 'Last-Event-ID': 'event-1' }) }))
  expect(wrapper.get('.clarified').text()).toContain('第二段')
  expect(wrapper.find('.conversation-clarification').exists()).toBe(false)
  expect(fetch.mock.calls.filter(([url]) => url.endsWith('/conversation-runs'))).toHaveLength(1)

  await click(wrapper, '取消')
  await flushPromises()
  expect(stop).toMatchObject({ reason: '用户取消任务', idempotency_key: expect.any(String) })
  expect(wrapper.get('.response-header').text()).toContain('已取消')
})

test('posts a screenshot as multipart, removes or attaches it, then preserves its thumbnail in the user turn', async () => {
  let chat
  const fetch = server((path, options) => {
    if (path.endsWith('/query-images')) return json({ artifact_id: 'query-img', url: '/query-img.png' }, 201)
    if (path.endsWith('/conversation-runs')) {
      chat = JSON.parse(options.body)
      return json({ run_id: 'image-run' }, 202)
    }
    if (path.endsWith('/image-run')) return json({ run_id: 'image-run', status: 'SUCCEEDED', result_artifact_id: 'result' })
    if (path.endsWith('/image-run/conversation')) {
      return json({ turns: [{ ...chat, run_id: 'image-run', status: 'SUCCEEDED', answer: '看到了相同的画面。' }] })
    }
    if (path.endsWith('/image-run/result')) return json({ answer: '看到了相同的画面。', citations: [] })
  })
  const wrapper = render()
  await flushPromises()
  const input = wrapper.get('input[aria-label="附带截图"]')
  const image = new File(['image'], 'frame.png', { type: 'image/png' })
  await setFiles(input, [image])
  await flushPromises()
  expect(wrapper.get('.attachment-chip img').attributes('src')).toBe('/query-img.png')
  expect(wrapper.get('.attachment-chip').text()).toContain('frame.png')
  await wrapper.get('[aria-label="移除截图"]').trigger('click')
  expect(wrapper.find('.attachment-chip').exists()).toBe(false)
  await setFiles(input, [image])
  await flushPromises()
  await wrapper.get('[aria-label="对话素材范围"]').setValue('m')
  await textarea(wrapper).setValue('定位截图中的镜头')
  await click(wrapper, '发送')
  await flushPromises()
  const upload = fetch.mock.calls.find(([url]) => url.endsWith('/query-images'))[1]
  expect(upload.method).toBe('POST')
  expect(upload.body.get('file').name).toBe('frame.png')
  expect(chat).toMatchObject({ media_revision_id: 'm', edit_project_id: 'e', image_artifact_id: 'query-img' })
  expect(wrapper.get('.conversation-user img').attributes('src')).toBe('/query-img.png')
  expect(wrapper.get('.conversation-user').text()).toContain('定位截图中的镜头')
  expect(wrapper.find('.attachment-chip').exists()).toBe(false)
})

test('reopens a conversation, focuses its historical revision, and restores that revision as a new version', async () => {
  history.replaceState(null, '', '#edit?project=p&run=old')
  let historyRows = [first, zero]
  const older = { run_id: 'question', message: '第一轮问题', status: 'SUCCEEDED', answer: '第一轮回答' }
  const turn = { run_id: 'old', parent_run_id: 'question', message: '第二轮问题', status: 'SUCCEEDED', edit_project_id: 'e', revision_id: 'r0' }
  const fetch = server((path, options) => {
    if (path === '/v1/projects/p/workflow-runs') return json(page([turn, older]))
    if (path.endsWith('/old')) return json({ ...turn, result_artifact_id: 'result', usage })
    if (path.endsWith('/old/conversation')) return json({ turns: [older, turn] })
    if (path.endsWith('/old/result')) return json({ ...turn, answer: '第二轮回答' })
    if (path.endsWith('/e/revisions')) return json(page(historyRows))
    if (path.endsWith('/e/operations')) {
      historyRows = [{ ...zero, revision_id: 'r2', revision_no: 2 }, ...historyRows]
      return json({ revision_no: 2 }, 201)
    }
  })
  const wrapper = render()
  await flushPromises()
  expect(wrapper.findAll('.turn-answer').map(node => node.text())).toEqual(['第一轮回答', '第二轮回答'])
  expect(version(wrapper)).toBe('V1（当前）')
  await click(wrapper, '查看时间线')
  await flushPromises()
  expect(version(wrapper)).toBe('V0')
  expect(wrapper.get('.timeline-pane').classes()).toContain('timeline-focused')
  expect(button(wrapper, '加入时间线').attributes()).toHaveProperty('disabled')
  await click(wrapper, '恢复为新版本')
  await flushPromises()
  expect(version(wrapper)).toBe('V2（当前）')
  expect(requestBody(fetch, '/v1/edit-projects/e/operations')).toMatchObject({
    expected_revision: 1, operations: [{ op: 'restore_revision', source_revision_id: 'r0' }],
  })
  await click(wrapper, '新对话')
  expect(wrapper.findAll('.conversation-turn')).toHaveLength(0)
  expect(location.hash).not.toContain('run=')
})

test('manual marks auto-create a scheme, poll a revision-pinned render, play it and expose the MP4 download', async () => {
  let scheme
  let revisions = [zero]
  let job
  let finished = false
  const fetch = server((path, options) => {
    if (path.endsWith('/p/edit-projects')) return json(page(scheme ? [scheme] : []))
    if (path === '/v1/edit-projects' && options.method === 'POST') {
      scheme = { ...edit, head_revision_id: 'r0' }
      return json(scheme, 201)
    }
    if (path.endsWith('/e/revisions')) return json(page(revisions))
    if (path.endsWith('/e/operations')) {
      const body = JSON.parse(options.body)
      revisions = [{ ...first, clips: [{ clip_id: 'c1', ...body.operations[0].clip }] }, zero]
      return json({ revision_id: 'r1' }, 201)
    }
    if (path === '/v1/render-jobs' && options.method === 'POST') {
      job = { render_job_id: 'export', revision_id: 'r1', status: 'RUNNING', profile: 'EXPORT', progress: .4 }
      return json(job, 202)
    }
    if (path.endsWith('/e/render-jobs')) {
      return json(page(job ? [finished ? { ...job, status: 'SUCCEEDED', artifact_id: 'final' } : job] : []))
    }
    if (path.endsWith('/artifacts/final')) return json(renderFile('EXPORT', 'final'))
  })
  const wrapper = render()
  await flushPromises()
  await monitor(wrapper).trigger('loadedmetadata')
  monitor(wrapper).element.currentTime = .25
  await wrapper.get('[aria-label="取当前入点"]').trigger('click')
  monitor(wrapper).element.currentTime = 1.75
  await wrapper.get('[aria-label="取当前出点"]').trigger('click')
  await wrapper.get('.trim-audio select').setValue('mute')
  await wrapper.get('.quick-trim').trigger('submit')
  await flushPromises()
  expect(requestBody(fetch, '/v1/edit-projects')).toMatchObject({ project_id: 'p', title: '剪辑方案 1' })
  expect(requestBody(fetch, '/v1/edit-projects/e/operations')).toMatchObject({
    expected_revision: 0, operations: [{ op: 'append_clip', clip: { ...clipDraft(asset, .25, 1.75, 'mute') } }],
  })
  expect(version(wrapper)).toBe('V1（当前）')
  expect(wrapper.get('.edit-clips').text()).toContain('0:00.25 – 0:01.75')

  await click(wrapper, '导出视频')
  await flushPromises()
  expect(requestBody(fetch, '/v1/render-jobs')).toMatchObject({ revision_id: 'r1', profile: 'EXPORT' })
  expect(wrapper.get('.edit-job-list').text()).toContain('40%')
  finished = true
  await vi.waitFor(() => expect(wrapper.find('.render-bar a[download]').exists()).toBe(true), { timeout: 3500, interval: 50 })
  await click(wrapper, '播放成片')
  await flushPromises()
  expect(button(wrapper, '成片预览').attributes('aria-pressed')).toBe('true')
  expect(monitor(wrapper).attributes('src')).toBe('/final.mp4')
  const failedVideo = monitor(wrapper).element
  Object.defineProperty(failedVideo, 'error', { value: { code: 4 } })
  await monitor(wrapper).trigger('error')
  await click(wrapper, '播放成片')
  await flushPromises()
  expect(monitor(wrapper).element).not.toBe(failedVideo)
  const download = wrapper.get('.render-bar a[download]')
  expect(download.text()).toBe('下载 MP4')
  expect(download.attributes()).toMatchObject({ href: '/final.mp4', download: 'cutsieve-r1.mp4' })
})

test.each([['webm', 'WebM', 'video/webm'], ['mkv', 'MKV', 'video/x-matroska']])(
  'exports %s for the selected revision with the proper download extension',
  async (extension, name, mime) => {
    let queued
    const fetch = server((path, options, query) => {
      if (path === '/v1/render-jobs' && options.method === 'POST') {
        expect(query.get('format')).toBe(extension)
        queued = { render_job_id: 'job', revision_id: 'r1', profile: 'EXPORT', status: 'SUCCEEDED', artifact_id: 'out' }
        return json(queued, 202)
      }
      if (path.endsWith('/e/render-jobs')) return json(page(queued ? [queued] : []))
      if (path.endsWith('/artifacts/out')) return json(renderFile('EXPORT', 'out', 'r1', mime))
    })
    const wrapper = render(RenderJobs, { revision: first })
    await wrapper.get('[aria-label="导出格式"]').setValue(extension)
    await click(wrapper, '导出视频')
    await flushPromises()
    expect(requestBody(fetch, '/v1/render-jobs?format=' + extension)).toMatchObject({ revision_id: 'r1', profile: 'EXPORT' })
    expect(wrapper.get('a[download]').text()).toBe('下载 ' + name)
    expect(wrapper.get('a[download]').attributes('download')).toBe('cutsieve-r1.' + extension)
  },
)

test('readiness drawer shows unavailable workers and the latest run telemetry, then refreshes', async () => {
  const earlier = { run_id: 'earlier', created_at: '2026-11-01T00:00Z', status: 'FAILED', usage: { model_calls: 99 } }
  const newest = { run_id: 'latest', created_at: '2026-11-02T00:00Z', status: 'SUCCEEDED', usage }
  let database = false
  const fetch = server(path => {
    if (path.endsWith('/p/workflow-runs')) return json(page([earlier, newest]))
    if (path === '/v1/readiness') {
      return json(database ? { ready: false, dependency: 'database' } : { ready: false,
        queues: { render: { online: false, counts: { QUEUED: 2 }, failed: 3 }, workflow: { online: true, counts: {}, failed: 0 } },
      }, 503)
    }
  })
  const wrapper = render()
  await flushPromises()
  expect(fetch.mock.calls.some(([url]) => url === '/v1/readiness')).toBe(false)
  await click(wrapper, '系统状态')
  await flushPromises()
  const panel = wrapper.get('dialog[open]')
  expect(panel.text()).toContain('视频渲染离线')
  expect(panel.text()).toContain('排队 2')
  expect(panel.text()).toContain('最近一轮用量')
  expect(panel.get('.usage-metrics').text()).toContain('1,024 / 128')
  expect(panel.get('.usage-metrics').text()).toContain('¥0.048')
  database = true
  await click(panel, '更新服务状态')
  await flushPromises()
  expect(panel.text()).toContain('数据库暂时不可用')
  await click(panel, '关闭')
  expect(wrapper.find('dialog[open]').exists()).toBe(false)
})

test('the notes drawer shows capability and observation details and sends timestamps to the monitor', async () => {
  const range = { start_us: 2_000_000, end_us: 3_500_000 }
  server(path => {
    if (path.endsWith('/assets/a')) return json({ ...asset, capabilities: [{ name: 'asr', state: 'AVAILABLE' }] })
    if (path.endsWith('/a/processing-runs')) return json(page([{ processing_run_id: 'processing', status: 'SUCCEEDED', stage: 'PUBLISHED' }]))
    if (path.endsWith('/a/coverage')) return json(page([{ coverage_id: 'c', range, modality: 'ASR', stage: 'OBSERVED' }]))
    if (path.endsWith('/a/observations')) return json(page([{ observation_id: 'o', range, modality: 'ASR', text: '站台广播' }]))
    if (path.endsWith('/a/notes')) return json({ artifact_id: 'notes' })
    if (path.endsWith('/artifacts/notes')) return json({ url: '/notes.json' })
    if (path.endsWith('/notes.json')) return json({
      chapters: [{ ...range, claims: [{ range, text: '广播通知列车晚点', evidence_id: 'ev', source: 'ASR', support: 'SUPPORTED' }] }],
    })
  })
  const wrapper = render()
  await flushPromises()
  await click(wrapper, '素材与笔记')
  await flushPromises()
  const panel = wrapper.get('dialog[open]')
  expect(panel.get('.cap-list').text()).toContain('语音转写 · 可用')
  expect(panel.text()).toContain('站台广播')
  await click(panel, '打开观影笔记')
  await flushPromises()
  await panel.get('.notes-body button').trigger('click')
  expect(wrapper.find('dialog[open]').exists()).toBe(false)
  expect(monitor(wrapper).element.currentTime).toBe(2)
  expect(wrapper.get('output').text()).toBe('0:02 – 0:03.5')
})

test('top-bar upload can be paused and the same file resumes through its saved upload', async () => {
  const signed = '/upload-signed'
  let complete = false
  const upload = { upload_mode: 'SINGLE_PUT', upload_id: 'up', parts: [{ part_number: 1, url: signed, headers: { 'Content-Type': 'video/mp4' } }] }
  const fetch = server((path, options, query) => {
    if (path === '/v1/projects/p/assets' && options.method === 'POST') return json(upload, 201)
    if (path === '/v1/projects/p/assets') return json(page(complete ? [asset] : []))
    if (path === signed && !query.has('resumed')) {
      return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true }))
    }
    if (path === '/v1/uploads/up') return json({ status: 'UPLOADING', uploaded_parts: [] })
    if (path === '/v1/uploads/up/refresh') return json({ ...upload, parts: [{ ...upload.parts[0], url: signed + '?resumed=1' }] })
    if (path === signed) return new Response(null, { headers: { ETag: '"digest"' } })
    if (path === '/v1/uploads/up/complete') {
      complete = true
      return json(asset)
    }
  })
  const wrapper = render()
  await flushPromises()
  const input = wrapper.get('input[aria-label="添加视频"]')
  const file = new File(['data'], 'station.mp4', { type: 'video/mp4', lastModified: 123 })
  await setFiles(input, [file])
  await flushPromises()
  expect(wrapper.get('progress').attributes('value')).toBe('0')
  await click(wrapper, '暂停上传')
  await flushPromises()
  expect(wrapper.get('.studio-notices').text()).toContain('上传已暂停')
  await setFiles(input, [file])
  await flushPromises()
  expect(requestBody(fetch, '/v1/uploads/up/refresh')).toMatchObject({ part_numbers: [1] })
  expect(requestBody(fetch, '/v1/uploads/up/complete').parts).toEqual([{ part_number: 1, etag: 'digest' }])
  expect(wrapper.get('.asset-chip').text()).toContain(asset.filename)
  expect(wrapper.find('progress').exists()).toBe(false)
})

test('upload sends file bytes only to the signed URL and reports confirmed progress', async () => {
  const signed = 'https://storage.test/one-shot?signature=test'
  const progress = vi.fn()
  const fetch = vi.fn(async url => {
    if (url === signed) return new Response(null, { headers: { ETag: '"digest"' } })
    if (url.endsWith('/assets')) return json({ upload_mode: 'SINGLE_PUT', upload_id: 'up', parts: [{ part_number: 1, url: signed, headers: { 'Content-Type': 'video/mp4' } }] })
    return json(asset)
  })
  vi.stubGlobal('fetch', fetch)
  const file = new File(['data'], 'test.mp4', { type: 'video/mp4' })
  await uploadAsset('p', file, { progress })
  const [create, put, complete] = fetch.mock.calls
  expect(JSON.parse(create[1].body)).toMatchObject({ filename: 'test.mp4', size_bytes: 4 })
  expect(put[1]).toMatchObject({ method: 'PUT', body: file, credentials: 'omit', headers: { 'Content-Type': 'video/mp4' } })
  expect(new Headers(put[1].headers).has('Authorization')).toBe(false)
  expect(JSON.parse(complete[1].body).parts).toEqual([{ part_number: 1, etag: 'digest' }])
  expect(progress).toHaveBeenCalledWith(4, 4)
})

test('a retried edit keeps its idempotency key, and invalid trim ranges are rejected', async () => {
  const send = makeCommand()
  const fetch = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(json({ ok: true }))
  vi.stubGlobal('fetch', fetch)
  const body = { expected_revision: 4, operations: [] }
  await expect(send('/edits', body)).rejects.toThrow()
  await send('/edits', body)
  expect(JSON.parse(fetch.mock.calls[0][1].body).idempotency_key).toBe(JSON.parse(fetch.mock.calls[1][1].body).idempotency_key)
  for (const [start, end] of [[0, ''], [2, 1], [0, 15.000001]]) {
    expect(() => clipDraft(asset, start, end, 'keep')).toThrow('区间')
  }
})
