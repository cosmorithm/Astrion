import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: { host: '127.0.0.1', proxy: { '/api': process.env.ASTRION_BACKEND_URL || 'http://127.0.0.1:8000' } },
})
