<script setup lang="ts">
import { onMounted, ref } from 'vue'
import QRCode from 'qrcode'
import { getHotspotStatus, type HotspotStatus } from './api'

const status = ref<HotspotStatus>({ active: false })
const wifiQr = ref('')
const appQr = ref('')
const error = ref('')

function escapeWifi(value: string): string {
  return value.replace(/([\\;,:"])/g, '\\$1')
}

async function refresh() {
  try {
    status.value = await getHotspotStatus()
    if (status.value.active && status.value.ssid && status.value.password && status.value.app_url) {
      const options = { margin: 1, width: 260, color: { dark: '#07101d', light: '#ffffff' } }
      wifiQr.value = await QRCode.toDataURL(
        `WIFI:T:WPA;S:${escapeWifi(status.value.ssid)};P:${escapeWifi(status.value.password)};;`,
        options,
      )
      appQr.value = await QRCode.toDataURL(status.value.app_url, options)
    }
  } catch {
    error.value = 'Stav hotspotu nelze načíst.'
  }
}

onMounted(refresh)
</script>

<template>
  <section class="hotspot-panel">
    <div class="heading"><div><span class="eyebrow">ostrovní síť</span><h3>Wi‑Fi hotspot</h3></div><button class="small" @click="refresh">Obnovit</button></div>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-else-if="!status.active" class="inactive">
      <strong>Hotspot neběží</strong>
      <small>Spusťte aplikaci příkazem <code>./run-hotspot.sh</code>. Vyžádá si oprávnění správce.</small>
    </div>
    <template v-else>
      <p class="network"><strong>{{ status.ssid }}</strong><code>{{ status.password }}</code><small>{{ status.interface }} · bez internetu</small></p>
      <div class="qr-grid">
        <figure><img :src="wifiQr" alt="QR pro připojení k Wi-Fi"><figcaption>1. Připojit k Wi‑Fi</figcaption></figure>
        <figure><img :src="appQr" alt="QR adresy MultiCam"><figcaption>2. Otevřít MultiCam</figcaption></figure>
      </div>
      <p class="address">{{ status.app_url }}</p>
    </template>
  </section>
</template>

<style scoped>
.hotspot-panel { margin-bottom: 26px; padding: 18px; border: 1px solid #365a4a; border-radius: 14px; background: #0b1322; }
.heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h3 { margin: 3px 0 0; }
.inactive { display: grid; gap: 5px; margin-top: 15px; color: #aab7ca; }
code { padding: 3px 6px; border-radius: 5px; background: #050a12; }
.network { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.network small { margin-left: auto; }
.qr-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
figure { margin: 0; padding: 10px; color: #07101d; text-align: center; border-radius: 12px; background: white; }
img { display: block; width: 100%; }
figcaption { padding: 7px 0 2px; font-size: .78rem; font-weight: 800; }
.address { overflow-wrap: anywhere; color: #7dd3fc; text-align: center; }
@media (max-width: 520px) { .qr-grid { grid-template-columns: 1fr; } }
</style>
