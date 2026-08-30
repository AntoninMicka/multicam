import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'MultiCam Capture',
        short_name: 'MultiCam',
        description: 'Lokální synchronizované nahrávání z více telefonů',
        theme_color: '#101827',
        background_color: '#101827',
        display: 'standalone',
        start_url: '/',
        icons: [{ src: '/multicam.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any maskable' }],
      },
    }),
  ],
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', ws: true },
    },
  },
})
