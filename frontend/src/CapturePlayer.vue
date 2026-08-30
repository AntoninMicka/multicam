<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getTelemetry, type CaptureMedia, type TelemetrySample } from './api'

const props = withDefaults(defineProps<{ capture: CaptureMedia; muted?: boolean }>(), { muted: false })
const samples = ref<TelemetrySample[]>([])
const video = ref<HTMLVideoElement | null>(null)
const currentTimeMs = ref(0)
const loadingError = ref('')

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

function number(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

async function playFromStart(): Promise<void> {
  if (!video.value) return
  video.value.currentTime = 0
  await video.value.play()
}

function pause(): void {
  video.value?.pause()
}

defineExpose({ playFromStart, pause })
</script>

<template>
  <article class="capture-player">
    <header>
      <div><strong>{{ capture.device_name }}</strong><small>{{ capture.role }}</small></div>
      <small>{{ (capture.size_bytes / 1024 / 1024).toFixed(1) }} MB</small>
    </header>
    <video ref="video" :src="capture.video_url" :muted="muted" controls playsinline preload="auto" @timeupdate="currentTimeMs = ($event.target as HTMLVideoElement).currentTime * 1000"></video>
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
@media (max-width: 520px) { .telemetry-values { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
