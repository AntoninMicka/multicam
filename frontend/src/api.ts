import type { Device, Session } from './types'

export type DeviceCapabilities = Device['capabilities']

interface UploadStatus {
  upload_id: string
  received_chunks: number[]
  total_chunks: number
  size_bytes: number
  complete: boolean
}

export interface UploadReceipt {
  upload_id: string
  capture_id: string
  kind: 'recording' | 'telemetry'
  receipt_id: string
  file_path: string
  size_bytes: number
  sha256: string
  verified: boolean
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error((await response.json()).detail ?? 'Požadavek selhal')
  return response.json() as Promise<T>
}

export function createSession(name: string): Promise<Session> {
  return fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then(json<Session>)
}

export function getSession(id: string): Promise<Session> {
  return fetch(`/api/sessions/${id}`).then(json<Session>)
}

export function getCurrentSession(): Promise<Session> {
  return fetch('/api/sessions/current').then(json<Session>)
}

export function registerDevice(
  sessionId: string,
  name: string,
  role: Device['role'],
  capabilities: DeviceCapabilities,
  deviceId?: string,
): Promise<Device> {
  return fetch(`/api/sessions/${sessionId}/devices`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, role, device_id: deviceId, capabilities }),
  }).then(json<Device>)
}

export function sessionSocket(sessionId: string, deviceId?: string): WebSocket {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
  const query = deviceId ? `?device_id=${deviceId}` : ''
  return new WebSocket(`${protocol}://${location.host}/api/ws/${sessionId}${query}`)
}

async function sha256(data: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await data.arrayBuffer())
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

async function retry<T>(operation: () => Promise<T>, attempts = 3): Promise<T> {
  let lastError: unknown
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await operation()
    } catch (reason) {
      lastError = reason
      if (attempt + 1 < attempts) await new Promise((resolve) => setTimeout(resolve, 500 * 2 ** attempt))
    }
  }
  throw lastError
}

export async function uploadArtifact(
  sessionId: string,
  deviceId: string,
  captureId: string,
  kind: 'recording' | 'telemetry',
  artifact: Blob,
  onProgress: (percent: number) => void,
): Promise<UploadReceipt> {
  const chunkSize = 4 * 1024 * 1024
  const totalChunks = Math.ceil(artifact.size / chunkSize)
  const fileHash = await sha256(artifact)
  const extension = artifact.type.includes('mp4') ? 'mp4' : 'webm'
  const base = `/api/sessions/${sessionId}/devices/${deviceId}/uploads`
  const upload = await retry(() => fetch(base, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      capture_id: captureId,
      kind,
      file_name: kind === 'telemetry' ? 'timing.jsonl' : `recording.${extension}`,
      mime_type: artifact.type || 'application/octet-stream',
      size_bytes: artifact.size,
      sha256: fileHash,
      chunk_size: chunkSize,
      total_chunks: totalChunks,
    }),
  }).then(json<UploadStatus>))

  const received = new Set(upload.received_chunks)
  let completedChunks = received.size
  onProgress(Math.round(completedChunks / totalChunks * 100))
  for (let index = 0; index < totalChunks; index += 1) {
    if (received.has(index)) continue
    const chunk = artifact.slice(index * chunkSize, Math.min((index + 1) * chunkSize, artifact.size))
    const chunkHash = await sha256(chunk)
    await retry(() => fetch(`${base}/${upload.upload_id}/chunks/${index}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': chunkHash },
      body: chunk,
    }).then(json<UploadStatus>))
    completedChunks += 1
    onProgress(Math.round(completedChunks / totalChunks * 100))
  }
  return retry(() => fetch(`${base}/${upload.upload_id}/complete`, { method: 'POST' }).then(json<UploadReceipt>))
}
