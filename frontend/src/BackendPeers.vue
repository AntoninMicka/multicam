<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { getBackends, type BackendInfo } from './api'

const peers = ref<BackendInfo[]>([])
const own = ref<BackendInfo | null>(null)
const error = ref('')
let timer = 0

async function refresh() {
  try {
    const result = await getBackends()
    own.value = result.self
    peers.value = result.peers
    error.value = ''
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Discovery backendů není dostupné.'
  }
}

onMounted(() => {
  void refresh()
  timer = window.setInterval(refresh, 5000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <div class="backend-peers">
    <div><strong>Backend: {{ own?.name ?? 'načítám…' }}</strong><small v-if="own">{{ own.url }}</small></div>
    <span v-if="error" class="muted">{{ error }}</span>
    <span v-else-if="!peers.length" class="muted">Další pult nenalezen</span>
    <a v-for="peer in peers" :key="peer.backend_id" :href="peer.url" target="_blank" rel="noopener">
      {{ peer.name }} · {{ (peer.last_seen_seconds_ago ?? 0).toFixed(1) }} s
    </a>
  </div>
</template>

<style scoped>
.backend-peers { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 12px 0; }
.backend-peers div { display: flex; flex-direction: column; margin-right: auto; }
.backend-peers a { padding: 8px 12px; border: 1px solid #456; border-radius: 8px; }
</style>
