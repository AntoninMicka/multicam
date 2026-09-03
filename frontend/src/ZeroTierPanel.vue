<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getZeroTierStatus, joinZeroTier, type ZeroTierStatus } from './api'

const status = ref<ZeroTierStatus>({ installed: false, online: false, networks: [] })
const networkId = ref('')
const busy = ref(false)
const message = ref('')
const error = ref('')

async function refresh() {
  try {
    status.value = await getZeroTierStatus()
    if (!networkId.value && status.value.remembered_network_id) networkId.value = status.value.remembered_network_id
    error.value = ''
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Stav ZeroTier nelze načíst.'
  }
}

async function connect() {
  busy.value = true
  error.value = ''
  try {
    const result = await joinZeroTier(networkId.value.trim(), !status.value.installed)
    message.value = result.detail
    await refresh()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Připojení k ZeroTier selhalo.'
  } finally {
    busy.value = false
  }
}

const validNetworkId = () => /^[0-9a-fA-F]{16}$/.test(networkId.value)

onMounted(refresh)
</script>

<template>
  <section class="zerotier-panel">
    <details>
      <summary><span><span class="eyebrow">páteřní síť</span><strong>ZeroTier</strong></span><span :class="['state', { online: status.online, unknown: status.installed && status.status_available === false }]">{{ !status.installed ? 'nenainstalován' : status.status_available === false ? 'stav nedostupný' : status.online ? 'online' : 'offline' }}</span></summary>
      <div class="toolbar"><small v-if="status.node_id">Node {{ status.node_id }} · {{ status.version }}</small><button class="small secondary" @click="refresh">Obnovit</button></div>
      <small v-if="status.cli_path" class="muted">CLI: {{ status.cli_path }}</small>
      <p v-if="status.detail" class="muted">{{ status.detail }}</p>
      <div v-if="status.networks.length" class="networks">
        <article v-for="network in status.networks" :key="network.id">
          <strong>{{ network.name }}</strong><code>{{ network.id }}</code>
          <small>{{ network.status }} · {{ network.interface ?? 'bez rozhraní' }}</small>
          <small>{{ network.addresses.join(', ') || 'čeká na přidělení adresy / autorizaci' }}</small>
        </article>
      </div>
      <label>Network ID <input v-model.trim="networkId" maxlength="16" pattern="[0-9a-fA-F]{16}" :placeholder="status.installed ? '16 hexadecimálních znaků' : 'volitelné — lze doplnit později'"></label>
      <button :disabled="busy || (status.installed && !validNetworkId()) || (!status.installed && !!networkId && !validNetworkId())" @click="connect">{{ busy ? 'Čekám na systémové potvrzení…' : status.installed ? 'Připojit síť' : networkId ? 'Nainstalovat a připojit' : 'Nainstalovat ZeroTier' }}</button>
      <p v-if="message" class="muted">{{ message }}</p>
      <p v-if="error" class="error">{{ error }}</p>
      <small class="muted">Systém si může vyžádat administrátorské heslo. Nového člena je poté nutné autorizovat v ZeroTier Central.</small>
    </details>
  </section>
</template>

<style scoped>
.zerotier-panel { margin: 18px 0; padding: 16px 18px; border: 1px solid #405170; border-radius: 14px; background: #0b1322; }
summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; cursor: pointer; }
summary > span:first-child { display: inline-flex; flex-direction: column; gap: 3px; }
.state { padding: 5px 9px; color: #fca5a5; border-radius: 999px; background: #7f1d1d55; font-size: .72rem; font-weight: 800; }
.state.online { color: #86efac; background: #14532d55; }
.state.unknown { color: #fde68a; background: #78350f66; }
.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.networks { display: grid; gap: 8px; margin-top: 12px; }
.networks article { display: grid; gap: 4px; padding: 10px; border: 1px solid #283750; border-radius: 9px; }
code { overflow-wrap: anywhere; color: #7dd3fc; }
</style>
