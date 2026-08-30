<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  deleteServerCapture,
  deleteServerTake,
  listSessionMedia,
  listSessions,
  type CaptureMedia,
} from './api'
import CaptureGroup from './CaptureGroup.vue'

defineEmits<{ close: [] }>()

interface ArchiveCapture extends CaptureMedia {
  session_id: string
  session_name: string
}

const captures = ref<ArchiveCapture[]>([])
const loading = ref(false)
const deleting = ref(false)
const error = ref('')

const groups = computed(() => {
  const result = new Map<string, { sessionId: string; sessionName: string; takeId: string; captures: ArchiveCapture[] }>()
  for (const capture of captures.value) {
    const takeId = capture.take_id ?? capture.capture_id
    const key = `${capture.session_id}:${takeId}`
    const group = result.get(key) ?? { sessionId: capture.session_id, sessionName: capture.session_name, takeId, captures: [] }
    group.captures.push(capture)
    result.set(key, group)
  }
  return [...result.values()].sort((left, right) =>
    (right.captures[0]?.created_at ?? '').localeCompare(left.captures[0]?.created_at ?? ''),
  )
})

async function loadArchive(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const sessions = await listSessions()
    const media = await Promise.all(sessions.map(async (session) =>
      (await listSessionMedia(session.session_id)).map((capture) => ({
        ...capture,
        session_id: session.session_id,
        session_name: session.name,
      })),
    ))
    captures.value = media.flat()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Archiv nelze načíst.'
  } finally {
    loading.value = false
  }
}

async function removeCapture(capture: ArchiveCapture): Promise<void> {
  if (!window.confirm(`Nenávratně smazat záběr kamery „${capture.device_name}“ včetně telemetrie?`)) return
  deleting.value = true
  try {
    await deleteServerCapture(capture.session_id, capture.device_id, capture.capture_id)
    captures.value = captures.value.filter((item) => item.capture_id !== capture.capture_id)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Záběr nelze smazat.'
  } finally {
    deleting.value = false
  }
}

async function removeGroup(group: (typeof groups.value)[number]): Promise<void> {
  if (!window.confirm(`Nenávratně smazat celou klapku (${group.captures.length} záznamů) včetně telemetrie?`)) return
  deleting.value = true
  try {
    await deleteServerTake(group.sessionId, group.takeId)
    const deleted = new Set(group.captures.map((capture) => capture.capture_id))
    captures.value = captures.value.filter((capture) => !deleted.has(capture.capture_id))
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Skupinu nelze smazat.'
  } finally {
    deleting.value = false
  }
}

onMounted(loadArchive)
</script>

<template>
  <section class="card archive-page">
    <div class="archive-nav">
      <button class="back" @click="$emit('close')">← režisérský pult</button>
      <button class="small" :disabled="loading || deleting" @click="loadArchive">Obnovit</button>
    </div>
    <span class="eyebrow">všechny relace</span>
    <h2>Archiv záznamů</h2>
    <p class="muted">{{ captures.length }} záznamů v {{ groups.length }} skupinách</p>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="muted">Načítám archiv…</p>
    <p v-else-if="!groups.length" class="muted">Archiv zatím neobsahuje žádné záznamy.</p>
    <div v-else class="archive-groups">
      <section v-for="group in groups" :key="`${group.sessionId}:${group.takeId}`" class="archive-group">
        <header>
          <div><strong>{{ group.sessionName }}</strong><small>Klapka {{ group.captures[0]?.created_at ? new Date(group.captures[0].created_at).toLocaleString() : group.takeId.slice(0, 8) }}</small></div>
          <button class="small danger" :disabled="deleting" @click="removeGroup(group)">Smazat celou klapku</button>
        </header>
        <CaptureGroup :captures="group.captures" />
        <div class="capture-actions">
          <button v-for="capture in group.captures" :key="capture.capture_id" class="small danger" :disabled="deleting" @click="removeCapture(capture)">
            Smazat: {{ capture.device_name }}
          </button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.archive-page { width: 100%; }
.archive-nav, .archive-group > header { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.archive-nav .back { margin: 0; }
.archive-groups { display: grid; gap: 22px; margin-top: 22px; }
.archive-group { padding: 16px; border: 1px solid #405170; border-radius: 18px; background: #111b2d; }
.archive-group > header { margin-bottom: 14px; }
.archive-group > header div { display: grid; gap: 4px; }
.capture-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
small { color: #8391a7; }
@media (max-width: 520px) { .archive-group > header { align-items: stretch; flex-direction: column; } }
</style>
