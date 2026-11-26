'use strict';
/* Runs before anything paints, so the window never flashes the wrong colours. Follows the system setting until the user picks one, then remembers it. */
(() => {
  const root = document.documentElement;
  const systemLight = window.matchMedia('(prefers-color-scheme: light)');
  const saved = () => { try { return localStorage.getItem('kc.theme'); } catch { return null; } };
  const wanted = () => { const choice = saved(); return choice === 'light' || choice === 'dark' ? choice : (systemLight.matches ? 'light' : 'dark'); };
  const apply = (theme) => {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'light' ? '#fbfaf8' : '#161615');
  };
  apply(wanted());
  systemLight.addEventListener('change', () => { if (!saved()) apply(wanted()); });
  window.kcToggleTheme = () => {
    const next = root.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('kc.theme', next); } catch { /* not remembered */ }
    root.classList.add('theming');
    apply(next);
    setTimeout(() => root.classList.remove('theming'), 320);
  };
})();
