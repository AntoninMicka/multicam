export type DeviceState = 'disconnected' | 'ready' | 'armed' | 'recording' | 'stored' | 'uploading' | 'verified'

export interface Device {
  device_id: string
  name: string
  role: 'main_camera' | 'top_camera' | 'secondary_camera'
  state: DeviceState
  connected: boolean
  last_seen_at: string
  owner_backend_id?: string | null
  owner_backend_name?: string | null
  capabilities: {
    battery_percent: number | null
    free_storage_bytes: number | null
    camera_permission: boolean | null
    microphone_permission: boolean | null
  }
}

export interface Session {
  schema_version: string
  session_id: string
  name: string
  state: 'created' | 'armed' | 'recording' | 'stopped'
  created_at: string
  devices: Record<string, Device>
}
