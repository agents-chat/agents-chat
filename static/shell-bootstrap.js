/* Flash-free theme bootstrap: blocking by design so the first paint is correct. */
(() => {
  try {
    const saved = localStorage.getItem('ac-theme');
    const themes = ['dark', 'light', 'signal', 'signal-light', 'emerald'];
    const darkThemes = ['dark', 'signal', 'emerald'];
    const metaColors = ['#0a0b10', '#f4f3ef', '#0d0c0a', '#f8f2ea', '#0a0e0b'];
    const theme = themes.includes(saved)
      ? saved
      : (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    const root = document.documentElement;
    root.dataset.theme = theme;
    root.classList.toggle('dark', darkThemes.includes(theme));
    document.getElementById('themeColorMeta')
      ?.setAttribute('content', metaColors[themes.indexOf(theme)] || '#0a0b10');
  } catch {}
})();
