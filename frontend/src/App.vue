<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { createSession, getCurrentSession, getSession, registerDevice, sessionSocket, type DeviceCapabilities } from './api'
import type { Session } from './types'

type Role = 'director' | 'main_camera' | 'top_camera' | 'secondary_camera'
const role = ref<Role | null>(null)
const session = ref<Session | null>(null)
const sessionName = ref('Zkušební relace')
const deviceName = ref(`Telefon ${Math.floor(Math.random() * 90 + 10)}`)
const deviceId = ref(localStorage.getItem('multicam.deviceId') ?? '')
const error = ref('')
const busy = ref(false)
const flashVisible = ref(false)
const preview = ref<HTMLVideoElement | null>(null)
const recording = ref(false)
const recordingUrl = ref('')
const cameraReady = ref(false)
let socket: WebSocket | null = null
let flashTimer: number | undefined
let mediaStream: MediaStream | null = null
let mediaRecorder: MediaRecorder | null = null
let recordedChunks: Blob[] = []

const devices = computed(() => Object.values(session.value?.devices ?? {}))

async function readCapabilities(): Promise<DeviceCapabilities> {
  const estimate = await navigator.storage?.estimate?.().catch(() => undefined)
  const permission = async (name: 'camera' | 'microphone') => {
    try {
      const result = await navigator.permissions.query({ name } as PermissionDescriptor)
      return result.state === 'granted' ? true : result.state === 'denied' ? false : null
    } catch {
      return null
    }
  }
  let batteryPercent: number | null = null
  try {
    const battery = await (navigator as Navigator & { getBattery?: () => Promise<{ level: number }> }).getBattery?.()
    if (battery) batteryPercent = Math.round(battery.level * 100)
  } catch { /* API není dostupné na všech telefonech. */ }
  return {
    battery_percent: batteryPercent,
    free_storage_bytes: estimate?.quota && estimate.usage !== undefined ? estimate.quota - estimate.usage : null,
    camera_permission: await permission('camera'),
    microphone_permission: await permission('microphone'),
  }
}

function formatBytes(value: number | null): string {
  if (value === null) return 'neznámé'
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}

function permissionLabel(value: boolean | null): string {
  return value === true ? 'ano' : value === false ? 'ne' : 'nezjištěno'
}

function connectSocket(id: string, cameraId?: string) {
  socket?.close()
  socket = sessionSocket(id, cameraId)
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data)
    if (message.type === 'session.snapshot' || message.type === 'session.updated') session.value = message.payload
    if (message.type === 'clap.trigger' && role.value === 'main_camera') flashClap()
    if (message.type === 'recording.start' && role.value !== 'director') startLocalRecording()
    if (message.type === 'recording.stop' && role.value !== 'director') stopLocalRecording()
  }
  socket.onerror = () => (error.value = 'Spojení se serverem bylo přerušeno.')
}

async function startDirector() {
  busy.value = true
  error.value = ''
  try {
    session.value = await createSession(sessionName.value)
    connectSocket(session.value.session_id)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Relaci nelze vytvořit.'
  } finally {
    busy.value = false
  }
}

async function joinCamera() {
  busy.value = true
  error.value = ''
  try {
    const activeSession = await getCurrentSession()
    const capabilities = await readCapabilities()
    const cameraRole = role.value === 'main_camera'
      ? 'main_camera'
      : role.value === 'top_camera' ? 'top_camera' : 'secondary_camera'
    const device = await registerDevice(activeSession.session_id, deviceName.value, cameraRole, capabilities, deviceId.value || undefined)
    deviceId.value = device.device_id
    localStorage.setItem('multicam.deviceId', device.device_id)
    session.value = await getSession(activeSession.session_id)
    connectSocket(activeSession.session_id, device.device_id)
    await nextTick()
    await prepareCamera()
    if (activeSession.state === 'recording') startLocalRecording()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'K relaci se nelze připojit.'
  } finally {
    busy.value = false
  }
}

function triggerClap() {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    error.value = 'Hlavní kamera není připojená k řídicímu kanálu.'
    return
  }
  socket.send(JSON.stringify({ type: 'clap.trigger', payload: { requested_at: new Date().toISOString() } }))
}

function sendRecordingCommand(type: 'recording.start' | 'recording.stop') {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    error.value = 'Řídicí kanál není připojený.'
    return
  }
  socket.send(JSON.stringify({ type, payload: { requested_at: new Date().toISOString() } }))
}

async function prepareCamera() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' } },
      audio: true,
    })
    if (preview.value) preview.value.srcObject = mediaStream
    cameraReady.value = true
  } catch {
    error.value = 'Kamera nebo mikrofon nejsou dostupné. Zkontrolujte oprávnění a HTTPS.'
  }
}

function startLocalRecording() {
  if (!mediaStream || recording.value) return
  try {
    const preferredTypes = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4']
    const mimeType = preferredTypes.find((type) => MediaRecorder.isTypeSupported(type))
    recordedChunks = []
    if (recordingUrl.value) URL.revokeObjectURL(recordingUrl.value)
    recordingUrl.value = ''
    mediaRecorder = new MediaRecorder(mediaStream, mimeType ? { mimeType } : undefined)
    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size) recordedChunks.push(event.data)
    }
    mediaRecorder.onstop = () => {
      const blob = new Blob(recordedChunks, { type: mediaRecorder?.mimeType || 'video/webm' })
      recordingUrl.value = URL.createObjectURL(blob)
    }
    mediaRecorder.start(1000)
    recording.value = true
  } catch {
    error.value = 'Záznam se na tomto zařízení nepodařilo spustit.'
  }
}

function stopLocalRecording() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return
  mediaRecorder.stop()
  recording.value = false
}

function flashClap() {
  window.clearTimeout(flashTimer)
  flashVisible.value = true
  flashTimer = window.setTimeout(() => (flashVisible.value = false), 700)
}

function roleLabel(value: Role): string {
  if (value === 'director') return 'režisér'
  if (value === 'main_camera') return 'hlavní kamera'
  return value === 'top_camera' ? 'top-over kamera' : 'vedlejší kamera'
}

onBeforeUnmount(() => {
  socket?.close()
  stopLocalRecording()
  mediaStream?.getTracks().forEach((track) => track.stop())
  if (recordingUrl.value) URL.revokeObjectURL(recordingUrl.value)
  window.clearTimeout(flashTimer)
})
</script>

<template>
  <main>
    <header><span class="eyebrow">lokální capture systém</span><h1>MultiCam</h1></header>

    <section v-if="!role" class="card role-picker">
      <h2>Jak chcete zařízení použít?</h2>
      <button @click="role = 'director'">Režisérský pult</button>
      <button class="secondary" @click="role = 'main_camera'">Hlavní kamera</button>
      <button class="secondary" @click="role = 'top_camera'">Top-over kamera</button>
      <button class="secondary" @click="role = 'secondary_camera'">Vedlejší kamera</button>
    </section>

    <section v-else-if="!session" class="card">
      <button class="back" @click="role = null">← změnit roli</button>
      <template v-if="role === 'director'">
        <h2>Nová relace</h2>
        <label>Název <input v-model="sessionName" /></label>
        <button :disabled="busy" @click="startDirector">Založit relaci</button>
      </template>
      <template v-else>
        <h2>Připojit: {{ roleLabel(role) }}</h2>
        <label>Název zařízení <input v-model.trim="deviceName" /></label>
        <p class="muted">Aplikace automaticky použije právě aktivní relaci.</p>
        <button :disabled="busy || !deviceName" @click="joinCamera">Připojit</button>
      </template>
    </section>

    <section v-else class="card">
      <div class="session-heading"><div><span class="eyebrow">{{ roleLabel(role) }}</span><h2>{{ session.name }}</h2></div><span class="status">{{ session.state }}</span></div>

      <template v-if="role === 'director'">
        <h3>Zařízení ({{ devices.length }})</h3>
        <div class="record-controls">
          <button v-if="session.state !== 'recording'" :disabled="!devices.length" @click="sendRecordingCommand('recording.start')">● Spustit záznam</button>
          <button v-else class="stop" @click="sendRecordingCommand('recording.stop')">■ Zastavit záznam</button>
        </div>
        <button class="clap-button" :disabled="!devices.some(device => device.role === 'main_camera' && device.connected)" @click="triggerClap">Spustit světelnou klapku</button>
        <p v-if="!devices.length" class="muted">Čekám na připojení prvního telefonu…</p>
        <article v-for="device in devices" :key="device.device_id" class="device">
          <div><strong>{{ device.name }} · {{ roleLabel(device.role) }}</strong><small>Baterie: {{ device.capabilities.battery_percent ?? 'neznámá' }} % · Volno: {{ formatBytes(device.capabilities.free_storage_bytes) }}</small><small>Kamera: {{ permissionLabel(device.capabilities.camera_permission) }} · Mikrofon: {{ permissionLabel(device.capabilities.microphone_permission) }}</small></div>
          <span :class="['dot', { offline: !device.connected }]">{{ device.connected ? device.state : 'odpojeno' }}</span>
        </article>
      </template>
      <template v-else>
        <video ref="preview" class="preview" autoplay muted playsinline></video>
        <div class="ready"><span>✓</span><div><strong>Zařízení je připravené</strong><small>{{ deviceName }}</small></div></div>
        <div v-if="role === 'main_camera'" class="record-controls">
          <button v-if="session.state !== 'recording'" :disabled="!cameraReady" @click="sendRecordingCommand('recording.start')">● Spustit záznam</button>
          <button v-else class="stop" @click="sendRecordingCommand('recording.stop')">■ Zastavit záznam</button>
        </div>
        <p v-else-if="recording" class="recording-indicator">● Probíhá záznam</p>
        <button v-if="role === 'main_camera'" class="clap-button" @click="flashClap">Otestovat světelnou klapku</button>
        <a v-if="recordingUrl" class="download" :href="recordingUrl" :download="`${role}-${Date.now()}.webm`">Stáhnout poslední záznam</a>
      </template>
    </section>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="flashVisible" class="flash" aria-label="Světelná klapka"></div>
  </main>
</template>
