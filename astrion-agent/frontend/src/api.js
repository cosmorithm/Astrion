export async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`
    try { detail = (await response.json()).detail || detail } catch { /* 保留状态 */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return response.json()
}

// POST + fetch 支持在请求体中发送研究任务，再逐块读取 SSE。
export async function streamResearch(body, onMessage) {
  const response = await fetch('/api/research/stream', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = `研究请求失败 (${response.status})`
    try { detail = (await response.json()).detail || detail } catch { /* 保留状态 */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  if (!response.body) throw new Error('浏览器不支持读取流式响应')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let pending = ''
  let finished = false
  while (true) {
    const { value, done } = await reader.read()
    pending = (pending + decoder.decode(value || new Uint8Array(), { stream: !done })).replace(/\r\n/g, '\n')
    let boundary
    while ((boundary = pending.indexOf('\n\n')) >= 0) {
      const event = pending.slice(0, boundary)
      pending = pending.slice(boundary + 2)
      const data = event.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
      if (data) {
        const message = JSON.parse(data)
        if (message.type === 'done' || message.type === 'error') finished = true
        onMessage(message)
      }
    }
    if (done) break
  }
  if (!finished) throw new Error('连接中断，未收到研究结束事件')
}
