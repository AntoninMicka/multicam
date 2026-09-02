<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import QRCode from 'qrcode'
import { createPairingOffer, getBackends, joinPairingOffer, setFederationTransfer, type BackendInfo } from './api'

const emit = defineEmits<{ (event: 'join-session', sessionId: string): void }>()

const peers = ref<BackendInfo[]>([])
const own = ref<BackendInfo | null>(null)
const error = ref('')
const federationEnabled = ref(false)
const transferEnabled = ref(false)
const pairingQr = ref('')
const pairingUri = ref('')
const pairingCode = ref('')
const pairingInput = ref('')
const pairingMessage = ref('')
const scanInput = ref<HTMLInputElement | null>(null)
let timer = 0

async function refresh() {
  try {
    const result = await getBackends()
    own.value = result.self
    peers.value = result.peers
    federationEnabled.value = result.federation_enabled
    transferEnabled.value = result.transfer_enabled
    error.value = ''
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Discovery backendů není dostupné.'
  }
}

async function createOffer() {
  try {
    const offer = await createPairingOffer()
    pairingUri.value = offer.pairing_uri
    pairingCode.value = offer.pairing_code
    pairingQr.value = await QRCode.toDataURL(offer.pairing_uri, { margin: 1, width: 280 })
    pairingMessage.value = 'QR platí 5 minut a lze jej použít jen jednou.'
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

async function scanImage(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  const Detector = (window as typeof window & { BarcodeDetector?: new (options: { formats: string[] }) => { detect(source: ImageBitmap): Promise<Array<{ rawValue: string }>> } }).BarcodeDetector
  if (!file || !Detector) {
    error.value = 'Tento prohlížeč neumí číst QR z obrázku; vložte párovací kód ručně.'
    return
  }
  const bitmap = await createImageBitmap(file)
  const codes = await new Detector({ formats: ['qr_code'] }).detect(bitmap)
  bitmap.close()
  if (!codes[0]?.rawValue) {
    error.value = 'V obrázku nebyl nalezen QR kód.'
    return
  }
  pairingInput.value = codes[0].rawValue
  await joinOffer(codes[0].rawValue)
}

async function toggleTransfer() {
  try {
    const result = await setFederationTransfer(!transferEnabled.value)
    transferEnabled.value = result.transfer_enabled
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Nastavení přenosu nelze uložit.'
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
    <div><strong>Backend: {{ own?.name ?? 'načítám…' }}</strong><small v-if="own">{{ own.url }}</small><small v-if="federationEnabled">Federace aktivní · přenos záznamů {{ transferEnabled ? 'zapnutý' : 'potlačený' }}</small><small v-else>Jen discovery · federace není nakonfigurovaná</small></div>
    <span v-if="error" class="muted">{{ error }}</span>
    <span v-else-if="!peers.length" class="muted">Další pult nenalezen</span>
    <a v-for="peer in peers" :key="peer.backend_id" :href="peer.url" target="_blank" rel="noopener">
      {{ peer.name }} · {{ (peer.last_seen_seconds_ago ?? 0).toFixed(1) }} s
    </a>
    <button v-for="peer in peers.filter(item => item.active_session)" :key="`join-${peer.backend_id}`" class="small" @click="emit('join-session', peer.active_session!.session_id)">
      Připojit k „{{ peer.active_session!.name }}“ na {{ peer.name }}
    </button>
    <details class="pairing">
      <summary>Spárovat pulty / nastavení federace</summary>
      <div class="pairing-actions">
        <button class="small" @click="createOffer">Vytvořit párovací QR</button>
        <button class="small secondary" @click="scanInput?.click()">Načíst QR z obrázku</button>
        <input ref="scanInput" type="file" accept="image/*" capture="environment" hidden @change="scanImage">
        <button v-if="federationEnabled" class="small secondary" @click="toggleTransfer">{{ transferEnabled ? 'Potlačit přenos záznamů' : 'Povolit přenos záznamů' }}</button>
      </div>
      <figure v-if="pairingQr"><img :src="pairingQr" alt="Jednorázový QR pro spárování pultů"><figcaption>Načtěte na druhém pultu</figcaption></figure>
      <p v-if="pairingCode" class="pairing-code"><small>Párovací kód</small><strong>{{ pairingCode.slice(0, 5) }}-{{ pairingCode.slice(5) }}</strong></p>
      <label>QR obsah nebo párovací kód <textarea v-model="pairingInput" rows="3" placeholder="Např. ABCDE-FG234 nebo multicam://federation?…"></textarea></label>
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
.pairing figure { max-width: 280px; margin: 12px 0; padding: 10px; color: #07101d; text-align: center; background: white; border-radius: 10px; }
.pairing img { display: block; width: 100%; }
.pairing-code { display: inline-flex; flex-direction: column; gap: 3px; margin: 8px 0 14px; padding: 10px 16px; border: 1px solid #456; border-radius: 9px; }
.pairing-code strong { font-size: 1.45rem; letter-spacing: .12em; }
.pairing label, .pairing textarea { display: block; width: 100%; }
.pairing textarea { margin: 6px 0 10px; }
</style>
