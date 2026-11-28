import { apiRequest } from '../api.js'

export async function request(path, body, signal, method = body === undefined ? 'GET' : 'POST') {
  const options = { method, signal }
  if (body instanceof FormData) options.body = body
  else if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' }
    options.body = JSON.stringify(body)
  }
  const response = await apiRequest('/v1' + path, options)
  if (!response.ok) {
    const text = await response.text()
    let message = text
    try {
      const data = JSON.parse(text)
      message = data.message || (typeof data.detail === 'string' && data.detail) || text
    } catch { /* Some adapters return plain text. */ }
    const error = new Error(message || ('请求失败 (' + response.status + ')'))
    error.status = response.status
    throw error
  }
  return response.json()
}

export async function listAll(path, signal) {
  const items = []
  let cursor
  do {
    const query = new URLSearchParams({ limit: '100' })
    if (cursor) query.set('cursor', cursor)
    const page = await request(path + (path.includes('?') ? '&' : '?') + query, undefined, signal)
    items.push(...page.items)
    cursor = page.next_cursor
  } while (cursor)
  return items
}

export const fileExpired = file => file.url_expires_at && Date.parse(file.url_expires_at) <= Date.now() + 1000

export async function renderArtifact(id, kind, revisionId, signal) {
  const file = await request('/artifacts/' + id, undefined, signal)
  if (!file.url) throw new Error('成片暂不可用')
  if (file.kind !== kind || (revisionId && file.timeline_revision_id !== revisionId)) {
    throw new Error('成片与所选时间线版本不符')
  }
  return file
}

// An interrupted response can be retried with the same key, even after a commit.
export function makeCommand() {
  let previous, key
  return async (path, body, method = 'POST') => {
    const signature = JSON.stringify([method, path, body])
    if (signature !== previous) {
      previous = signature
      key = crypto.randomUUID()
    }
    const result = await request(path, { ...body, idempotency_key: key }, undefined, method)
    previous = null
    return result
  }
}

export async function uploadAsset(projectId, file, { signal, progress = () => {} } = {}) {
  const key = 'cutsieve-upload:' + JSON.stringify([projectId, file.name, file.size, file.lastModified])
  const saved = JSON.parse(localStorage.getItem(key) || 'null')
  let upload, received = []
  if (saved) {
    const state = await request('/uploads/' + saved.id, undefined, signal)
    if (state.status === 'COMPLETED') {
      localStorage.removeItem(key)
      return request('/assets/' + state.asset_id, undefined, signal)
    }
    received = state.uploaded_parts
    upload = await request('/uploads/' + saved.id + '/refresh', {
      part_numbers: saved.numbers, idempotency_key: crypto.randomUUID(),
    }, signal)
  } else {
    upload = await request('/projects/' + projectId + '/assets', {
      filename: file.name, size_bytes: file.size, content_type: file.type || 'application/octet-stream',
    }, signal)
    localStorage.setItem(key, JSON.stringify({ id: upload.upload_id, numbers: upload.parts.map(p => p.part_number) }))
  }
  const parts = []
  let bytes = 0
  for (const part of upload.parts) {
    signal?.throwIfAborted()
    let body = file
    if (upload.upload_mode === 'MULTIPART') {
      const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(part.headers['Content-Range'])
      if (!match || Number(match[3]) !== file.size) throw new Error('分片范围与所选视频不一致')
      body = file.slice(Number(match[1]), Number(match[2]) + 1)
    }
    const previous = received.find(p => p.part_number === part.part_number)
    let etag
    if (previous) {
      const digest = await crypto.subtle.digest('SHA-256', await body.arrayBuffer())
      etag = Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('')
      if (etag !== previous.etag) {
        localStorage.removeItem(key)
        throw new Error('所选文件与已上传的分片不同。请重新选择文件以创建新的上传。')
      }
    } else {
      const put = await fetch(part.url, { method: 'PUT', body, headers: part.headers, credentials: 'omit', signal })
      if (!put.ok) throw new Error('上传失败，重新选择同一文件可续传 (' + put.status + ')')
      etag = (put.headers.get('etag') || '').replaceAll('"', '')
    }
    parts.push({ part_number: part.part_number, etag })
    bytes += body.size
    progress(bytes, file.size)
  }
  const asset = await request('/uploads/' + upload.upload_id + '/complete', {
    idempotency_key: 'complete-' + upload.upload_id, parts,
  }, signal)
  localStorage.removeItem(key)
  return asset
}

export const defaultProfile = { width: 1280, height: 720, fps_num: 30, fps_den: 1, audio_sample_rate: 48000 }
export const seconds = microseconds => microseconds / 1_000_000

export function clipDraft(asset, start, end, audio) {
  const startUs = Math.round(Number(start) * 1_000_000)
  const endUs = Math.round(Number(end) * 1_000_000)
  if (!asset || [start, end].some(v => v === '' || v == null) ||
      !Number.isSafeInteger(startUs) || !Number.isSafeInteger(endUs) ||
      startUs < 0 || endUs <= startUs || endUs > asset.duration_us) {
    throw new Error('请在原片时长内选择区间，出点必须晚于入点。')
  }
  return { media_revision_id: asset.media_revision_id, source_start_us: startUs, source_end_us: endUs,
    evidence_ids: [], audio_policy: audio }
}
