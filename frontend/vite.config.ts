import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      }
    }
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        manualChunks: {
          'monaco': ['monaco-editor'],
          'react-vendor': ['react', 'react-dom'],
          'sandpack': ['@codesandbox/sandpack-react'],
        }
      }
    }
  }
})
