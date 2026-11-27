import assert from 'node:assert/strict'
import test from 'node:test'
import { apiRequest } from './api.js'

test('API network failures produce an actionable message', async t => {
  const cause = new TypeError('fetch failed')
  t.mock.method(globalThis, 'fetch', async () => { throw cause })
  await assert.rejects(apiRequest('/health'), {
    message: '无法连接后端服务，请确认后端已启动且地址配置正确',
    cause,
  })
})

test('API requests preserve abort errors', async t => {
  const abort = new DOMException('Request aborted', 'AbortError')
  t.mock.method(globalThis, 'fetch', async () => { throw abort })
  await assert.rejects(apiRequest('/v1/projects'), error => error === abort)
})

test('API envelope exposes its data and preserves the HTTP response metadata', async t => {
  const data = { project_id: 'local' }
  const options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"title":"Local"}' }
  const fetch = t.mock.method(globalThis, 'fetch', async () =>
    Response.json({ code: 0, message: '', data }, { status: 202 }))

  const response = await apiRequest('/v1/projects', options)
  assert.deepEqual(fetch.mock.calls[0].arguments, ['/v1/projects', options])
  assert.equal(response.ok, true)
  assert.equal(response.status, 202)
  assert.equal(response.headers.get('content-type'), 'application/json')
  assert.deepEqual(await response.json(), data)
  assert.equal(await response.text(), JSON.stringify(data))
})

test('API error envelope exposes its message when data is omitted', async t => {
  t.mock.method(globalThis, 'fetch', async () =>
    Response.json({ code: 422, message: '请求参数无效' }, { status: 422 }))

  const response = await apiRequest('/v1/projects')
  assert.equal(response.ok, false)
  assert.equal(response.status, 422)
  assert.equal(await response.json(), null)
  assert.equal(await response.text(), '请求参数无效')
})

test('API requests pass through plain JSON with its body intact', async t => {
  const data = { items: [], next_cursor: null }
  const original = Response.json(data)
  t.mock.method(globalThis, 'fetch', async () => original)

  const response = await apiRequest('/v1/projects')
  assert.equal(response, original)
  assert.deepEqual(await response.json(), data)
})

test('API requests pass through SSE with its stream intact', async t => {
  const event = 'event: completed\ndata: {}\n\n'
  const original = new Response(event, { headers: { 'Content-Type': 'text/event-stream' } })
  t.mock.method(globalThis, 'fetch', async () => original)

  const response = await apiRequest('/v1/workflow-runs/run/events')
  assert.equal(response, original)
  assert.equal(response.bodyUsed, false)
  assert.equal(await response.text(), event)
})

test('API requests pass through binary downloads', async t => {
  const bytes = Uint8Array.of(1, 2, 255)
  const original = new Response(bytes, { headers: { 'Content-Type': 'video/mp4' } })
  t.mock.method(globalThis, 'fetch', async () => original)

  const response = await apiRequest('/v1/artifacts/video')
  assert.equal(response, original)
  assert.equal(response.bodyUsed, false)
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), bytes)
})
