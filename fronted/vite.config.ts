import { defineConfig } from 'vite'
import path from 'path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'


function figmaAssetResolver() {
  return {
    name: 'figma-asset-resolver',
    resolveId(id) {
      if (id.startsWith('figma:asset/')) {
        const filename = id.replace('figma:asset/', '')
        return path.resolve(__dirname, 'src/assets', filename)
      }
    },
  }
}

export default defineConfig({
  plugins: [
    figmaAssetResolver(),
    // The React and Tailwind plugins are both required for Make, even if
    // Tailwind is not being actively used – do not remove them
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      // Alias @ to the src directory
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (/(?:\/|\\)(?:recharts|d3-[a-z]+|victory-vendor)(?:\/|\\)/.test(id)) return 'charts'
          if (/(?:\/|\\)(?:react|react-dom|scheduler)(?:\/|\\)/.test(id)) return 'react'
          if (/(?:\/|\\)(?:motion|framer-motion|motion-dom|motion-utils)(?:\/|\\)/.test(id)) return 'motion'
          if (/(?:\/|\\)(?:react-markdown|remark-[a-z-]+|unified|micromark[^/\\]*)(?:\/|\\)/.test(id)) return 'markdown'
        },
      },
    },
  },

  // File types to support raw imports. Never add .css, .tsx, or .ts files to this.
  assetsInclude: ['**/*.svg', '**/*.csv'],
})
