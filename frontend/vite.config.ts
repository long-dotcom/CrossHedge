import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('echarts')) return 'vendor-charts';
          if (id.includes('@tanstack') || id.includes('axios')) return 'vendor-data';
          return undefined;
        }
      }
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': apiProxyTarget,
      '/health': apiProxyTarget
    }
  }
});
