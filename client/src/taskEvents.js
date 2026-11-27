export async function consumeStream(body, onEvent, signal, current = () => !signal.aborted) {
  const reader = body.getReader()
  const cancel = () => {
    try { Promise.resolve(reader.cancel()).catch(() => {}) } catch { /* cleanup is best effort */ }
  }
  signal.addEventListener('abort', cancel, { once: true })
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (current()) {
      const { value, done } = await reader.read()
      if (!current()) return false
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() || ''
      for (const frame of frames) {
        if (!current()) return false
        const data = frame.split(/\r?\n/)
          .filter(line => line.startsWith('data:'))
          .map(line => line.slice(5).trimStart())
          .join('\n')
        if (!data) continue
        const event = JSON.parse(data)
        const terminal = event.state === 'COMPLETED' || event.state === 'FAILED'
        if (terminal) cancel()
        await onEvent(event)
        if (terminal) return true
      }
      if (done) return false
    }
    return false
  } finally {
    signal.removeEventListener('abort', cancel)
    cancel()
    try { reader.releaseLock() } catch { /* preserve read/handler errors */ }
  }
}
