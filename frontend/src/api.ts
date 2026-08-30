import type { Device, Session } from './types'

export type DeviceCapabilities = Device['capabilities']

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
