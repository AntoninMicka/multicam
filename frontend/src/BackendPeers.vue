<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { createPairingOffer, getBackends, getFederationConfig, getFederationTransfers, joinPairingOffer, pingBackends, setFederationBackup, setFederationTransfer, type BackendInfo, type BackendPingResult } from './api'

const emit = defineEmits<{ (event: 'join-session', sessionId: string): void }>()

const peers = ref<BackendInfo[]>([])
const own = ref<BackendInfo | null>(null)
const error = ref('')
const federationEnabled = ref(false)
const transferEnabled = ref(false)
const pairingCode = ref('')
const pairingInput = ref('')
const pairingMessage = ref('')
const lastSyncAt = ref<string | null>(null)
const syncError = ref<string | null>(null)
const federationRole = ref<'standalone' | 'leader' | 'follower'>('standalone')
const backupToFollower = ref(false)
const pendingTransfers = ref(0)
const discoveryDetail = ref('')
const pingResults = ref<BackendPingResult[]>([])
const pingBusy = ref(false)
let timer = 0

async function refresh() {
  try {
    const result = await getBackends()
    own.value = result.self
    peers.value = result.peers
    federationEnabled.value = result.federation_enabled
    transferEnabled.value = result.transfer_enabled
    const diagnostic = result.discovery_diagnostics
    discoveryDetail.value = !result.peers.length && diagnostic
      ? diagnostic.last_rejection
        ? `Discovery odmítlo paket z ${diagnostic.last_source ?? '?'}: ${diagnostic.last_rejection}. Vlastní ID: ${diagnostic.backend_id}`
        : diagnostic.received_packets
          ? `Poslední paket z ${diagnostic.last_source ?? '?'} před ${diagnostic.last_packet_seconds_ago ?? '?'} s; čekám na platný heartbeat.`
          : `Nepřišel žádný heartbeat. Poslouchám na: ${diagnostic.listening_interface_ips.join(', ') || 'výchozí rozhraní'}.`
      : ''
    const config = await getFederationConfig()
    lastSyncAt.value = config.last_sync_at
    syncError.value = config.last_error
    federationRole.value = config.role
    backupToFollower.value = config.backup_to_follower
    pendingTransfers.value = (await getFederationTransfers()).pending_count
    error.value = ''
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Discovery backendů není dostupné.'
  }
}

async function runApplicationPing() {
  pingBusy.value = true
  try {
    pingResults.value = (await pingBackends()).results
    if (!pingResults.value.length) error.value = 'Aplikační ping nemá cílovou IP; zatím nedorazil žádný heartbeat.'
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Aplikační ping selhal.'
  } finally {
    pingBusy.value = false
  }
}

async function createOffer() {
  try {
    const offer = await createPairingOffer()
    pairingCode.value = offer.pairing_code
    pairingMessage.value = 'Kód platí 5 minut a lze jej použít jen jednou.'
    await refresh()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Párovací QR nelze vytvořit.'
  }
}

async function joinOffer(value = pairingInput.value) {
  try {
    await joinPairingOffer(value.trim())
    pairingMessage.value = 'Pulty jsou spárované. Discovery je během několika sekund propojí.'
    pairingInput.value = ''
    await refresh()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Párování selhalo.'
  }
}

async function toggleTransfer() {
  try {
    const result = await setFederationTransfer(!transferEnabled.value)
    transferEnabled.value = result.transfer_enabled
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Nastavení přenosu nelze uložit.'
  }
}

async function toggleBackup() {
  try {
    const result = await setFederationBackup(!backupToFollower.value)
    backupToFollower.value = result.backup_to_follower
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Nastavení zálohy nelze uložit.'
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
    <div><strong>Backend: {{ own?.name ?? 'načítám…' }}</strong><small v-if="own">{{ own.url }}</small><small v-if="federationEnabled">Federace {{ federationRole }} · páteřní přenos {{ transferEnabled ? 'povolený' : 'odložený' }} · ve frontě {{ pendingTransfers }}</small><small v-else>Jen discovery · federace není nakonfigurovaná</small><small v-if="lastSyncAt">Poslední synchronizace: {{ new Date(lastSyncAt).toLocaleTimeString() }}</small><small v-if="syncError" class="sync-error">Synchronizace selhala: {{ syncError }}</small></div>
    <span v-if="error" class="muted">{{ error }}</span>
    <span v-else-if="!peers.length" class="muted">{{ discoveryDetail || 'Další pult nenalezen' }}</span>
    <button class="small secondary" :disabled="pingBusy" @click="runApplicationPing">{{ pingBusy ? 'Testuji…' : 'Aplikační ping' }}</button>
    <small v-for="result in pingResults" :key="result.url" :class="result.ok ? 'ping-ok' : 'sync-error'">{{ result.url }} · {{ result.ok ? `${result.latency_ms} ms` : result.detail }}</small>
    <a v-for="peer in peers" :key="peer.backend_id" :href="peer.url" target="_blank" rel="noopener">
      {{ peer.name }} · {{ (peer.last_seen_seconds_ago ?? 0).toFixed(1) }} s
    </a>
    <button v-for="peer in peers.filter(item => item.active_session)" :key="`join-${peer.backend_id}`" class="small" @click="emit('join-session', peer.active_session!.session_id)">
      Připojit k „{{ peer.active_session!.name }}“ na {{ peer.name }}
    </button>
    <details class="pairing">
      <summary>Spárovat pulty / nastavení federace</summary>
      <div class="pairing-actions">
        <button class="small" @click="createOffer">Vytvořit krátký kód</button>
        <button v-if="federationEnabled" class="small secondary" @click="toggleTransfer">{{ transferEnabled ? 'Odložit páteřní přenosy' : `Spustit odložené přenosy (${pendingTransfers})` }}</button>
        <button v-if="federationRole === 'leader'" class="small secondary" @click="toggleBackup">{{ backupToFollower ? 'Vypnout zálohu na follower' : 'Zapnout zálohu na follower' }}</button>
      </div>
      <p v-if="pairingCode" class="pairing-code"><small>Párovací kód</small><strong>{{ pairingCode.slice(0, 5) }}-{{ pairingCode.slice(5) }}</strong></p>
      <label>Krátký párovací kód <input v-model="pairingInput" maxlength="11" placeholder="ABCDE-FG234"></label>
      <button class="small" :disabled="!pairingInput.trim()" @click="joinOffer()">Spárovat s prvním pultem</button>
      <small v-if="pairingMessage">{{ pairingMessage }}</small>
    </details>
  </div>
</template>

<style scoped>
.backend-peers { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 12px 0; }
.backend-peers div { display: flex; flex-direction: column; margin-right: auto; }
.backend-peers a { padding: 8px 12px; border: 1px solid #456; border-radius: 8px; }
.pairing { flex-basis: 100%; padding-top: 8px; }
.pairing summary { cursor: pointer; font-weight: 700; }
.pairing-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.pairing-code { display: inline-flex; flex-direction: column; gap: 3px; margin: 8px 0 14px; padding: 10px 16px; border: 1px solid #456; border-radius: 9px; }
.pairing-code strong { font-size: 1.45rem; letter-spacing: .12em; }
.pairing label, .pairing input { display: block; width: 100%; }
.pairing input { margin: 6px 0 10px; }
.sync-error { color: #fca5a5; }
.ping-ok { color: #86efac; }
</style>
