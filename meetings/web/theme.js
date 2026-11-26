'use strict';
/* Runs before anything paints, so the window never flashes the wrong colours. Follows the system until the user picks one, then remembers it. */
(() => {
  const root = document.documentElement;
  const systemLight = window.matchMedia('(prefers-color-scheme: light)');
  const saved = () => { try { return localStorage.getItem('kc.theme'); } catch { return null; } };
  // Inside Flock (?embed=1&theme=...) the harness decides the theme, and tells us when it changes.
  const params = new URLSearchParams(location.search);
  const embedTheme = params.get('theme');
  if (params.get('embed') === '1') root.classList.add('embed');
  const wanted = () => { if (embedTheme === 'light' || embedTheme === 'dark') return embedTheme; const choice = saved(); return choice === 'light' || choice === 'dark' ? choice : (systemLight.matches ? 'light' : 'dark'); };
  const apply = (theme) => {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'light' ? '#fbfaf8' : '#161615');
  };
  apply(wanted());
  systemLight.addEventListener('change', () => { if (!saved()) apply(wanted()); });
  window.addEventListener('message', (event) => {
    if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(event.origin)) return;
    const theme = event.data && event.data.kcTheme;
    if (theme === 'light' || theme === 'dark') { root.classList.add('theming'); apply(theme); setTimeout(() => root.classList.remove('theming'), 320); }
  });
  window.kcToggleTheme = () => {
    const next = root.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('kc.theme', next); } catch { /* not remembered */ }
    root.classList.add('theming');
    apply(next);
    setTimeout(() => root.classList.remove('theming'), 320);
  };
})();
