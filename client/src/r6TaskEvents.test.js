import test from 'node:test'
import assert from 'node:assert/strict'
import { consumeStream } from './taskEvents.js'

const frame = event => new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)

test('cancelled SSE discards buffered frames and cancels/releases reader', async () => {
  let finishRead
  let cancelled = 0
  let released = 0
  const reader = { read: () => new Promise(resolve => { finishRead = resolve }), cancel: async () => { cancelled++ }, releaseLock: () => { released++ } }
  const controller = new AbortController()
  const events = []
  const consuming = consumeStream({ getReader: () => reader }, event => events.push(event), controller.signal)
  controller.abort()
  finishRead({ value: frame({ state: 'PROCESSING' }), done: false })
  await consuming
  assert.deepEqual(events, [])
  assert.ok(cancelled > 0)
  assert.equal(released, 1)
})

test('terminal SSE finishes and cancels a socket that never sends EOF', async () => {
  let cancelled = false
  const body = new ReadableStream({ start(controller) { controller.enqueue(frame({ state: 'COMPLETED', result: 'done' })); controller.enqueue(frame({ state: 'PROCESSING' })) }, cancel() { cancelled = true } })
  const events = []
  assert.equal(await consumeStream(body, event => events.push(event), new AbortController().signal), true)
  assert.equal(cancelled, true)
  assert.deepEqual(events.map(event => event.state), ['COMPLETED'])
  assert.equal(body.locked, false)
})

test('reader cleanup failures preserve the original read error', async () => {
  const failure = new Error('read failed')
  let releases = 0
  const reader = { read: async () => { throw failure }, cancel: () => { throw new Error('cancel failed') }, releaseLock: () => { releases++; throw new Error('release failed') } }
  await assert.rejects(consumeStream({ getReader: () => reader }, () => {}, new AbortController().signal), error => error === failure)
  assert.equal(releases, 1)
})

test('terminal delivery does not await a pending reader cancel', async () => {
  const events = []
  let releases = 0
  const reader = { read: async () => ({ value: frame({ state: 'FAILED' }), done: false }), cancel: () => new Promise(() => {}), releaseLock: () => { releases++ } }
  assert.equal(await consumeStream({ getReader: () => reader }, event => events.push(event), new AbortController().signal), true)
  assert.deepEqual(events.map(event => event.state), ['FAILED'])
  assert.equal(releases, 1)
})
