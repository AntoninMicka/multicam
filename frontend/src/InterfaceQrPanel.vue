<script setup lang="ts">
import { onMounted, ref } from 'vue'
import QRCode from 'qrcode'
import { getNetworkInterfaces, type NetworkInterfaceAddress } from './api'

interface AddressQr extends NetworkInterfaceAddress { qr: string }
const addresses = ref<AddressQr[]>([])
const error = ref('')

async function refresh() {
  try {
    const result = await getNetworkInterfaces()
    addresses.value = await Promise.all(result.interfaces.map(async (item) => ({
      ...item,
      qr: await QRCode.toDataURL(item.url, { margin: 1, width: 240, color: { dark: '#07101d', light: '#ffffff' } }),
    })))
    error.value = ''
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Síťová rozhraní nelze načíst.'
  }
}

onMounted(refresh)
</script>

<template>
  <section class="interface-panel">
    <details>
      <summary><span><span class="eyebrow">přístup ke klientovi</span><strong>QR pro připojení kamer</strong></span></summary>
      <div class="heading"><p class="muted">Frontend podle síťového rozhraní</p><button class="small" @click="refresh">Obnovit</button></div>
      <p v-if="error" class="error">{{ error }}</p>
      <p v-else-if="!addresses.length" class="muted">Nebyla nalezena žádná použitelná síťová adresa.</p>
      <div v-else class="interface-grid">
        <figure v-for="item in addresses" :key="`${item.interface}-${item.address}`">
          <img :src="item.qr" :alt="`QR frontendu přes ${item.interface}`">
          <figcaption><strong>{{ item.interface }}</strong><small>{{ item.family }} · {{ item.address }}</small><a :href="item.url">{{ item.url }}</a></figcaption>
        </figure>
      </div>
      <small class="muted">Telefon musí být připojený k odpovídající síti a důvěřovat lokální certifikační autoritě.</small>
    </details>
  </section>
</template>

<style scoped>
.interface-panel { margin: 18px 0 26px; padding: 18px; border: 1px solid #405170; border-radius: 14px; background: #0b1322; }
.interface-panel summary { cursor: pointer; }
.interface-panel summary > span { display: inline-flex; flex-direction: column; gap: 3px; }
.interface-panel summary strong { font-size: 1.05rem; }
.heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.heading p { margin: 14px 0 0; }
.interface-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin: 15px 0; }
figure { margin: 0; padding: 10px; color: #07101d; text-align: center; border-radius: 12px; background: white; }
img { display: block; width: 100%; max-width: 240px; margin: auto; }
figcaption { display: grid; gap: 3px; padding-top: 7px; }
figcaption small, figcaption a { overflow-wrap: anywhere; color: #334155; }
</style>
