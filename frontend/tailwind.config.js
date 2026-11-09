/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // V524: AntiTrade theme — أحمر/أسود مع لمسات ذهبية
        bg: { DEFAULT: '#0a0000', soft: '#1a0505', card: '#1a0808', border: '#3a0a0a' },
        accent: {
          green: '#10b981',
          red: '#dc2626',         // أحمر أغمق للـ primary
          redBright: '#ef4444',   // أحمر فاتح للـ highlights
          yellow: '#f59e0b',
          blue: '#3b82f6',
          purple: '#8b5cf6',
          gold: '#fbbf24',        // ذهبي للـ clover accents
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  plugins: [],
}
