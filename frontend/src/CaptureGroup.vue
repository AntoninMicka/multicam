<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import type { CaptureMedia } from './api'
import CapturePlayer from './CapturePlayer.vue'

const props = defineProps<{ captures: CaptureMedia[]; sessionId?: string }>()
const players = ref<Array<InstanceType<typeof CapturePlayer>>>([])
const playing = ref(false)
const playbackError = ref('')
let syncTimer: number | undefined

function setPlayer(player: unknown, index: number): void {
  if (player) players.value[index] = player as InstanceType<typeof CapturePlayer>
}

async function togglePlayback(): Promise<void> {
  playbackError.value = ''
  if (playing.value) {
    window.clearTimeout(syncTimer)
    players.value.forEach((player) => player.pause())
    playing.value = false
    return
  }
  const results = await Promise.allSettled(players.value.map((player) => player.playFromStart()))
  const failed = results.filter((result) => result.status === 'rejected')
  playing.value = failed.length < results.length
  if (failed.length) {
    const firstReason = failed[0].status === 'rejected' ? failed[0].reason : null
    const detail = firstReason instanceof DOMException ? firstReason.message : ''
    playbackError.value = `${failed.length} z ${results.length} videí se nepodařilo spustit${detail ? `: ${detail}` : '.'}`
  }
  if (playing.value) synchronizePlayers()
}

function synchronizePlayers(): void {
  if (!playing.value || !players.value.length) return
  const masterIndex = Math.max(0, props.captures.findIndex((capture) => capture.role === 'main_camera'))
  const masterTime = players.value[masterIndex]?.logicalTime() ?? players.value[0]?.logicalTime() ?? 0
  players.value.forEach((player, index) => { if (index !== masterIndex) player.synchronizeTo(masterTime) })
  syncTimer = window.setTimeout(synchronizePlayers, 250)
}

onBeforeUnmount(() => window.clearTimeout(syncTimer))

function formattedTime(): string {
  const createdAt = props.captures.find((capture) => capture.created_at)?.created_at
  return createdAt ? new Date(createdAt).toLocaleString() : 'čas není známý'
}

function downloadTake(): void {
  const takeId = props.captures[0]?.take_id ?? props.captures[0]?.capture_id
  if (props.sessionId && takeId) window.location.assign(`/api/sessions/${props.sessionId}/takes/${takeId}/bundle`)
}
</script>

<template>
  <section class="capture-group">
    <header>
      <div>
        <strong>Klapka · {{ formattedTime() }}</strong>
        <small>{{ captures.length }} {{ captures.length === 1 ? 'kamera' : 'kamery' }}</small>
      </div>
      <div class="group-actions">
        <button v-if="sessionId" class="small secondary" @click="downloadTake">Stáhnout klapku</button>
        <button class="small" @click="togglePlayback">{{ playing ? '❚❚ Pozastavit vše' : '▶ Přehrát vše' }}</button>
      </div>
    </header>
    <p v-if="playbackError" class="error">{{ playbackError }}</p>
    <div class="stream-matrix">
      <CapturePlayer
        v-for="(capture, index) in captures"
        :key="capture.capture_id"
        :ref="(player) => setPlayer(player, index)"
        :capture="capture"
        :muted="capture.role !== 'main_camera'"
      />
    </div>
  </section>
</template>

<style scoped>
.capture-group { padding: 16px; border: 1px solid #405170; border-radius: 18px; background: #111b2d; }
header { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
header div { display: grid; gap: 4px; }
.group-actions { display: flex; grid-auto-flow: column; gap: 8px; }
small { color: #8391a7; }
.stream-matrix { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr)); gap: 14px; }
.error { margin-bottom: 12px; }
@media (max-width: 520px) { header { align-items: stretch; flex-direction: column; } }
</style>
