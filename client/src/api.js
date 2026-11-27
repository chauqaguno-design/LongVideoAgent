const API_BASE = (import.meta.env?.VITE_API_BASE_URL || '').replace(/\/$/, '')

// 数字 code 和 message 标识响应信封。错误响应可以省略 data。
function isEnvelope(payload) {
  return payload !== null
    && typeof payload === 'object'
    && !Array.isArray(payload)
    && typeof payload.code === 'number'
    && 'message' in payload
}

/** 把 data 转成文本：字符串直接用，空值给空串，其余序列化。 */
function dataAsText(data) {
  if (data === null || data === undefined) return ''
  return typeof data === 'string' ? data : JSON.stringify(data)
}

function unwrap(response, envelope) {
  const payload = envelope.data ?? null

  return {
    ok: response.ok,
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
    redirected: response.redirected,
    url: response.url,
    json: async () => payload,
    text: async () => (response.ok ? dataAsText(payload) : (envelope.message || '')),
    raw: response
  }
}

export async function apiRequest(path, options = {}) {
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, options)
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new Error('无法连接后端服务，请确认后端已启动且地址配置正确', { cause: error })
  }

  // 非 JSON（SSE / 音频流 / 空响应）原样返回，绝不触碰 body。
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) return response

  let envelope
  try {
    envelope = await response.clone().json()
  } catch {
    // 声明是 JSON 却解析不了（例如空 body），退回原始响应交给调用方处理。
    return response
  }
  if (!isEnvelope(envelope)) return response

  return unwrap(response, envelope)
}
