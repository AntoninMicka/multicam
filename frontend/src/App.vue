<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  createSession,
  getCurrentSession,
  getSessionReport,
  getSession,
  listSessionMedia,
  listSessions,
  registerDevice,
  sessionSocket,
  uploadArtifact,
  type CaptureMedia,
  type DeviceCapabilities,
} from './api'
import CaptureGroup from './CaptureGroup.vue'
import ArchivePage from './ArchivePage.vue'
import HotspotPanel from './HotspotPanel.vue'
import {
  appendRecordingChunk,
  appendTelemetryEvent,
  createLocalCapture,
  deleteLocalCapture,
  listLocalCaptures,
  readLocalArtifacts,
  setLocalCaptureState,
  type LocalCapture,
} from './recordingStore'
import type { Session } from './types'

type Role = 'director' | 'main_camera' | 'top_camera' | 'secondary_camera'
const role = ref<Role | null>(null)
const archiveOpen = ref(false)
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
const uploadProgress = ref<number | null>(null)
const uploadVerified = ref(false)
const deviceUploadProgress = ref<Record<string, number>>({})
const localCaptures = ref<LocalCapture[]>([])
const availableSessions = ref<Session[]>([])
const sessionMedia = ref<CaptureMedia[]>([])
const recordingStarting = ref(false)
const recordingFinalizing = ref(false)
const uploadingCaptureId = ref<string | null>(null)
type AckStatus = 'pending' | 'ready' | 'started' | 'stopped' | 'error' | 'timeout'
interface ControlAck { status: AckStatus; detail?: string }
const controlAcks = ref<Record<string, ControlAck>>({})
const activeCommandId = ref('')
const activeCommandType = ref('')
const operationalWarnings = ref<string[]>([])
const clockMetrics = ref<Record<string, { offset_ms: number; rtt_ms: number }>>({})
let socket: WebSocket | null = null
let commandTimeout: number | undefined
let clockTimer: number | undefined
let wakeLock: { release: () => Promise<void>; released?: boolean } | null = null
let flashTimer: number | undefined
let torchTimer: number | undefined
let mediaStream: MediaStream | null = null
let mediaRecorder: MediaRecorder | null = null
let captureId = ''
let chunkIndex = 0
let telemetryIndex = 0
let pendingChunkWrites: Promise<void>[] = []
let pendingTelemetryWrites: Promise<void>[] = []
let recordingStartedAt: number | null = null
let telemetryTimer: number | undefined
let geolocationWatchId: number | undefined

interface PositionSample {
  latitude: number
  longitude: number
  accuracy_m: number
  altitude_m: number | null
  altitude_accuracy_m: number | null
  heading_deg: number | null
  speed_m_s: number | null
  timestamp_ms: number
}

interface OrientationSample {
  alpha_deg: number | null
  beta_deg: number | null
  gamma_deg: number | null
  absolute: boolean
}

let latestPosition: PositionSample | null = null
let latestOrientation: OrientationSample | null = null

interface TelemetryEvent {
  schema_version: '1.0'
  event: 'recording_requested' | 'recording_started' | 'recording_stopped' | 'clock_sample' | 'clock_sync' | 'sync_marker'
  monotonic_ms: number
  recording_offset_ms: number | null
  utc_time: string
  camera: {
    zoom_ratio: number | null
    field_of_view_deg: number | null
    width: number | null
    height: number | null
  }
  position: PositionSample | null
  orientation: OrientationSample | null
  details?: Record<string, unknown>
}

const devices = computed(() => Object.values(session.value?.devices ?? {}))
const connectedDevices = computed(() => devices.value.filter((device) => device.connected))
const allCamerasArmed = computed(() => connectedDevices.value.length > 0 && connectedDevices.value.every(
  (device) => controlAcks.value[device.device_id]?.status === 'ready' || device.state === 'armed',
))
const captureGroups = computed(() => {
  const groups = new Map<string, CaptureMedia[]>()
  for (const capture of sessionMedia.value) {
    // Starší záznamy nemají identifikátor společné klapky, proto zůstávají
    // samostatně a nemohou být omylem spojeny s jiným natáčením.
    const key = capture.take_id ?? `legacy-${capture.capture_id}`
    const group = groups.get(key) ?? []
    group.push(capture)
    groups.set(key, group)
  }
  return [...groups.entries()]
    .map(([takeId, captures]) => ({ takeId, captures }))
    .sort((left, right) => (right.captures[0]?.created_at ?? '').localeCompare(left.captures[0]?.created_at ?? ''))
})

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

function ackLabel(ack: ControlAck | undefined): string {
  if (!ack) return ''
  return {
    pending: 'čekám na potvrzení',
    ready: 'ARM potvrzen',
    started: 'START potvrzen',
    stopped: 'STOP potvrzen',
    error: `chyba${ack.detail ? `: ${ack.detail}` : ''}`,
    timeout: 'bez odpovědi',
  }[ack.status]
}

function connectSocket(id: string, cameraId?: string) {
  socket?.close()
  socket = sessionSocket(id, cameraId)
  socket.onopen = () => {
    if (!cameraId) return
    sendClockPing()
    window.clearInterval(clockTimer)
    clockTimer = window.setInterval(sendClockPing, 5000)
  }
  socket.onmessage = async (event) => {
    const message = JSON.parse(event.data)
    if (message.type === 'session.snapshot' || message.type === 'session.updated') {
      session.value = message.payload
      if (role.value === 'director') void loadSessionMedia()
    }
    if (message.type === 'clap.trigger' && role.value !== 'director' && recording.value) {
      recordTelemetry('sync_marker', message.payload)
      if (role.value === 'main_camera') flashClap()
    }
    if (message.type === 'control.arm' && role.value !== 'director') {
      const readiness = await checkCameraReadiness()
      sendControlAck(message.payload.command_id, readiness.ready ? 'ready' : 'error', readiness.detail)
    }
    if (message.type === 'recording.start' && role.value !== 'director') {
      const started = await startLocalRecording(message.payload)
      sendControlAck(message.payload.command_id, started ? 'started' : 'error', started ? undefined : error.value)
    }
    if (message.type === 'recording.stop' && role.value !== 'director') {
      const stopped = stopLocalRecording()
      sendControlAck(message.payload.command_id, stopped ? 'stopped' : 'error', stopped ? undefined : 'Na zařízení neběžel záznam.')
    }
    if (message.type === 'control.ack' && role.value === 'director' && message.payload.command_id === activeCommandId.value) {
      controlAcks.value[message.payload.device_id] = {
        status: message.payload.status,
        detail: message.payload.detail,
      }
    }
    if (message.type === 'control.rejected') {
      error.value = message.payload.detail ?? 'Server řídicí povel odmítl.'
    }
    if (message.type === 'clock.pong' && role.value !== 'director') handleClockPong(message.payload)
    if (message.type === 'clock.report' && role.value === 'director') {
      clockMetrics.value[message.payload.device_id] = { offset_ms: message.payload.offset_ms, rtt_ms: message.payload.rtt_ms }
    }
    if (message.type === 'upload.progress') {
      const { device_id, received_chunks, total_chunks } = message.payload
      deviceUploadProgress.value[device_id] = Math.round(received_chunks / total_chunks * 100)
    }
  }
  socket.onerror = () => (error.value = 'Spojení se serverem bylo přerušeno.')
}

async function checkCameraReadiness(): Promise<{ ready: boolean; detail?: string }> {
  if (!cameraReady.value || !mediaStream?.getVideoTracks().some((track) => track.readyState === 'live')) return { ready: false, detail: 'Kamera není aktivní.' }
  if (!mediaStream.getAudioTracks().some((track) => track.readyState === 'live')) return { ready: false, detail: 'Mikrofon není aktivní.' }
  const estimate = await navigator.storage?.estimate?.().catch(() => undefined)
  const free = estimate?.quota !== undefined && estimate.usage !== undefined ? estimate.quota - estimate.usage : null
  if (free !== null && free < 500 * 1024 * 1024) return { ready: false, detail: 'Volné místo je menší než 500 MB.' }
  return { ready: true }
}

function sendClockPing(): void {
  if (!socket || socket.readyState !== WebSocket.OPEN || role.value === 'director') return
  socket.send(JSON.stringify({ type: 'clock.ping', payload: { ping_id: crypto.randomUUID(), client_sent_ms: Date.now() } }))
}

function handleClockPong(payload: Record<string, number | string>): void {
  const clientReceived = Date.now()
  const clientSent = Number(payload.client_sent_ms)
  const serverReceived = Number(payload.server_received_ms)
  const serverSent = Number(payload.server_sent_ms)
  const metrics = {
    rtt_ms: Math.max(0, (clientReceived - clientSent) - (serverSent - serverReceived)),
    offset_ms: ((serverReceived - clientSent) + (serverSent - clientReceived)) / 2,
  }
  if (deviceId.value) clockMetrics.value[deviceId.value] = metrics
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'clock.report', payload: { ...metrics, measured_at: new Date().toISOString() } }))
  if (recording.value) recordTelemetry('clock_sync', metrics)
}

async function chooseRole(selectedRole: Role) {
  role.value = selectedRole
  error.value = ''
  if (selectedRole === 'director') {
    try {
      availableSessions.value = await listSessions()
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : 'Seznam relací nelze načíst.'
    }
  } else {
    await refreshLocalCaptures()
  }
}

async function selectSession(selected: Session) {
  session.value = await getSession(selected.session_id)
  connectSocket(selected.session_id)
  await loadSessionMedia()
}

async function backToSessions() {
  socket?.close()
  socket = null
  session.value = null
  sessionMedia.value = []
  availableSessions.value = await listSessions()
}

async function loadSessionMedia() {
  if (!session.value) return
  try {
    sessionMedia.value = await listSessionMedia(session.value.session_id)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Záznamy relace nelze načíst.'
  }
}

async function downloadSessionReport(): Promise<void> {
  if (!session.value) return
  try {
    const report = await getSessionReport(session.value.session_id)
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${session.value.name.replace(/[^a-zA-Z0-9_-]+/g, '-') || 'session'}-report.json`
    link.click()
    URL.revokeObjectURL(url)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Report relace nelze vytvořit.'
  }
}

async function startDirector() {
  busy.value = true
  error.value = ''
  try {
    session.value = await createSession(sessionName.value)
    connectSocket(session.value.session_id)
    await loadSessionMedia()
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
    await prepareSensors()
    await navigator.storage?.persist?.().catch(() => false)
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
    await refreshLocalCaptures()
    if (activeSession.state === 'recording') void startLocalRecording({ joined_during_recording: true })
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

function sendControlAck(commandId: string | undefined, status: 'ready' | 'started' | 'stopped' | 'error', detail?: string) {
  if (!commandId || !socket || socket.readyState !== WebSocket.OPEN) return
  socket.send(JSON.stringify({ type: 'control.ack', payload: { command_id: commandId, status, detail } }))
}

function sendRecordingCommand(type: 'control.arm' | 'recording.start' | 'recording.stop') {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    error.value = 'Řídicí kanál není připojený.'
    return
  }
  const commandId = crypto.randomUUID()
  activeCommandId.value = commandId
  activeCommandType.value = type
  controlAcks.value = Object.fromEntries(connectedDevices.value.map((device) => [device.device_id, { status: 'pending' }]))
  window.clearTimeout(commandTimeout)
  commandTimeout = window.setTimeout(() => {
    for (const device of connectedDevices.value) {
      if (controlAcks.value[device.device_id]?.status === 'pending') {
        controlAcks.value[device.device_id] = { status: 'timeout', detail: 'Kamera neodpověděla do 5 sekund.' }
      }
    }
  }, 5000)
  socket.send(JSON.stringify({ type, payload: { command_id: commandId, requested_at: new Date().toISOString() } }))
}

async function prepareCamera() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' } },
      audio: true,
    })
    if (preview.value) preview.value.srcObject = mediaStream
    mediaStream.getTracks().forEach((track) => track.addEventListener('ended', () => {
      const warning = `${track.kind === 'video' ? 'Kamera' : 'Mikrofon'} byl odpojen.`
      operationalWarnings.value = [...new Set([...operationalWarnings.value, warning])]
      cameraReady.value = false
      if (recording.value) error.value = 'Během záznamu došlo ke ztrátě kamery nebo mikrofonu.'
    }))
    cameraReady.value = true
  } catch {
    error.value = 'Kamera nebo mikrofon nejsou dostupné. Zkontrolujte oprávnění a HTTPS.'
  }
}

async function acquireWakeLock(): Promise<void> {
  const wakeLockApi = (navigator as Navigator & { wakeLock?: { request: (type: 'screen') => Promise<{ release: () => Promise<void>; released?: boolean }> } }).wakeLock
  if (!wakeLockApi || document.visibilityState !== 'visible') {
    operationalWarnings.value = [...new Set([...operationalWarnings.value, 'Wake Lock není dostupný; obrazovku během záznamu nezamykejte.'])]
    return
  }
  try {
    wakeLock = await wakeLockApi.request('screen')
  } catch {
    operationalWarnings.value = [...new Set([...operationalWarnings.value, 'Nepodařilo se zabránit uspání obrazovky.'])]
  }
}

async function releaseWakeLock(): Promise<void> {
  if (wakeLock && !wakeLock.released) await wakeLock.release().catch(() => undefined)
  wakeLock = null
}

function handleVisibilityChange(): void {
  if (!recording.value) return
  if (document.visibilityState === 'hidden') {
    operationalWarnings.value = [...new Set([...operationalWarnings.value, 'Aplikace byla během záznamu skryta; zkontrolujte výsledný soubor.'])]
  } else {
    void acquireWakeLock()
  }
}

function recordTelemetry(event: TelemetryEvent['event'], details?: Record<string, unknown>) {
  const monotonic = performance.now()
  const settings = mediaStream?.getVideoTracks()[0]?.getSettings() as (MediaTrackSettings & { zoom?: number }) | undefined
  const sample: TelemetryEvent = {
    schema_version: '1.0',
    event,
    monotonic_ms: monotonic,
    recording_offset_ms: recordingStartedAt === null ? null : monotonic - recordingStartedAt,
    utc_time: new Date().toISOString(),
    camera: {
      zoom_ratio: settings?.zoom ?? null,
      field_of_view_deg: null,
      width: settings?.width ?? null,
      height: settings?.height ?? null,
    },
    position: latestPosition ? { ...latestPosition } : null,
    orientation: latestOrientation ? { ...latestOrientation } : null,
    ...(details ? { details } : {}),
  }
  if (captureId) {
    const write = appendTelemetryEvent(captureId, telemetryIndex, sample).catch((reason) => {
      error.value = reason instanceof Error ? `Telemetrii nelze uložit: ${reason.message}` : 'Telemetrii nelze uložit.'
      throw reason
    })
    telemetryIndex += 1
    pendingTelemetryWrites.push(write)
  }
}

async function prepareSensors() {
  const orientationEvent = window.DeviceOrientationEvent as typeof DeviceOrientationEvent & {
    requestPermission?: () => Promise<'granted' | 'denied'>
  }
  try {
    const permission = await orientationEvent.requestPermission?.()
    if (permission === 'denied') throw new Error('Orientace nebyla povolena.')
    window.addEventListener('deviceorientation', handleOrientation)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Orientaci zařízení nelze načíst.'
  }
  if (navigator.geolocation && geolocationWatchId === undefined) {
    geolocationWatchId = navigator.geolocation.watchPosition(
      (position) => {
        const { coords } = position
        latestPosition = {
          latitude: coords.latitude,
          longitude: coords.longitude,
          accuracy_m: coords.accuracy,
          altitude_m: coords.altitude,
          altitude_accuracy_m: coords.altitudeAccuracy,
          heading_deg: coords.heading,
          speed_m_s: coords.speed,
          timestamp_ms: position.timestamp,
        }
      },
      () => { /* Chybějící GNSS vzorek je v telemetrii reprezentovaný hodnotou null. */ },
      { enableHighAccuracy: true, maximumAge: 1000, timeout: 10000 },
    )
  }
}

function handleOrientation(event: DeviceOrientationEvent) {
  latestOrientation = {
    alpha_deg: event.alpha,
    beta_deg: event.beta,
    gamma_deg: event.gamma,
    absolute: event.absolute,
  }
}

async function startLocalRecording(requestDetails: Record<string, unknown> = {}): Promise<boolean> {
  if (!mediaStream || recording.value || recordingStarting.value || recordingFinalizing.value || !session.value || !deviceId.value || role.value === 'director') return false
  recordingStarting.value = true
  try {
    const cameraRole = role.value as Exclude<Role, 'director'>
    const preferredTypes = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4']
    const mimeType = preferredTypes.find((type) => MediaRecorder.isTypeSupported(type))
    captureId = crypto.randomUUID()
    const currentCaptureId = captureId
    chunkIndex = 0
    telemetryIndex = 0
    pendingChunkWrites = []
    pendingTelemetryWrites = []
    recordingStartedAt = null
    if (recordingUrl.value) URL.revokeObjectURL(recordingUrl.value)
    recordingUrl.value = ''
    mediaRecorder = new MediaRecorder(mediaStream, mimeType ? { mimeType } : undefined)
    const settings = mediaStream.getVideoTracks()[0]?.getSettings() as (MediaTrackSettings & { zoom?: number }) | undefined
    const localCapture: LocalCapture = {
      capture_id: captureId,
      take_id: typeof requestDetails.take_id === 'string' ? requestDetails.take_id : undefined,
      session_id: session.value.session_id,
      device_id: deviceId.value,
      role: cameraRole,
      mime_type: mediaRecorder.mimeType || mimeType || 'video/webm',
      state: 'recording',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      chunk_count: 0,
      size_bytes: 0,
      stream_settings: {
        width: settings?.width ?? null,
        height: settings?.height ?? null,
        frame_rate: settings?.frameRate ?? null,
        zoom_ratio: settings?.zoom ?? null,
        mime_type: mediaRecorder.mimeType,
        app_version: '0.1.0',
      },
    }
    await createLocalCapture(localCapture)
    await refreshLocalCaptures()
    recordTelemetry('recording_requested', requestDetails)
    mediaRecorder.ondataavailable = (event) => {
      if (!event.data.size) return
      const write = appendRecordingChunk(currentCaptureId, chunkIndex, event.data).catch((reason) => {
        error.value = reason instanceof Error ? `Blok záznamu nelze uložit: ${reason.message}` : 'Blok záznamu nelze uložit.'
        throw reason
      })
      chunkIndex += 1
      pendingChunkWrites.push(write)
    }
    mediaRecorder.onstop = async () => {
      try {
        await Promise.all([...pendingChunkWrites, ...pendingTelemetryWrites])
        await setLocalCaptureState(currentCaptureId, 'stored')
        const stored = (await listLocalCaptures()).find((capture) => capture.capture_id === currentCaptureId)
        if (stored) await uploadStoredCapture(stored)
      } catch (reason) {
        error.value = reason instanceof Error ? reason.message : 'Dokončení lokálního záznamu selhalo.'
        await refreshLocalCaptures()
      } finally {
        recordingFinalizing.value = false
      }
    }
    mediaRecorder.start(1000)
    recordingStartedAt = performance.now()
    recordTelemetry('recording_started')
    telemetryTimer = window.setInterval(() => recordTelemetry('clock_sample', deviceId.value ? clockMetrics.value[deviceId.value] : undefined), 1000)
    recording.value = true
    await acquireWakeLock()
    return true
  } catch {
    error.value = 'Záznam se na tomto zařízení nepodařilo spustit.'
    return false
  } finally {
    recordingStarting.value = false
  }
}

function stopLocalRecording(): boolean {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return false
  recordingFinalizing.value = true
  recordTelemetry('recording_stopped')
  window.clearInterval(telemetryTimer)
  window.removeEventListener('deviceorientation', handleOrientation)
  if (geolocationWatchId !== undefined) navigator.geolocation.clearWatch(geolocationWatchId)
  mediaRecorder.stop()
  recording.value = false
  void releaseWakeLock()
  return true
}

async function refreshLocalCaptures() {
  try {
    localCaptures.value = await listLocalCaptures()
  } catch (reason) {
    error.value = reason instanceof Error ? `Lokální záznamy nelze načíst: ${reason.message}` : 'Lokální záznamy nelze načíst.'
  }
}

async function uploadStoredCapture(capture: LocalCapture) {
  if (!session.value || !deviceId.value || uploadingCaptureId.value) return
  uploadingCaptureId.value = capture.capture_id
  uploadProgress.value = 0
  uploadVerified.value = false
  await setLocalCaptureState(capture.capture_id, 'uploading')
  await refreshLocalCaptures()
  try {
    const { recording: localRecording, telemetry } = await readLocalArtifacts(capture.capture_id, capture.mime_type)
    if (!localRecording.size || !telemetry.size) throw new Error('Lokální záznam nebo telemetrie jsou prázdné.')
    if (recordingUrl.value) URL.revokeObjectURL(recordingUrl.value)
    recordingUrl.value = URL.createObjectURL(localRecording)
    let videoProgress = 0
    let telemetryProgress = 0
    const updateProgress = () => {
      uploadProgress.value = Math.round(
        (videoProgress * localRecording.size + telemetryProgress * telemetry.size) / (localRecording.size + telemetry.size),
      )
    }
    await Promise.all([
      uploadArtifact(session.value.session_id, deviceId.value, capture.capture_id, 'recording', localRecording, (progress) => {
        videoProgress = progress
        updateProgress()
      }, capture.take_id),
      uploadArtifact(session.value.session_id, deviceId.value, capture.capture_id, 'telemetry', telemetry, (progress) => {
        telemetryProgress = progress
        updateProgress()
      }, capture.take_id),
    ])
    await setLocalCaptureState(capture.capture_id, 'verified')
    uploadVerified.value = true
  } catch (reason) {
    await setLocalCaptureState(capture.capture_id, 'stored')
    error.value = reason instanceof Error ? `Přenos selhal: ${reason.message}` : 'Přenos záznamu selhal.'
  } finally {
    uploadingCaptureId.value = null
    await refreshLocalCaptures()
  }
}

async function confirmDeleteCapture(capture: LocalCapture) {
  if (capture.state !== 'verified') return
  if (!window.confirm('Server potvrdil převzetí. Opravdu smazat lokální kopii z telefonu?')) return
  await deleteLocalCapture(capture.capture_id)
  await refreshLocalCaptures()
}

async function flashClap() {
  window.clearTimeout(flashTimer)
  const videoTrack = mediaStream?.getVideoTracks()[0]
  const capabilities = videoTrack?.getCapabilities() as (MediaTrackCapabilities & { torch?: boolean }) | undefined
  if (videoTrack && capabilities?.torch) {
    try {
      await videoTrack.applyConstraints({ advanced: [{ torch: true } as MediaTrackConstraintSet] })
      window.clearTimeout(torchTimer)
      torchTimer = window.setTimeout(() => {
        void videoTrack.applyConstraints({ advanced: [{ torch: false } as MediaTrackConstraintSet] })
      }, 700)
      return
    } catch { /* Chrome může capability hlásit, ale změnu přesto odmítnout. */ }
  }
  flashVisible.value = true
  flashTimer = window.setTimeout(() => (flashVisible.value = false), 700)
}

function roleLabel(value: Role): string {
  if (value === 'director') return 'režisér'
  if (value === 'main_camera') return 'hlavní kamera'
  return value === 'top_camera' ? 'top-over kamera' : 'vedlejší kamera'
}

onMounted(() => document.addEventListener('visibilitychange', handleVisibilityChange))

onBeforeUnmount(() => {
  socket?.close()
  stopLocalRecording()
  mediaStream?.getTracks().forEach((track) => track.stop())
  if (recordingUrl.value) URL.revokeObjectURL(recordingUrl.value)
  window.clearTimeout(flashTimer)
  window.clearTimeout(torchTimer)
  const videoTrack = mediaStream?.getVideoTracks()[0]
  if (videoTrack) void videoTrack.applyConstraints({ advanced: [{ torch: false } as MediaTrackConstraintSet] }).catch(() => undefined)
  window.clearInterval(telemetryTimer)
  window.clearInterval(clockTimer)
  window.clearTimeout(commandTimeout)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  void releaseWakeLock()
})
</script>

<template>
  <main>
    <header><span class="eyebrow">lokální capture systém</span><h1>MultiCam</h1></header>

    <section v-if="!role" class="card role-picker">
      <h2>Jak chcete zařízení použít?</h2>
      <button @click="chooseRole('director')">Režisérský pult</button>
      <button class="secondary" @click="chooseRole('main_camera')">Hlavní kamera</button>
      <button class="secondary" @click="chooseRole('top_camera')">Top-over kamera</button>
      <button class="secondary" @click="chooseRole('secondary_camera')">Vedlejší kamera</button>
    </section>

    <ArchivePage v-else-if="role === 'director' && archiveOpen" @close="archiveOpen = false" />

    <section v-else-if="!session" class="card">
      <button class="back" @click="role = null">← změnit roli</button>
      <template v-if="role === 'director'">
        <HotspotPanel />
        <button class="archive-button secondary" @click="archiveOpen = true">Archiv všech záznamů</button>
        <div v-if="availableSessions.length" class="session-list">
          <h2>Relace</h2>
          <button v-for="item in availableSessions" :key="item.session_id" class="session-item" @click="selectSession(item)">
            <span>{{ item.name }}</span><small>{{ new Date(item.created_at).toLocaleString() }} · {{ Object.keys(item.devices).length }} kamer</small>
          </button>
        </div>
        <h2>Nová relace</h2>
        <label>Název <input v-model="sessionName" /></label>
        <button :disabled="busy" @click="startDirector">Založit relaci</button>
      </template>
      <template v-else>
        <h2>Připojit: {{ roleLabel(role) }}</h2>
        <label>Název zařízení <input v-model.trim="deviceName" /></label>
        <p class="muted">Aplikace automaticky použije právě aktivní relaci.</p>
        <div v-if="localCaptures.length" class="recovery-notice">
          <strong>Nalezené lokální záznamy: {{ localCaptures.length }}</strong>
          <small>Z toho {{ localCaptures.filter(capture => capture.state !== 'verified').length }} čeká na odeslání. Po připojení budou dostupné v seznamu kamery.</small>
        </div>
        <button :disabled="busy || !deviceName" @click="joinCamera">Připojit</button>
      </template>
    </section>

    <section v-else class="card">
      <div class="session-heading"><div><span class="eyebrow">{{ roleLabel(role) }}</span><h2>{{ session.name }}</h2></div><span class="status">{{ session.state }}</span></div>

      <template v-if="role === 'director'">
        <button class="back" @click="backToSessions">← seznam relací</button>
        <button class="archive-button secondary" @click="archiveOpen = true">Archiv všech záznamů</button>
        <HotspotPanel />
        <h3>Zařízení ({{ devices.length }})</h3>
        <div class="record-controls">
          <template v-if="session.state !== 'recording'">
            <button class="secondary" :disabled="!connectedDevices.length" @click="sendRecordingCommand('control.arm')">1. ARM · připravit kamery</button>
            <button :disabled="!allCamerasArmed" @click="sendRecordingCommand('recording.start')">2. ● Spustit záznam</button>
          </template>
          <button v-else class="stop" @click="sendRecordingCommand('recording.stop')">■ Zastavit záznam</button>
        </div>
        <p v-if="activeCommandType && connectedDevices.length" class="muted">Potvrzení povelu: {{ Object.values(controlAcks).filter(ack => !['pending', 'timeout', 'error'].includes(ack.status)).length }}/{{ connectedDevices.length }}</p>
        <button class="clap-button" :disabled="!devices.some(device => device.role === 'main_camera' && device.connected)" @click="triggerClap">Spustit světelnou klapku</button>
        <p v-if="!devices.length" class="muted">Čekám na připojení prvního telefonu…</p>
        <article v-for="device in devices" :key="device.device_id" class="device">
          <div><strong>{{ device.name }} · {{ roleLabel(device.role) }}</strong><small>Baterie: {{ device.capabilities.battery_percent ?? 'neznámá' }} % · Volno: {{ formatBytes(device.capabilities.free_storage_bytes) }}</small><small>Kamera: {{ permissionLabel(device.capabilities.camera_permission) }} · Mikrofon: {{ permissionLabel(device.capabilities.microphone_permission) }}</small><small v-if="clockMetrics[device.device_id]">Hodiny: offset {{ clockMetrics[device.device_id].offset_ms.toFixed(1) }} ms · RTT {{ clockMetrics[device.device_id].rtt_ms.toFixed(1) }} ms</small><small v-if="controlAcks[device.device_id]" :class="{ 'ack-error': ['error', 'timeout'].includes(controlAcks[device.device_id].status) }">{{ ackLabel(controlAcks[device.device_id]) }}</small></div>
          <span :class="['dot', { offline: !device.connected }]">{{ device.connected ? (device.state === 'uploading' ? `přenos ${deviceUploadProgress[device.device_id] ?? 0} %` : device.state) : 'odpojeno' }}</span>
        </article>
        <div class="media-heading"><h3>Záznamy ({{ sessionMedia.length }})</h3><div class="heading-actions"><button class="small secondary" @click="downloadSessionReport">Stáhnout report</button><button class="small" @click="loadSessionMedia">Obnovit</button></div></div>
        <p v-if="!sessionMedia.length" class="muted">Relace zatím nemá ověřené záznamy.</p>
        <div v-else class="take-list">
          <CaptureGroup v-for="group in captureGroups" :key="group.takeId" :captures="group.captures" />
        </div>
      </template>
      <template v-else>
        <div v-if="operationalWarnings.length" class="warning-list"><strong>Upozornění</strong><small v-for="warning in operationalWarnings" :key="warning">{{ warning }}</small></div>
        <video ref="preview" class="preview" autoplay muted playsinline></video>
        <div class="ready"><span>✓</span><div><strong>Zařízení je připravené</strong><small>{{ deviceName }}</small></div></div>
        <div v-if="role === 'main_camera'" class="record-controls">
          <template v-if="session.state !== 'recording'">
            <button v-if="session.state !== 'armed'" class="secondary" :disabled="!cameraReady" @click="sendRecordingCommand('control.arm')">1. ARM · připravit kamery</button>
            <button v-else :disabled="!cameraReady || recordingStarting || recordingFinalizing" @click="sendRecordingCommand('recording.start')">2. ● Spustit záznam</button>
          </template>
          <button v-else class="stop" @click="sendRecordingCommand('recording.stop')">■ Zastavit záznam</button>
        </div>
        <p v-else-if="recording" class="recording-indicator">● Probíhá záznam</p>
        <button v-if="role === 'main_camera'" class="clap-button" @click="flashClap">Otestovat světelnou klapku</button>
        <div v-if="uploadProgress !== null" class="upload">
          <div><strong>{{ uploadVerified ? 'Přenos ověřen' : 'Přenáším záznam' }}</strong><span>{{ uploadProgress }} %</span></div>
          <progress :value="uploadProgress" max="100"></progress>
        </div>
        <a v-if="recordingUrl" class="download" :href="recordingUrl" :download="`${role}-${Date.now()}.webm`">Stáhnout poslední záznam</a>
        <section v-if="localCaptures.length" class="local-captures">
          <h3>Lokální záznamy</h3>
          <article v-for="capture in localCaptures" :key="capture.capture_id" class="local-capture">
            <div>
              <strong>{{ new Date(capture.created_at).toLocaleString() }}</strong>
              <small>{{ capture.state === 'recording' ? 'přerušený' : capture.state }} · {{ formatBytes(capture.size_bytes) }}</small>
              <small>{{ roleLabel(capture.role as Role) }} · relace {{ capture.session_id.slice(0, 8) }}</small>
            </div>
            <button v-if="capture.state !== 'verified'" class="small" :disabled="uploadingCaptureId !== null" @click="uploadStoredCapture(capture)">{{ uploadingCaptureId === capture.capture_id ? 'Odesílám…' : 'Odeslat' }}</button>
            <button v-if="capture.state === 'verified'" class="small danger" @click="confirmDeleteCapture(capture)">Smazat z telefonu</button>
          </article>
        </section>
      </template>
    </section>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="flashVisible" class="flash" aria-label="Světelná klapka"></div>
  </main>
</template>
