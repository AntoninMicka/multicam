<script setup lang="ts">
import { ref } from 'vue'
const props = defineProps<{ sessionId: string; recording: boolean }>()
const emit = defineEmits<{ added: [] }>()
const name = ref('IP kamera')
const url = ref('')
const busy = ref(false)
const error = ref('')
async function add() {
  busy.value = true
  error.value = ''
  try {
    const response = await fetch(`/api/sessions/${props.sessionId}/ip-cameras`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.value, url: url.value, role: 'secondary_camera' }),
    })
    if (!response.ok) throw new Error((await response.json()).detail || 'Kameru nelze přidat.')
    url.value = ''
    emit('added')
  } catch (reason) { error.value = String(reason) }
  finally { busy.value = false }
}
</script>
<template>
  <details>
    <summary>Přidat IP kameru na tento backend</summary>
    <label>Název <input v-model="name" /></label>
    <label>RTSP nebo HTTP(S) adresa streamu <input v-model="url" type="password" autocomplete="off" placeholder="rtsp://kamera/stream" /></label>
    <p class="muted">Video zaznamenává tento backend. Při ARM ověří dostupnost kamery; start a stop jsou společné s telefony.</p>
    <button class="small" :disabled="busy || recording || !url || !name" @click="add">{{ busy ? 'Přidávám…' : 'Přidat IP kameru' }}</button>
    <p v-if="error" class="error">{{ error }}</p>
  </details>
</template>
