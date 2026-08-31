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

export interface CaptureMedia {
  capture_id: string
  take_id: string | null
  device_id: string
  device_name: string
  role: Device['role']
  mime_type: string
  size_bytes: number
  created_at: string | null
  video_url: string
  telemetry_url: string | null
  sync_point_seconds: number | null
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

export function listSessions(): Promise<Session[]> {
  return fetch('/api/sessions').then(json<Session[]>)
}

export function getSession(id: string): Promise<Session> {
  return fetch(`/api/sessions/${id}`).then(json<Session>)
}

export function getCurrentSession(): Promise<Session> {
  return fetch('/api/sessions/current').then(json<Session>)
}

export function listSessionMedia(sessionId: string): Promise<CaptureMedia[]> {
  return fetch(`/api/sessions/${sessionId}/media`).then(json<CaptureMedia[]>)
}

export function getSessionReport(sessionId: string): Promise<Record<string, unknown>> {
  return fetch(`/api/sessions/${sessionId}/report`).then(json<Record<string, unknown>>)
}

export function analyzeSessionClaps(sessionId: string): Promise<{ captures: Record<string, { status: string }> }> {
  return fetch(`/api/sessions/${sessionId}/analyze-claps`, { method: 'POST' }).then(json<{ captures: Record<string, { status: string }> }>)
}

export interface VisionJob {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  status_url?: string
  error?: string
  result_file?: string
}

export function startTopdownAnalysis(
  sessionId: string,
  takeId: string,
  backend: 'prepare' | 'ollama' | 'comfyui',
  model?: string,
): Promise<VisionJob> {
  return fetch(`/api/sessions/${sessionId}/takes/${takeId}/topdown-analysis`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ backend, model: model || null }),
  }).then(json<VisionJob>)
}

export function getVisionJob(url: string): Promise<VisionJob> {
  return fetch(url).then(json<VisionJob>)
}

export function deleteServerCapture(sessionId: string, deviceId: string, captureId: string): Promise<{ deleted: boolean }> {
  return fetch(`/api/media/${sessionId}/${deviceId}/${captureId}`, { method: 'DELETE' }).then(json<{ deleted: boolean }>)
}

export function deleteServerTake(sessionId: string, takeId: string): Promise<{ deleted: number }> {
  return fetch(`/api/sessions/${sessionId}/takes/${takeId}`, { method: 'DELETE' }).then(json<{ deleted: number }>)
}

export function getTelemetry(url: string): Promise<TelemetrySample[]> {
  return fetch(url).then(json<TelemetrySample[]>)
}

export interface TelemetrySample {
  event: string
  recording_offset_ms: number | null
  camera?: { zoom_ratio: number | null }
  position?: { latitude: number; longitude: number; accuracy_m: number } | null
  orientation?: { alpha_deg: number | null; beta_deg: number | null; gamma_deg: number | null } | null
}

export interface HotspotStatus {
  active: boolean
  ssid?: string
  password?: string
  address?: string
  app_url?: string
  interface?: string
}

export function getHotspotStatus(): Promise<HotspotStatus> {
  return fetch('/api/hotspot').then(json<HotspotStatus>)
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
  takeId?: string,
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
      take_id: takeId,
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
