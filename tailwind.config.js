module.exports = {
  darkMode: 'class',
  content: ['./templates/**/*.html', './static/app-runtime.js'],
  safelist: [
    'sm:grid-cols-1',
    'sm:grid-cols-2',
    'sm:grid-cols-3',
  ],
  theme: {
    extend: {
      colors: {
        gold: { DEFAULT: '#d4af37', soft: '#e8c66a' },
        silver: '#c0c0c0',
        obsidian: { DEFAULT: '#0b0b0d', 50: '#13131a', 100: '#1a1a22', 200: '#22222b' },
        ember: '#ff3b30',
        ice: '#2a6df4',
      },
      fontFamily: {
        sans: ['var(--font-sans)'],
        serif: ['var(--font-serif)'],
        mono: ['var(--font-mono)'],
      },
      boxShadow: {
        'card-soft': '0 1px 0 rgba(255,255,255,0.04), 0 4px 16px rgba(0,0,0,0.35)',
      },
    },
  },
};
