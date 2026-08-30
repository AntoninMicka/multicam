<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getTelemetry, type CaptureMedia, type TelemetrySample } from './api'

const props = withDefaults(defineProps<{ capture: CaptureMedia; muted?: boolean }>(), { muted: false })
const samples = ref<TelemetrySample[]>([])
const video = ref<HTMLVideoElement | null>(null)
const currentTimeMs = ref(0)
const loadingError = ref('')
const mediaStatus = ref<'loading' | 'ready' | 'playing' | 'paused' | 'ended' | 'error'>('loading')
const mediaError = ref('')
const alignedToMarker = ref(false)

onMounted(async () => {
  if (!props.capture.telemetry_url) {
    loadingError.value = 'Tato starší relace nemá telemetrii.'
    return
  }
  try {
    samples.value = (await getTelemetry(props.capture.telemetry_url))
      .filter((sample) => sample.recording_offset_ms !== null)
      .sort((left, right) => (left.recording_offset_ms ?? 0) - (right.recording_offset_ms ?? 0))
  } catch {
    loadingError.value = 'Telemetrii nelze načíst.'
  }
})

const current = computed<TelemetrySample | null>(() => {
  if (!samples.value.length) return null
  let nearest = samples.value[0]
  let distance = Math.abs((nearest.recording_offset_ms ?? 0) - currentTimeMs.value)
  for (const sample of samples.value) {
    const candidate = Math.abs((sample.recording_offset_ms ?? 0) - currentTimeMs.value)
    if (candidate > distance) break
    nearest = sample
    distance = candidate
  }
  return nearest
})
const syncOffsetSeconds = computed(() => {
  const marker = samples.value.find((sample) => sample.event === 'sync_marker' && sample.recording_offset_ms !== null)
  return (marker?.recording_offset_ms ?? 0) / 1000
})

function number(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

async function playFromStart(): Promise<void> {
  if (!video.value) return
  video.value.playbackRate = 1
  alignedToMarker.value = canSeekTo(syncOffsetSeconds.value)
  if (alignedToMarker.value) video.value.currentTime = syncOffsetSeconds.value
  await video.value.play()
}

function pause(): void {
  video.value?.pause()
}

function canSeekTo(time: number): boolean {
  if (!video.value || !Number.isFinite(video.value.duration)) return false
  for (let index = 0; index < video.value.seekable.length; index += 1) {
    if (time >= video.value.seekable.start(index) && time <= video.value.seekable.end(index)) return true
  }
  return false
}

function logicalTime(): number { return (video.value?.currentTime ?? 0) - (alignedToMarker.value ? syncOffsetSeconds.value : 0) }
function synchronizeTo(logicalMasterTime: number): void {
  if (!video.value || !Number.isFinite(logicalMasterTime)) return
  if (video.value.paused || video.value.seeking || video.value.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return
  const target = logicalMasterTime + (alignedToMarker.value ? syncOffsetSeconds.value : 0)
  const drift = video.value.currentTime - target
  if (Math.abs(drift) > 0.35 && canSeekTo(target)) {
    video.value.currentTime = target
    return
  }
  video.value.playbackRate = Math.abs(drift) > 0.06 ? (drift > 0 ? 0.97 : 1.03) : 1
}

function describeMediaError(): void {
  const code = video.value?.error?.code
  mediaStatus.value = 'error'
  mediaError.value = ({ 1: 'načítání přerušeno', 2: 'síťová chyba', 3: 'chyba dekódování', 4: 'nepodporovaný formát' } as Record<number, string>)[code ?? 0] ?? 'video nelze přehrát'
}

defineExpose({ playFromStart, pause, logicalTime, synchronizeTo })
</script>

<template>
  <article class="capture-player">
    <header>
      <div><strong>{{ capture.device_name }}</strong><small>{{ capture.role }}</small></div>
      <small>{{ (capture.size_bytes / 1024 / 1024).toFixed(1) }} MB</small>
    </header>
    <video ref="video" :src="capture.video_url" :muted="muted" controls playsinline preload="auto"
      @loadedmetadata="mediaStatus = 'ready'" @playing="mediaStatus = 'playing'" @pause="mediaStatus = 'paused'"
      @ended="mediaStatus = 'ended'" @error="describeMediaError"
      @timeupdate="currentTimeMs = ($event.target as HTMLVideoElement).currentTime * 1000"></video>
    <p :class="['media-state', mediaStatus]">{{ mediaStatus }}<span v-if="mediaError"> · {{ mediaError }}</span></p>
    <p v-if="loadingError" class="telemetry-error">{{ loadingError }}</p>
    <dl v-else class="telemetry-values">
      <div><dt>čas</dt><dd>{{ (currentTimeMs / 1000).toFixed(2) }} s</dd></div>
      <div><dt>zoom</dt><dd>{{ number(current?.camera?.zoom_ratio) }}×</dd></div>
      <div><dt>lat</dt><dd>{{ number(current?.position?.latitude, 6) }}</dd></div>
      <div><dt>lon</dt><dd>{{ number(current?.position?.longitude, 6) }}</dd></div>
      <div><dt>přesnost</dt><dd>{{ number(current?.position?.accuracy_m, 1) }} m</dd></div>
      <div><dt>α</dt><dd>{{ number(current?.orientation?.alpha_deg, 1) }}°</dd></div>
      <div><dt>β</dt><dd>{{ number(current?.orientation?.beta_deg, 1) }}°</dd></div>
      <div><dt>γ</dt><dd>{{ number(current?.orientation?.gamma_deg, 1) }}°</dd></div>
    </dl>
  </article>
</template>

<style scoped>
.capture-player { min-width: 0; overflow: hidden; border: 1px solid #2c3c59; border-radius: 16px; background: #0b1322; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px; margin: 0; }
header div { display: grid; gap: 3px; }
small { color: #8391a7; }
video { display: block; width: 100%; aspect-ratio: 16 / 9; background: #000; }
.telemetry-values { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin: 0; background: #283750; }
.telemetry-values div { min-width: 0; padding: 9px; background: #101827; }
dt { color: #8391a7; font-size: .65rem; text-transform: uppercase; }
dd { margin: 3px 0 0; overflow: hidden; font-size: .78rem; font-variant-numeric: tabular-nums; text-overflow: ellipsis; }
.telemetry-error { padding: 12px; color: #fca5a5; }
.media-state { margin: 0; padding: 6px 10px; color: #8391a7; font-size: .7rem; }
.media-state.error { color: #fca5a5; }
@media (max-width: 520px) { .telemetry-values { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
