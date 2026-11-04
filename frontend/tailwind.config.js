/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: { DEFAULT: '#0a0e1a', soft: '#131826', card: '#1a2033', border: '#2a3245' },
        accent: { green: '#10b981', red: '#ef4444', yellow: '#f59e0b', blue: '#3b82f6', purple: '#8b5cf6' },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  plugins: [],
}
