'use strict';
/* Flock front end: chats | chat | files. Everything the agent does arrives as Server-Sent Events from /api/events and is
   drawn as it happens: thinking, text, tool calls (with what they changed), and each step's speed and cache use. */
const $ = (id) => document.getElementById(id);
const S = {
  cfg: null, projects: [], project: null, chats: [], chat: null, meta: null, msgs: [], busy: new Set(),
  runtime: null, live: {}, chips: [], queued: 0, queueOpen: {}, lastStats: null, filter: '', health: {}, groups: [], msgStart: 0, msgTotal: 0, subs: {},
  embeddedSubs: [], parentInfo: null,
  sections: null, statuses: {}, burnAlerts: [],
};
const LS = {
  get(k, d) { try { const v = localStorage.getItem('kch.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('kch.' + k, JSON.stringify(v)); } catch { /* not remembered */ } },
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };
const fmtK = (n) => n == null ? '?' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k' : String(n);
const base = (p) => String(p || '').split(/[\\/]/).filter(Boolean).pop() || p;
window.KCH = { S, $, LS, esc, el, fmtK, base };

const SLOW_MS = 200;
const _slowBuf = [];
let _slowTimer = null;
function flushSlow() {
  clearTimeout(_slowTimer);
  _slowTimer = null;
  if (!_slowBuf.length) return;
  const batch = _slowBuf.splice(0, 30);
  fetch('/api/perf', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-KC-App': '1' },
    body: JSON.stringify({ events: batch }),
    keepalive: true,
  }).catch(() => {});
}
function reportSlow(kind, what, ms, detail = '') {
  if (!(ms >= SLOW_MS)) return;
  if (_slowBuf.length >= 60) _slowBuf.shift();
  _slowBuf.push({ kind, what: String(what || '').slice(0, 140), ms: Math.round(ms), detail: String(detail || '').slice(0, 140) });
  if (!_slowTimer) _slowTimer = setTimeout(flushSlow, 4000);
}
KCH.reportSlow = reportSlow;
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushSlow(); });
document.addEventListener('click', (ev) => {
  const target = ev.target?.closest?.('button, [role="button"], .chat-item, .sub-chip, .sub-head, .subrun > .row');
  if (!target) return;
  const t0 = performance.now();
  const label = (target.id ? '#' + target.id : (target.getAttribute('aria-label') || target.textContent || target.className || 'button'))
    .replace(/\s+/g, ' ').trim().slice(0, 64);
  requestAnimationFrame(() => {
    const dt = performance.now() - t0;
    if (dt >= SLOW_MS) reportSlow('click', label, dt);
  });
}, true);
try {
  if (typeof PerformanceObserver !== 'undefined') {
    const types = PerformanceObserver.supportedEntryTypes || [];
    if (types.includes('long-animation-frame')) {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          if (e.duration >= SLOW_MS) {
            const top = (e.scripts || []).reduce((a, b) => ((b.duration || 0) > (a?.duration || 0) ? b : a), null);
            const src = top?.invoker || (top?.sourceURL ? String(top.sourceURL).split('/').pop() : '');
            const blk = e.blockingDuration != null ? `block=${Math.round(e.blockingDuration)}ms` : '';
            reportSlow('freeze', 'loaf', e.duration, [blk, src].filter(Boolean).join(' '));
          }
        }
      }).observe({ type: 'long-animation-frame', buffered: false });
    } else if (types.includes('longtask')) {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          if (e.duration >= SLOW_MS) reportSlow('freeze', e.name || 'longtask', e.duration);
        }
      }).observe({ type: 'longtask', buffered: false });
    }
  }
} catch { /* unsupported browser */ }

async function api(path, body, opts = {}) {
  const t0 = performance.now();
  const init = { method: body === undefined && !opts.method ? 'GET' : (opts.method || 'POST'), headers: {} };
  if (init.method !== 'GET') {
    init.headers['X-KC-App'] = '1';
    if (body instanceof Blob) { init.body = body; init.headers['Content-Type'] = body.type || 'application/octet-stream'; }
    else { init.body = JSON.stringify(body || {}); init.headers['Content-Type'] = 'application/json'; }
  }
  const r = await fetch(path, init);
  let data = {};
  try { data = await r.json(); } catch { /* empty */ }
  const dt = performance.now() - t0;
  if (dt >= SLOW_MS && !String(path).startsWith('/api/perf') && !String(path).startsWith('/api/dictation/')) {
    reportSlow('api', `${init.method} ${path}`, dt, `status=${r.status}`);
  }
  if (!r.ok) { const e = new Error(data.error || `HTTP ${r.status}`); e.data = data; e.status = r.status; throw e; }
  return data;
}
KCH.api = api;

function toast(text, ms = 3200) {
  const t = $('toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.h);
  toast.h = setTimeout(() => { t.hidden = true; }, ms);
}
KCH.toast = toast;

/* ---------------------------------------------------------------- markdown */
const md = (() => {
  const renderer = {
    code(code, lang) {
      if (code && typeof code === 'object') { lang = code.lang; code = code.text; }
      const l = (lang || '').trim().split(/\s/)[0];
      const vk = window.KCVis?.visualKind?.(l);
      if (vk) return window.KCVis.placeholderHTML(vk, code, { streaming: false, closed: true });
      return `<div class="code"><div class="lab"><span>${esc(l || 'text')}</span><button data-copy>Copy</button></div><pre><code>${esc(code)}</code></pre></div>`;
    },
  };
  try {
    const tokenizer = {
      del(src) {
        if (src[0] === '~' && src[1] !== '~') return { type: 'text', raw: '~', text: '~' };
        const m = /^~~(?=\S)([\s\S]*?\S)~~/.exec(src);
        if (m) return { type: 'del', raw: m[0], text: m[1], tokens: this.lexer.inlineTokens(m[1]) };
        return undefined;
      },
    };
    marked.use({ renderer, tokenizer, gfm: true, breaks: false });
  } catch { /* older marked */ }
  return (text, opts) => {
    try {
      const pre = window.KCVis?.preprocessStreaming ? window.KCVis.preprocessStreaming(text || '', Boolean(opts?.streaming)) : { text: text || '', tailPlaceholder: '' };
      const body = pre.text ? withMedia(DOMPurify.sanitize(markLocal(marked.parse(pre.text)))) : '';
      return body + (pre.tailPlaceholder || '');
    } catch { return esc(text); }
  };
})();
/* A file the model names in its reply is shown in the chat itself: the
   absolute path of a sound, video or picture file, in backticks or as a link or image target, gets a player or the
   picture under it. The path is all a model has to write; the bytes come from /api/files/raw (seeking works). */
const MEDIA_RE = { audio: /\.(mp3|wav|m4a|aac|ogg|oga|opus|flac|weba)$/i, video: /\.(mp4|webm|mov|m4v|mkv)$/i, image: /\.(png|jpe?g|gif|webp|bmp|avif)$/i };
function mediaKind(p) { return MEDIA_RE.audio.test(p) ? 'audio' : MEDIA_RE.video.test(p) ? 'video' : MEDIA_RE.image.test(p) ? 'image' : ''; }
function localPath(t) {
  let p = String(t || '').trim();
  try {
    if (/^file:\/\//i.test(p)) p = decodeURIComponent(p.replace(/^file:\/+/i, ''));
    else if (/%[0-9a-f]{2}/i.test(p)) p = decodeURIComponent(p);
  } catch { /* as written */ }
  if (!/^([a-z]:[\\/]|\/[^/]|~[\\/])/i.test(p)) return '';
  return /[\n<>"|?*]/.test(p.slice(2)) ? '' : p;
}
function mediaEl(path, kind) {
  const w = document.createElement('div');
  w.className = 'm-media ' + kind;
  const src = '/api/files/raw?path=' + encodeURIComponent(path);
  const name = path.replace(/^.*[\\/]/, '');
  const m = document.createElement(kind === 'image' ? 'img' : kind);
  m.setAttribute('src', src);
  if (kind === 'image') { m.setAttribute('alt', ''); m.setAttribute('loading', 'lazy'); m.setAttribute('data-zoom', src); }
  else {
    m.setAttribute('controls', '');
    m.setAttribute('preload', kind === 'video' ? 'metadata' : 'none');
    if (kind === 'video') {
      m.setAttribute('playsinline', '');
      m.setAttribute('webkit-playsinline', '');
    }
  }
  const cap = document.createElement('div');
  cap.className = 'm-media-name';
  cap.textContent = name;
  cap.title = path;
  if (kind === 'video') {
    const box = document.createElement('div');
    box.className = 'm-media-box';
    const fsIcon = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"/></svg>';
    const fab = document.createElement('button');
    fab.type = 'button';
    fab.className = 'm-media-fs-fab';
    fab.setAttribute('data-video-fs', src);
    fab.setAttribute('data-video-title', name);
    fab.setAttribute('aria-label', 'Fullscreen video');
    fab.title = 'Fullscreen';
    fab.innerHTML = fsIcon;
    box.append(m, fab);
    const foot = document.createElement('div');
    foot.className = 'm-media-foot';
    const fsBtn = document.createElement('button');
    fsBtn.type = 'button';
    fsBtn.className = 'm-media-fs';
    fsBtn.setAttribute('data-video-fs', src);
    fsBtn.setAttribute('data-video-title', name);
    fsBtn.setAttribute('aria-label', 'Fullscreen video');
    fsBtn.innerHTML = fsIcon + '<span>Fullscreen</span>';
    foot.append(cap, fsBtn);
    w.append(box, foot);
    return w;
  }
  w.append(m, cap);
  return w;
}
/* Before the sanitizer runs: it drops a link or image whose target is a file path ("C:/..." reads as an unknown scheme),
   so the path is carried in data-local instead. The template is inert: nothing in it loads or runs. */
function markLocal(html) {
  if (!/(href|src)=/i.test(html)) return html;
  const t = document.createElement('template');
  t.innerHTML = html;
  let n = 0;
  for (const el of t.content.querySelectorAll('a[href], img[src]')) {
    const attr = el.tagName === 'IMG' ? 'src' : 'href';
    let p = localPath(el.getAttribute(attr));
    if (attr === 'href') p = p.replace(/#.*$/, '');          // file:///C:/x/Pill.kt#L10
    if (!p || (attr === 'src' && !mediaKind(p))) continue;
    el.setAttribute('data-local', p);
    el.removeAttribute(attr);
    n += 1;
  }
  return n ? t.innerHTML : html;
}
function withMedia(html) {
  if (!/<a /i.test(html) && !/\.(mp3|wav|m4a|aac|ogg|oga|opus|flac|weba|mp4|webm|mov|m4v|mkv|png|jpe?g|gif|webp|bmp|avif)/i.test(html)) return html;
  const t = document.createElement('template');
  t.innerHTML = html;
  const seen = new Set();
  const add = (node, path, replace) => {
    const kind = mediaKind(path);
    if (!kind) return;
    const key = path.toLowerCase().replace(/\\/g, '/');
    if (seen.has(key)) { if (replace) node.remove(); return; }
    seen.add(key);
    const m = mediaEl(path, kind);
    if (replace) { node.replaceWith(m); return; }
    const table = node.closest('table');
    if (table) { table.after(m); return; }
    const block = node.closest('p, li, td, h1, h2, h3, h4, blockquote') || node;
    if (block.tagName === 'TD' || block.tagName === 'LI') block.appendChild(m); else block.after(m);
  };
  for (const img of [...t.content.querySelectorAll('img[data-local]')]) add(img, img.dataset.local, true);     // ![](/path/to/pic.png)
  for (const a of [...t.content.querySelectorAll('a[data-local]')]) {         // [name](</path/to/take.wav>), [file.py](file:///path/to/file.py)
    add(a, a.dataset.local, false);
    a.classList.add('file-link');                // a tap opens the file in the app's viewer
    a.setAttribute('role', 'link');
    a.setAttribute('tabindex', '0');
  }
  for (const a of [...t.content.querySelectorAll('a[href]')]) {               // a web link opens outside the chat page
    if (/^https?:/i.test(a.getAttribute('href'))) { a.setAttribute('target', '_blank'); a.setAttribute('rel', 'noopener noreferrer'); }
  }
  for (const c of [...t.content.querySelectorAll('code')]) {                  // `/path/to/clip.mp3`
    if (c.closest('pre')) continue;
    const p = localPath(c.textContent);
    if (p) add(c, p, false);
  }
  return t.innerHTML;
}
document.addEventListener('click', (ev) => {
  const vfs = ev.target.closest('[data-video-fs]');
  if (vfs && typeof zoomVideo === 'function') {
    ev.preventDefault();
    ev.stopPropagation();
    const wrap = vfs.closest('.m-media');
    const inlineVid = wrap ? wrap.querySelector('video') : null;
    const src = vfs.dataset.videoFs || inlineVid?.currentSrc || inlineVid?.getAttribute('src') || '';
    if (src) zoomVideo(src, { from: inlineVid, title: vfs.dataset.videoTitle || '' });
    return;
  }
  const z = ev.target.closest('img[data-zoom]');
  if (z && typeof zoom === 'function') { zoom(z.dataset.zoom); return; }
  const f = ev.target.closest('a[data-local]');
  if (f) {                                       // a link to a file on this machine: open it in the file viewer
    ev.preventDefault();
    window.KCF?.showPane?.('files');
    KCH.setRight?.(true);
    window.KCF?.openFile(f.dataset.local);
  }
});
document.addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-copy]');
  if (!b) return;
  const code = b.closest('.code')?.querySelector('code')?.textContent || '';
  navigator.clipboard?.writeText(code).then(() => { b.textContent = 'Copied'; setTimeout(() => { b.textContent = 'Copy'; }, 1200); });
});

/* ---------------------------------------------------------------- icons for tools */
const IC = {
  Read: '<path d="M3 3.5h4.2A1.8 1.8 0 0 1 9 5.3V13a1.4 1.4 0 0 0-1.4-1.4H3zM13 3.5H8.8A1.8 1.8 0 0 0 7 5.3"/><path d="M13 3.5v8.1H9"/>',
  Write: '<path d="M9 2H4.5A1.5 1.5 0 0 0 3 3.5v9A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5V6z"/><path d="M9 2v4h4"/>',
  Edit: '<path d="M10.5 2.5l3 3L6 13H3v-3z"/>',
  Bash: '<path d="M3 4.5l3.5 3.5L3 11.5M8.5 11.5H13"/>',
  Glob: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/>',
  Grep: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14M5 7h4"/>',
  WebSearch: '<circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2.6 4 2.6 6S10 12 8 14C6 12 5.4 10 5.4 8S6 4 8 2z"/>',
  WebFetch: '<path d="M8 2v8.5M4.5 7L8 10.5 11.5 7M3 13.5h10"/>',
  Browser: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2 6h12"/>',
  TodoWrite: '<path d="M3 4.5l1.2 1.2L6.5 3.5M3 10l1.2 1.2 2.3-2.2M8.5 5h5M8.5 10.5h5"/>',
  Notes: '<path d="M4 2.5h8v11H4z"/><path d="M6 5.5h4M6 8h4M6 10.5h2.5"/>',
  Skill: '<path d="M8 2l1.8 3.8 4.2.5-3.1 2.9.8 4.1L8 11.3l-3.7 2 .8-4.1L2 6.3l4.2-.5z"/>',
  mcp: '<circle cx="5" cy="8" r="2"/><circle cx="11" cy="8" r="2"/><path d="M7 8h2"/>',
  Agent: '<circle cx="8" cy="5" r="2.5"/><path d="M3.5 13.5c.6-2.6 2.3-4 4.5-4s3.9 1.4 4.5 4"/>',
  Computer: '<rect x="2" y="2.5" width="12" height="8.5" rx="1.2"/><path d="M6 13.5h4M8 11v2.5"/>',
};
const isAgent = (name) => name === 'Agent' || /__harness_agent$/.test(name || '');
const isComputer = (name) => name === 'Computer' || name === 'mcp__computer__computer';
const icon = (name) => `<svg viewBox="0 0 16 16">${IC[name] || IC.mcp}</svg>`;

function parseArgs(raw) { try { return JSON.parse(raw || '{}'); } catch { return {}; } }
function rel(p) {
  if (!p || !S.project) return p || '';
  const a = String(p).replace(/\\/g, '/'), b = S.project.replace(/\\/g, '/').replace(/\/$/, '') + '/';
  return a.toLowerCase().startsWith(b.toLowerCase()) ? a.slice(b.length) : a;
}
function toolLabel(name, raw) {
  const a = parseArgs(raw);
  switch (name) {
    case 'Read': return rel(a.file_path || a.path) + (a.offset ? `:${a.offset}` : '');
    case 'Write': case 'Edit': return rel(a.file_path || a.path);
    case 'Bash': case 'PowerShell': return (a.background || a.run_in_background ? '& ' : '$ ') + (a.command || '');
    case 'Glob': return a.pattern || '';
    case 'Grep': return `${a.pattern || ''}${a.glob ? '  ' + a.glob : ''}`;
    case 'WebSearch': return a.query || (a.queries || []).join(' | ');
    case 'WebFetch': return a.url || '';
    case 'Browser': return [a.action, a.url || a.element || a.ref || a.key || a.script || ''].filter(Boolean).join(' ');
    case 'TodoWrite': return `${(a.todos || []).filter((t) => t.status === 'completed').length}/${(a.todos || []).length} done`;
    case 'Notes': return a.replace !== undefined ? 'rewrite' : a.add ? a.add.split('\n')[0].slice(0, 120) : 'read';
    case 'Skill': return a.name || '';
    case 'Agent': return `${a.model ? a.model + ': ' : ''}${a.description || a.prompt || ''}`;
    case 'Computer': return [a.action, a.coordinate || a.region ? `(${(a.coordinate || a.region).join(', ')})` : '', a.text ? JSON.stringify(a.text).slice(0, 80) : ''].filter(Boolean).join(' ');
    default:
      if (isComputer(name)) return toolLabel('Computer', raw);
      if (isAgent(name)) return toolLabel('Agent', raw); return raw ? String(raw).slice(0, 160) : '';
  }
}

/* ---------------------------------------------------------------- chat rendering */
/* Short timestamp at the bottom of a chat's last message (Today: 15:53; yesterday: Yesterday 15:53; older: 21 Nov 15:53).
   Drawn by CSS (`[data-stamp]::after`) on the last message's own element, so a reply still streaming into that turn stays
   above it and lastTurn() still finds the turn. */
function shortStamp(ts) {
  const d = new Date(ts * 1000), now = new Date();
  if (Number.isNaN(d.getTime())) return '';
  const hm = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
  if (d.toDateString() === now.toDateString()) return hm;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return 'Yesterday ' + hm;
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) }) + ' ' + hm;
}
function stampLast() {
  const box = $('msgs');
  if (!box) return;
  box.querySelectorAll(':scope > [data-stamp]').forEach((n) => { delete n.dataset.stamp; });
  let last = null;
  for (let i = S.msgs.length - 1; i >= 0; i--) {
    const m = S.msgs[i];
    if ((m.role === 'user' && !m.fold_brief) || (m.role === 'assistant' && String(m.content || '').trim())) { last = m; break; }
  }
  if (!last) return;
  const kids = [...box.children].filter((n) => !n.classList.contains('working'));
  const holder = [...kids].reverse().find((n) => (last.role === 'user' ? n.classList.contains('msg') && n.classList.contains('user') : n.classList.contains('turn')));
  const when = shortStamp(last.ts || last.t || Date.now() / 1000);
  if (holder && when) holder.dataset.stamp = when;
}
function lastTurn() {                // the "Working" line at the bottom is not part of any turn: look past it
  const m = $('msgs'), w = m.querySelector(':scope > .working');
  const last = w && w === m.lastElementChild ? w.previousElementSibling : m.lastElementChild;
  if (last && last.classList.contains('turn')) return last;
  const t = el('div', 'turn');
  m.insertBefore(t, w && w === m.lastElementChild ? w : null);
  return t;
}

function statsLine(st, ep) {
  if (!st) return null;
  const parts = [];
  if (st.tps) parts.push(`<span class="${st.tps >= 80 ? 'fast' : ''}">${st.tps} tok/s</span>`);
  // in = the whole prompt; cached = read from the prompt cache (cheap); written = added to the cache (Claude bills a
  // write above a plain read); out = the reply, thinking included
  if (st.prompt_new != null || st.prompt_cached) parts.push(`${fmtK((st.prompt_new || 0) + (st.prompt_cached || 0))} in`);
  if (st.prompt_cached) parts.push(`${fmtK(st.prompt_cached)} cached`);
  if (st.prompt_written) parts.push(`${fmtK(st.prompt_written)} written`);
  if (st.out_tokens) parts.push(`${fmtK(st.out_tokens)} out`);
  if (st.draft_accept != null) parts.push(`drafts ${Math.round(st.draft_accept * 100)}% kept`);
  if (st.ttft != null) parts.push(`first token ${st.ttft}s`);
  if (st.secs != null) parts.push(`${st.secs}s`);
  if (st.effort) parts.push(`<span class="eff">${esc(/·|^(Fast|Think|Deep)$/.test(st.effort) ? st.effort : 'effort ' + st.effort)}</span>`);
  const d = el('div', 'stats', parts.join('<span>·</span>'));
  d.title = (`${ep || ''} ${st.model || ''}`.trim() + '\nin: the whole prompt · cached: read from the prompt cache · ' +
    'written: added to the cache this step · out: the reply').trim();
  return d;
}

function thinkBlock(text, live) {
  const d = el('div', 'think' + (live ? ' live' : ''));
  d.innerHTML = `<button><svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg><span>${live ? 'Thinking' : 'Thought'}</span><span class="peek"></span></button><div class="body"></div>`;
  setThought(d, text);
  d.querySelector('button').onclick = () => d.classList.toggle('open');
  return d;
}
/* a thought's text, and its one-line glimpse beside the label: the start once it is done, the newest words while live */
function setThought(d, text) {
  d.querySelector('.body').textContent = text || '';
  const flat = (text || '').replace(/\s+/g, ' ').trim();
  d.querySelector('.peek').textContent = d.classList.contains('live') ? flat.slice(-110) : flat.slice(0, 110);
}

/* ---------------------------------------------------------------- the turn's flow
   What the model says stays in view. Everything it does in between (thinking, tool calls, each step's numbers) folds
   into one activity line per stretch: "Read 3 files, ran 2 commands", with what it is doing now while it runs. A click
   opens it. Agent calls stay outside (their block shows the subagent's progress), as do questions and checks. The
   turn's own numbers (steps, tokens, time) go in one line at its end. */
const KINDS = [
  ['read', /^(Read|NotebookRead)$/, (n) => `read ${n} file${n === 1 ? '' : 's'}`],
  ['edit', /^(Edit|MultiEdit|Write|NotebookEdit)$/, (n) => `edited ${n} file${n === 1 ? '' : 's'}`],
  ['bash', /^(Bash|BashOutput|KillShell|KillBash|PowerShell)$/, (n) => `ran ${n} command${n === 1 ? '' : 's'}`],
  ['search', /^(Glob|Grep|LS|ToolSearch)$/, (n) => `${n} search${n === 1 ? '' : 'es'}`],
  ['web', /^(WebSearch|WebFetch)$/, (n) => `${n} web lookup${n === 1 ? '' : 's'}`],
  ['browser', /^(Browser|mcp__playwright__.*|mcp__claude-in-chrome__.*|mcp__Claude_Browser.*)$/, (n) => `${n} browser step${n === 1 ? '' : 's'}`],
  ['computer', /^(Computer|mcp__computer__computer)$/, (n) => `${n} computer action${n === 1 ? '' : 's'}`],
  ['todo', /^(TodoWrite)$/, () => 'updated the to-dos'],
  ['other', /./, (n) => `${n} other tool call${n === 1 ? '' : 's'}`],
];
function quietNode(n) {
  if (n.classList.contains('think')) return true;
  return n.classList.contains('tool') && !isAgent(n._call?.name);
}
function actsGroup(turn) {
  const last = turn.lastElementChild;
  if (last?.classList.contains('acts')) return last;
  const g = el('div', 'acts');
  g.innerHTML = '<button class="acts-head"><svg class="chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg><span class="acts-sum"></span>' +
    '<span class="acts-now"></span><span class="acts-bad"></span></button><div class="acts-body"></div>';
  g.querySelector('.acts-head').onclick = () => g.classList.toggle('open');
  turn.appendChild(g);
  return g;
}
function flowNode(turn, n) {
  if (!quietNode(n)) { turn.appendChild(n); return; }
  const g = actsGroup(turn);
  g.querySelector('.acts-body').appendChild(n);
  paintGroup(g);
}
function modelLabelFor(m) {
  if (m.model_label) return m.model_label;
  const ep = (S.cfg?.endpoints || []).find((e) => e.id === m.endpoint);
  if (ep?.name) return ep.name;
  return m.stats?.model || m.endpoint || '';
}
function showModelTag() {
  if (S.meta?.neon) return true;
  const eps = new Set((S.msgs || []).filter((x) => x.role === 'assistant' && x.endpoint).map((x) => x.endpoint));
  return eps.size > 1;
}
function flowAssistant(m, turn) {
  if (m.id) {
    turn._lastMid = m.id;
    turn.dataset.mid = m.id;
  }
  const lbl = modelLabelFor(m);
  if (lbl && showModelTag() && !turn.querySelector(':scope > .m-by')) {
    turn.prepend(el('div', 'm-by', esc(lbl)));
  }
  for (const n of assistantNodes(m)) flowNode(turn, n);
  if (!m.stats) return;
  (turn._stats ||= []).push(m.stats);
  turn._ep = m.endpoint;
  // the step's own numbers: a tooltip on its last row, not a line after every call (the turn's total is under the turn)
  const row = turn.lastElementChild?.classList.contains('acts') && turn.lastElementChild.querySelector('.acts-body').lastElementChild;
  const s = row && statsLine(m.stats, m.endpoint);
  if (s) row.title = [...s.children].map((c) => c.textContent).filter((t) => t !== '·').join(' · ');
}
function unflowEmpty(L) {                    // a step cut off before it said anything: no empty blocks left behind
  if (L.prose && !L.text.trim()) unflow(L.prose);
  if (L.think && !L.reasoning.trim()) unflow(L.think);
}
function paintTurnSums(box, skipLast) {
  const turns = [...box.querySelectorAll(':scope > .turn')];
  let carry = [];                            // a turn a steered message split: its numbers go into the part after it
  turns.forEach((t, i) => {
    t._all = carry.concat(t._stats || []);
    if (t._cont && i < turns.length - 1) { t.querySelector(':scope > .turnsum')?.remove(); carry = t._all; return; }
    carry = [];
    if (!(skipLast && i === turns.length - 1)) paintTurnSum(t);
  });
}
function unflow(n) {                         // a live step's draft nodes, taken out before the finished step goes in
  const g = n?.closest('.acts');
  n?.remove();
  if (g) paintGroup(g);
}
function paintGroup(g) {
  const body = g.querySelector('.acts-body');
  const rows = [...body.querySelectorAll(':scope > .tool')];
  const thinks = [...body.querySelectorAll(':scope > .think')];
  if (!rows.length && !thinks.length) { g.remove(); return; }
  const count = {};
  for (const r of rows) {
    const k = KINDS.find(([, rx]) => rx.test(r._call?.name || ''))[0];
    count[k] = (count[k] || 0) + 1;
  }
  const parts = KINDS.filter(([k]) => count[k]).map(([k, , say]) => say(count[k]));
  if (thinks.length && !rows.length) parts.push('thought');
  const sum = parts.join(', ');
  g.querySelector('.acts-sum').textContent = sum.charAt(0).toUpperCase() + sum.slice(1);
  const liveThink = thinks.find((t) => t.classList.contains('live'));
  const running = rows.filter((r) => r.querySelector('.st.run'));
  const last = body.lastElementChild?.classList.contains('stats') ? body.lastElementChild.previousElementSibling : body.lastElementChild;
  let now = '';
  if (liveThink && last === liveThink) now = 'Thinking' + (liveThink.querySelector('.peek').textContent ? ': ' + liveThink.querySelector('.peek').textContent : '');
  else if (running.length || rows.length === 1) now = (running[running.length - 1] || rows[0]).querySelector('.arg').textContent;
  else if (!rows.length) now = thinks[thinks.length - 1].querySelector('.peek').textContent;    // a thought's first words
  g.querySelector('.acts-now').textContent = now;
  g.classList.toggle('thought', !rows.length);
  const bad = rows.filter((r) => r.classList.contains('failed') || r.querySelector('.meta .minus')?.textContent.startsWith('exit')).length;
  g.querySelector('.acts-bad').textContent = bad ? `${bad} failed` : '';
  g.classList.toggle('running', !!(liveThink || running.length));
}
const FORK_SVG = '<svg viewBox="0 0 16 16"><path d="M5 3v4.5a3 3 0 0 0 3 3h3M11 8.5l2 2-2 2M5 7.5V13"/></svg>';
function turnForkBtn(mid) {
  const b = el('button', 'turn-fork', `${FORK_SVG}<span>Fork</span>`);
  b.type = 'button';
  b.title = 'Fork from here';
  b.dataset.forkMid = mid;
  b.onclick = () => forkChat(S.chat, mid);
  return b;
}
/* one line under a finished turn: its steps, tokens and time added up (Claude's blocks of one API call counted once) */
function paintTurnSum(turn) {
  turn.querySelector(':scope > .turnsum')?.remove();
  const seen = new Map(), steps = [];
  let prev = '';
  for (const st of turn._all || turn._stats || []) {
    // api_id: Claude's blocks of one call. Its output count grows block by block: keep the call's last (largest).
    // Older Claude chats lack it: their blocks are neighbours with the same usage.
    const usage = `${st.prompt_new}|${st.prompt_cached}|${st.out_tokens}|${st.model || ''}`;
    if (st.api_id && seen.has(st.api_id)) {
      const i = seen.get(st.api_id);
      if ((st.out_tokens || 0) >= (steps[i].out_tokens || 0)) steps[i] = st;
      continue;
    }
    const dup = !st.api_id && st.model?.startsWith('claude') && usage === prev;
    prev = usage;
    if (dup) continue;
    if (st.api_id) seen.set(st.api_id, steps.length);
    steps.push(st);
  }
  let d = null;
  if (steps.length) {
    const add = (k) => steps.reduce((a, s) => a + (s[k] || 0), 0);
    const out = add('out_tokens');
    const timed = steps.filter((s) => s.tps && s.out_tokens);
    const tw = timed.reduce((a, s) => a + s.out_tokens, 0);
    const tot = { prompt_new: add('prompt_new'), prompt_cached: add('prompt_cached'), prompt_written: add('prompt_written'), out_tokens: out, secs: Math.round(add('secs') * 10) / 10,
      tps: tw ? Math.round(timed.reduce((a, s) => a + s.tps * s.out_tokens, 0) / tw) : null, effort: steps[steps.length - 1].effort,
      model: steps[steps.length - 1].model };
    d = statsLine(tot, turn._ep);
    if (d) {
      d.classList.add('turnsum');
      if (steps.length > 1) d.insertAdjacentHTML('afterbegin', `<span>${steps.length} steps</span><span>·</span>`);
    }
  }
  // One Fork button, in the chat's header.
  if (!d) return;
  turn.appendChild(d);
}

function toolRow(call) {
  const d = el('div', 'tool');
  d.dataset.id = call.id;
  const agent = isAgent(call.name);
  const nm = agent ? 'Agent' : isComputer(call.name) ? 'Computer' : call.name.startsWith('mcp__') ? call.name.split('__').slice(1).join(' ') : call.name;
  d.innerHTML = `<button class="row"><span class="ico">${icon(agent ? 'Agent' : isComputer(call.name) ? 'Computer' : call.name.startsWith('mcp__') ? 'mcp' : call.name)}</span><span class="name">${esc(nm)}</span>` +
    `<span class="arg">${esc(toolLabel(call.name, call.arguments))}</span><span class="meta"></span><span class="st"></span></button><div class="detail"></div>`;
  d.querySelector('.row').onclick = () => { d.classList.toggle('open'); if (d.classList.contains('open')) fillDetail(d); };
  d._call = call;
  return d;
}

function diffHtml(text) {
  return (text || '').split('\n').map((l) => {
    const c = l.startsWith('+') && !l.startsWith('+++') ? 'a' : l.startsWith('-') && !l.startsWith('---') ? 'd' : l.startsWith('@@') ? 'h' : '';
    return `<div class="${c}">${esc(l) || ' '}</div>`;
  }).join('');
}

function fillDetail(d) {
  const det = d.querySelector('.detail');
  const call = d._call, res = d._res;
  const a = parseArgs(call.arguments);
  let html = '';
  if (call.name === 'Bash' || call.name === 'PowerShell') html += `<div class="lbl">Command</div><pre>${esc(a.command || '')}</pre>`;
  else if (call.name === 'Write') html += res?.ui?.diff ? '' : `<div class="lbl">Content</div><pre>${esc((a.content || '').slice(0, 20000))}</pre>`;
  else if (call.name !== 'Edit') html += `<div class="lbl">Input</div><pre>${esc((call.raw ?? JSON.stringify(a, null, 1)).slice(0, 8000))}</pre>`;
  if (res?.ui?.diff) html += `<div class="lbl">Change</div><div class="diff">${diffHtml(res.ui.diff)}</div>`;
  if (res?.ui?.image) html += `<div class="lbl">Screenshot</div><img alt="screenshot" src="/api/files/raw?path=${encodeURIComponent(res.ui.image)}">`;
  if (res) html += `<div class="lbl">Result</div><pre>${esc((res.content || '').slice(0, 30000))}</pre>`;
  else html += '<div class="lbl">Running…</div>';
  det.innerHTML = html;
  det.querySelector('img')?.addEventListener('click', (e) => zoom(e.target.src));
  if (call.subagents?.length) det.appendChild(subagentRuns(call.subagents));
}

/* an imported Agent / Task / Workflow call: its subagent runs, each opened (and fetched) on a click */
function subagentRuns(runs) {
  const box = el('div', 'subruns');
  box.appendChild(el('div', 'lbl', runs.length === 1 ? 'Subagent run' : `${runs.length} subagent runs`));
  for (const r of runs) {
    const d = el('div', 'subrun');
    const b = el('button', 'row');
    b.innerHTML = `<svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg><span class="name"></span><span class="arg"></span><span class="meta"></span>`;
    b.querySelector('.name').textContent = r.type || 'subagent';
    b.querySelector('.arg').textContent = r.description || '';
    b.querySelector('.meta').textContent = r.missing ? 'not readable' : r.tools != null ? `${r.tools} tool call${r.tools === 1 ? '' : 's'}` : r.messages != null ? `${r.messages} messages` : '';
    const body = el('div', 'subbody');
    const SUB_PAGE = 40;
    const renderSubSlice = (msgs) => {
      const frag = document.createDocumentFragment();
      for (const m of msgs) {
        if (m.role === 'user') { const u = el('div', 'msg user sub'); u.textContent = m.content || ''; frag.appendChild(u); }
        else if (m.role === 'assistant') frag.appendChild(renderAssistant(m));
        else if (m.role === 'notice') frag.appendChild(el('div', 'msg notice', esc(m.content)));
      }
      return frag;
    };
    b.onclick = async () => {
      d.classList.toggle('open');
      if (!d.classList.contains('open') || body.dataset.done || r.missing) return;
      body.dataset.done = '1';
      body.innerHTML = '<div class="muted">Loading…</div>';
      try {
        const x = await api(`/api/chats/${S.chat}/subagent?id=${encodeURIComponent(r.id)}&limit=${SUB_PAGE}`);
        body.innerHTML = '';
        let curStart = x.start || 0;
        const addMoreBtn = () => {
          if (curStart <= 0) return;
          const mb = el('button', 'link sub-more', `Show earlier (${curStart} more)`);
          mb.type = 'button';
          mb.onclick = async () => {
            mb.disabled = true;
            mb.textContent = 'Loading…';
            try {
              const prev = await api(`/api/chats/${S.chat}/subagent?id=${encodeURIComponent(r.id)}&limit=${SUB_PAGE}&before=${curStart}`);
              curStart = prev.start || 0;
              mb.remove();
              const frag = renderSubSlice(prev.messages || []);
              body.prepend(frag);
              addMoreBtn();
            } catch (e) { mb.disabled = false; mb.textContent = e.message || 'Retry'; }
          };
          body.prepend(mb);
        };
        body.appendChild(renderSubSlice(x.messages || []));
        addMoreBtn();
        if (!(x.messages || []).length) body.appendChild(el('div', 'muted', 'Nothing in it'));
      } catch (e) { body.innerHTML = `<div class="muted">${esc(e.message)}</div>`; delete body.dataset.done; }
    };
    d.append(b, body);
    box.appendChild(d);
  }
  return box;
}

function zoom(src) {
  document.querySelectorAll('.zoom').forEach((old) => old.remove());
  const z = el('div', 'zoom');
  z.setAttribute('role', 'dialog');
  z.setAttribute('aria-modal', 'true');
  z.setAttribute('aria-label', 'Image viewer');
  z.innerHTML =
    '<div class="zoom-stage"><img alt="" draggable="false"></div>' +
    '<div class="zoom-bar" role="toolbar" aria-label="Image controls">' +
      '<button type="button" class="zoom-btn" data-act="out" aria-label="Zoom out" title="Zoom out (−)">−</button>' +
      '<button type="button" class="zoom-btn zoom-pct" data-act="reset" aria-label="Reset zoom" title="Reset zoom (0)">100%</button>' +
      '<button type="button" class="zoom-btn" data-act="in" aria-label="Zoom in" title="Zoom in (+)">+</button>' +
      '<button type="button" class="zoom-btn zoom-flip" data-act="flip" aria-label="Flip landscape fullscreen" title="Landscape fullscreen (R)" aria-pressed="false">' +
        '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M16 3l3 3-3 3"/>' +
        '</svg>' +
      '</button>' +
      '<button type="button" class="zoom-btn zoom-close" data-act="close" aria-label="Close image" title="Close (Esc)">×</button>' +
    '</div>';

  const stage = z.querySelector('.zoom-stage');
  const img = z.querySelector('img');
  const bar = z.querySelector('.zoom-bar');
  const pctBtn = z.querySelector('[data-act="reset"]');
  const outBtn = z.querySelector('[data-act="out"]');
  const inBtn = z.querySelector('[data-act="in"]');
  const flipBtn = z.querySelector('[data-act="flip"]');
  img.src = src;

  let scale = 1;
  let tx = 0;
  let ty = 0;
  let landscape = false;
  const MIN_S = 1;
  const MAX_S = 8;

  const clampPan = () => {
    if (scale <= 1.001) { tx = 0; ty = 0; return; }
    const bw = img.clientWidth || 1;
    const bh = img.clientHeight || 1;
    const vw = landscape ? window.innerHeight : window.innerWidth;
    const vh = landscape ? window.innerWidth : window.innerHeight;
    const maxX = Math.max(0, (bw * scale - vw) / 2 + 48);
    const maxY = Math.max(0, (bh * scale - vh) / 2 + 48);
    tx = Math.max(-maxX, Math.min(maxX, tx));
    ty = Math.max(-maxY, Math.min(maxY, ty));
  };

  const apply = () => {
    clampPan();
    const rot = landscape ? 90 : 0;
    img.style.transform = `rotate(${rot}deg) translate(${tx}px, ${ty}px) scale(${scale})`;
    pctBtn.textContent = `${Math.round(scale * 100)}%`;
    outBtn.disabled = scale <= MIN_S + 0.01;
    inBtn.disabled = scale >= MAX_S - 0.01;
    z.classList.toggle('zoomed', scale > 1.01);
    z.classList.toggle('land', landscape);
    flipBtn.setAttribute('aria-pressed', String(landscape));
    flipBtn.classList.toggle('on', landscape);
  };

  const setScale = (next, clientX, clientY) => {
    const ns = Math.max(MIN_S, Math.min(MAX_S, next));
    if (ns <= 1.01) {
      scale = 1; tx = 0; ty = 0;
      apply();
      return;
    }
    if (clientX != null && clientY != null) {
      const cx = window.innerWidth / 2;
      const cy = window.innerHeight / 2;
      const sx = clientX - cx;
      const sy = clientY - cy;
      // Convert screen offset into image-local axes (accounting for 90deg clockwise rotation)
      const lx = landscape ? sy : sx;
      const ly = landscape ? -sx : sy;
      const ratio = ns / scale;
      tx = lx - (lx - tx) * ratio;
      ty = ly - (ly - ty) * ratio;
    }
    scale = ns;
    apply();
  };

  const toggleLandscape = () => {
    landscape = !landscape;
    scale = 1; tx = 0; ty = 0;
    apply();
  };

  const ac = new AbortController();
  const close = () => { ac.abort(); z.remove(); };
  z._close = close;

  bar.addEventListener('click', (e) => {
    e.stopPropagation();
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'in') setScale(scale * 1.5);
    else if (act === 'out') setScale(scale / 1.5);
    else if (act === 'reset') setScale(1);
    else if (act === 'flip') toggleLandscape();
    else if (act === 'close') close();
  });

  // Desktop mouse wheel zoom around cursor
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    setScale(scale * Math.exp(-e.deltaY * 0.002), e.clientX, e.clientY);
  }, { passive: false });

  // Double-click (desktop) toggles 1x <-> 2.5x
  stage.addEventListener('dblclick', (e) => {
    e.preventDefault();
    if (scale > 1.05) setScale(1);
    else setScale(2.5, e.clientX, e.clientY);
  });

  // Mouse drag-to-pan when zoomed
  let dragging = false;
  let moved = false;
  let lastX = 0;
  let lastY = 0;
  stage.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || scale <= 1.01) return;
    dragging = true; moved = false;
    lastX = e.clientX; lastY = e.clientY;
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
    lastX = e.clientX; lastY = e.clientY;
    tx += landscape ? dy : dx;
    ty += landscape ? -dx : dy;
    apply();
  }, { signal: ac.signal });
  window.addEventListener('mouseup', () => { dragging = false; }, { signal: ac.signal });

  // Touch: 2-finger pinch zoom, 1-finger pan when zoomed, double-tap toggle zoom
  let pinchDist = 0;
  let pinchScale = 1;
  let touchDragging = false;
  let touchMoved = false;
  let tLastX = 0;
  let tLastY = 0;
  let lastTapAt = 0;
  let lastTapX = 0;
  let lastTapY = 0;

  const dist2 = (t0, t1) => Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);

  stage.addEventListener('touchstart', (e) => {
    if (e.touches.length === 2) {
      e.preventDefault();
      touchDragging = false;
      pinchDist = dist2(e.touches[0], e.touches[1]) || 1;
      pinchScale = scale;
      touchMoved = true;
    } else if (e.touches.length === 1) {
      tLastX = e.touches[0].clientX;
      tLastY = e.touches[0].clientY;
      touchDragging = scale > 1.01;
      touchMoved = false;
    }
  }, { passive: false });

  stage.addEventListener('touchmove', (e) => {
    if (e.touches.length === 2 && pinchDist > 0) {
      e.preventDefault();
      const d = dist2(e.touches[0], e.touches[1]);
      const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
      const my = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      setScale(pinchScale * (d / pinchDist), mx, my);
    } else if (e.touches.length === 1 && touchDragging) {
      e.preventDefault();
      const dx = e.touches[0].clientX - tLastX;
      const dy = e.touches[0].clientY - tLastY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) touchMoved = true;
      tLastX = e.touches[0].clientX;
      tLastY = e.touches[0].clientY;
      tx += landscape ? dy : dx;
      ty += landscape ? -dx : dy;
      apply();
    }
  }, { passive: false });

  stage.addEventListener('touchend', (e) => {
    if (e.touches.length < 2) pinchDist = 0;
    if (e.touches.length === 1) {
      tLastX = e.touches[0].clientX;
      tLastY = e.touches[0].clientY;
      touchDragging = scale > 1.01;
    } else if (e.touches.length === 0) {
      touchDragging = false;
      if (!touchMoved && e.changedTouches.length === 1) {
        const now = Date.now();
        const t = e.changedTouches[0];
        if (now - lastTapAt < 300 && Math.hypot(t.clientX - lastTapX, t.clientY - lastTapY) < 40) {
          lastTapAt = 0;
          if (scale > 1.05) setScale(1);
          else setScale(2.5, t.clientX, t.clientY);
        } else {
          lastTapAt = now;
          lastTapX = t.clientX;
          lastTapY = t.clientY;
        }
      }
    }
  });

  // Tap dark backdrop (outside image, not zoomed, not after drag) closes cleanly
  stage.addEventListener('click', (e) => {
    if (moved || touchMoved) { moved = false; touchMoved = false; return; }
    if (e.target === stage && scale <= 1.01) close();
  });

  window.addEventListener('keydown', (e) => {
    if (!z.isConnected) { ac.abort(); return; }
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === '+' || e.key === '=') { e.preventDefault(); setScale(scale * 1.5); }
    else if (e.key === '-' || e.key === '_') { e.preventDefault(); setScale(scale / 1.5); }
    else if (e.key === '0') { e.preventDefault(); setScale(1); }
    else if (e.key === 'r' || e.key === 'R' || e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleLandscape(); }
  }, { signal: ac.signal });

  window.addEventListener('resize', () => {
    if (!z.isConnected) { ac.abort(); return; }
    apply();
  }, { signal: ac.signal });

  img.addEventListener('load', apply, { once: true });
  document.body.appendChild(z);
  apply();
}

function zoomVideo(src, opts = {}) {
  const prev = document.querySelector('.zoom');
  if (prev) { if (prev._close) prev._close(); else prev.remove(); }

  const from = opts.from && opts.from.tagName === 'VIDEO' ? opts.from : null;
  const startTime = from && Number.isFinite(from.currentTime) ? from.currentTime : 0;
  const wasPlaying = !!(from && !from.paused && !from.ended);
  const initMuted = !!(from && from.muted);
  const initRate = from && Number.isFinite(from.playbackRate) && from.playbackRate > 0 ? from.playbackRate : 1;
  const initVol = from && typeof from.volume === 'number' ? from.volume : 1;
  if (from && !from.paused) { try { from.pause(); } catch { /* ignore */ } }

  const z = el('div', 'zoom zoom-video');
  z.setAttribute('role', 'dialog');
  z.setAttribute('aria-modal', 'true');
  z.setAttribute('aria-label', opts.title ? 'Fullscreen video: ' + opts.title : 'Fullscreen video');

  const stage = el('div', 'zoom-stage');
  const vid = document.createElement('video');
  vid.src = src;
  vid.controls = true;
  vid.preload = 'auto';
  vid.setAttribute('playsinline', '');
  vid.setAttribute('webkit-playsinline', '');
  vid.muted = initMuted;
  try { vid.volume = initVol; } catch { /* ignore */ }
  try { vid.playbackRate = initRate; } catch { /* ignore */ }
  stage.appendChild(vid);

  const canNative = !!(vid.requestFullscreen || vid.webkitRequestFullscreen || vid.webkitEnterFullscreen);
  const bar = el('div', 'zoom-bar');
  const titleHtml = opts.title ? `<span class="zoom-title" title="${esc(opts.title)}">${esc(opts.title)}</span>` : '';
  const nativeHtml = canNative
    ? '<button type="button" class="zoom-btn zoom-native" data-act="native" aria-label="Browser fullscreen" title="Browser fullscreen">' +
      '<svg width="17" height="17" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"/></svg></button>'
    : '';
  bar.innerHTML =
    titleHtml +
    '<button type="button" class="zoom-btn zoom-flip" data-act="flip" aria-label="Flip landscape" aria-pressed="false" title="Flip landscape (R)">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<rect x="3" y="7" width="13" height="10" rx="2"/><path d="M18 4a6 6 0 0 1 3 5"/><path d="M21 5v4h-4"/></svg>' +
    '</button>' +
    nativeHtml +
    '<button type="button" class="zoom-btn zoom-close" data-act="close" aria-label="Close" title="Close (Esc)">&times;</button>';
  z.append(stage, bar);

  const flipBtn = bar.querySelector('[data-act="flip"]');
  let landscape = false;
  let syncedStart = false;

  const apply = () => {
    z.classList.toggle('land', landscape);
    flipBtn.setAttribute('aria-pressed', String(landscape));
    flipBtn.classList.toggle('on', landscape);
  };

  const toggleLandscape = () => {
    landscape = !landscape;
    apply();
  };

  const openNative = () => {
    try {
      if (document.fullscreenElement) { document.exitFullscreen?.().catch(() => {}); return; }
      if (vid.requestFullscreen) { vid.requestFullscreen().catch(() => {}); return; }
      if (vid.webkitRequestFullscreen) { vid.webkitRequestFullscreen(); return; }
      if (vid.webkitEnterFullscreen) { vid.webkitEnterFullscreen(); }
    } catch { /* fallback stays in overlay */ }
  };

  const syncStart = () => {
    if (syncedStart) return;
    syncedStart = true;
    if (startTime > 0) {
      try { vid.currentTime = startTime; } catch { /* ignore */ }
    }
    if (wasPlaying || opts.autoplay) {
      vid.play().catch(() => {});
    }
  };
  if (vid.readyState >= 1) syncStart();
  else vid.addEventListener('loadedmetadata', syncStart, { once: true });

  const ac = new AbortController();
  const close = () => {
    ac.abort();
    try {
      if (document.fullscreenElement && z.contains(document.fullscreenElement)) {
        document.exitFullscreen?.().catch(() => {});
      }
    } catch { /* ignore */ }
    if (from && from.isConnected) {
      try {
        if (Number.isFinite(vid.currentTime)) from.currentTime = vid.currentTime;
        from.muted = vid.muted;
        from.volume = vid.volume;
        from.playbackRate = vid.playbackRate;
      } catch { /* ignore */ }
    }
    try { vid.pause(); } catch { /* ignore */ }
    vid.removeAttribute('src');
    try { vid.load(); } catch { /* ignore */ }
    z.remove();
  };
  z._close = close;

  bar.addEventListener('click', (e) => {
    e.stopPropagation();
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'flip') toggleLandscape();
    else if (act === 'native') openNative();
    else if (act === 'close') close();
  });

  stage.addEventListener('click', (e) => {
    if (e.target === stage) close();
  });

  window.addEventListener('keydown', (e) => {
    if (!z.isConnected) { ac.abort(); return; }
    if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); }
    else if (e.key === 'r' || e.key === 'R') { e.preventDefault(); toggleLandscape(); }
    else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); if (canNative) openNative(); else toggleLandscape(); }
  }, { signal: ac.signal, capture: true });

  document.body.appendChild(z);
  apply();
}
KCH.zoomVideo = zoomVideo;

function finishTool(m, scope) {
  const d = findRow(scope || null, m.tool_call_id);
  if (!d) return;
  d._res = m;
  const st = d.querySelector('.st');
  st.className = 'st ' + (m.ok === false ? 'bad' : 'ok');
  if (m.ok === false) d.classList.add('failed');
  const meta = d.querySelector('.meta');
  let extra = '';
  if (m.ui?.diff) {
    const lines = m.ui.diff.split('\n');
    const plus = lines.filter((l) => l.startsWith('+') && !l.startsWith('+++')).length;
    const minus = lines.filter((l) => l.startsWith('-') && !l.startsWith('---')).length;
    extra = `<span class="plus">+${plus}</span><span class="minus">−${minus}</span>`;
  } else if (m.ui?.exit !== undefined && m.ui.exit !== 0 && m.ui.exit !== null) {
    extra = `<span class="minus">exit ${m.ui.exit}</span>`;
  }
  meta.innerHTML = extra + (m.secs != null ? `<span>${m.secs}s</span>` : '');
  if (d.classList.contains('open')) fillDetail(d);
  const g = d.closest('.acts');
  if (g) paintGroup(g);
  const info = (scope ? scope._subs : S.subs)?.[m.tool_call_id];
  if (info && isAgent(m.name || d._call?.name)) {      // after a reload the answer is the tool result, without its header
    const answer = info.answer ?? (info.status === 'done' ? String(m.content || '').replace(/^\[Subagent [^\]]*\]\s*/, '') : undefined);
    paintAgentBlock(subBlock(d), { ...info, answer });
  }
}

/* ---------------------------------------------------------------- subagents (harness/subagents.py)
   A subagent is its own embedded chat. In the parent chat it renders as a compact live status card (no wall of extra text
   cluttering the main thread); clicking it opens the subagent's chat directly so you can read, steer or prompt it. */
function findRow(scope, id) {          // scope: a subagent block (its own steps), or null for the chat itself
  if (!id) return null;
  const rows = (scope ? scope.querySelector(':scope > .sub-steps') : $('msgs')).querySelectorAll(`.tool[data-id="${CSS.escape(id)}"]`);
  for (const r of rows) if ((r.parentElement.closest('.subagent') || null) === scope) return r;
  return null;
}
function subBlock(row) {
  let b = row.querySelector(':scope > .subagent');
  if (b) return b;
  b = el('div', 'subagent running');
  b.innerHTML = '<button class="sub-head" type="button" title="Open subagent chat"><span class="sub-dot"></span><span class="sub-model"></span>' +
    '<span class="sub-desc"></span><span class="sub-stats"></span><span class="sub-state"></span>' +
    '<span class="sub-open">Open <svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg></span></button>' +
    '<div class="sub-now"></div><div class="sub-preview"></div><div class="sub-steps" hidden></div>';
  b._subs = {};
  b._st = { steps: 0, inTok: 0, outTok: 0, t0: Date.now() / 1000, status: 'running', live: '' };
  b.querySelector(':scope > .sub-head').onclick = () => {
    if (b._id) openChat(b._id);
  };
  row.insertBefore(b, row.querySelector('.detail'));
  return b;
}
function paintAgentBlock(b, info) {           // info: the record kept in the parent chat (meta.subagents) or a sub_start/sub_end event
  const st = b._st;
  if (info.id) b._id = info.id;
  if (info.model) b.querySelector(':scope > .sub-head > .sub-model').textContent = info.model;
  if (info.description) b.querySelector(':scope > .sub-head > .sub-desc').textContent = info.description;
  if (info.started) st.t0 = info.started;
  if (info.status) st.status = info.status;
  if (info.secs != null) st.secs = info.secs;
  const u = info.usage;
  if (u && u.steps != null) { st.steps = u.steps; st.inTok = (u.prompt_new || 0) + (u.prompt_cached || 0); st.outTok = u.out_tokens || 0; }
  const prev = b.querySelector(':scope > .sub-preview');
  if (prev && (info.answer !== undefined || info.error)) {
    const clean = String(info.error || info.answer || '').replace(/\s+/g, ' ').trim();
    prev.textContent = clean.length > 150 ? clean.slice(0, 150) + '...' : clean;
    prev.classList.toggle('err', !!info.error);
  }
  paintSubStats(b);
}
function paintSubStats(b) {
  const st = b._st;
  const secs = st.secs != null && st.status !== 'running' ? st.secs : Math.max(0, Date.now() / 1000 - st.t0);
  const parts = [`${st.steps} step${st.steps === 1 ? '' : 's'}`];
  if (st.inTok) parts.push(`${fmtK(st.inTok)} in`);
  if (st.outTok) parts.push(`${fmtK(st.outTok)} out`);
  parts.push(secs < 60 ? `${Math.round(secs)}s` : `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`);
  b.querySelector(':scope > .sub-head > .sub-stats').textContent = parts.join(' · ');
  let label = { running: 'Working', done: 'Done', failed: 'Failed', stopped: 'Stopped', timeout: 'Out of time' }[st.status] || st.status;
  if (st.status === 'running' && b._id && actAgo(b._id)) label += ' · ' + actAgo(b._id);
  b.querySelector(':scope > .sub-head > .sub-state').textContent = label;
  b.className = 'subagent ' + st.status;
  if (st.status !== 'running') b.querySelector(':scope > .sub-now').textContent = '';
}
setInterval(() => document.querySelectorAll('.subagent.running').forEach(paintSubStats), 1000);

function onSub(scope, ev) {            // ev: {call, sub, ev}; scope: null (this chat) or the block of the subagent that sent it
  let row = findRow(scope, ev.call);
  if (!row) {                          // no call to hang it on (a CLI that did not say which call it was): a row of its own
    row = toolRow({ id: ev.call, name: 'Agent', arguments: '{}' });
    row.querySelector('.st').className = 'st run';
    (scope ? scope.querySelector(':scope > .sub-steps') : lastTurn()).appendChild(row);
  }
  const b = subBlock(row), e = ev.ev, st = b._st, now = b.querySelector(':scope > .sub-now');
  b._id = ev.sub;
  switch (e.type) {
    case 'sub_start': st.steps = 0; st.inTok = 0; st.outTok = 0; b._live = true; paintAgentBlock(b, e); now.textContent = 'Starting'; break;
    case 'sub_end': paintAgentBlock(b, e); if (scope) scope._subs[ev.call] = e; else S.subs[ev.call] = { ...(S.subs[ev.call] || {}), ...e }; break;
    case 'step': st.live = ''; now.textContent = 'Reading'; break;
    case 'reasoning': now.textContent = 'Thinking'; break;
    case 'delta': st.live += e.text || ''; now.textContent = st.live.replace(/\s+/g, ' ').slice(-120); break;
    case 'tool_draft': now.textContent = `Writing ${e.name || 'a tool call'}`; break;
    case 'waiting': case 'retry': now.textContent = e.text || 'Waiting'; break;
    case 'activity': if (e.activity && e.activity.what) now.textContent = e.activity.what + (e.activity.line ? ' — ' + e.activity.line : ''); break;
    case 'compacting': now.textContent = 'Compacting its context'; break;
    case 'assistant': {
      const m = e.message || {}, s = m.stats || {};
      st.steps += 1; st.inTok += (s.prompt_new || 0) + (s.prompt_cached || 0); st.outTok += s.out_tokens || 0;
      if ((m.tool_calls || []).length) now.textContent = 'Running ' + m.tool_calls.map((c) => isAgent(c.name) ? 'Agent' : c.name).join(', ');
      paintSubStats(b);
      break;
    }
    case 'tool_start': { now.textContent = `Running ${isAgent(e.call?.name) ? 'Agent' : e.call?.name}`; break; }
    case 'sub': onSub(b, e); break;
    default: break;
  }
  scrollDown();
}
async function loadSubSteps(b) {       // a finished subagent's steps, from its own (hidden) chat
  if (b._live || b._loaded || !b._id) return;
  b._loaded = true;
  const STEP_PAGE = 80;
  let r;
  try { r = await api(`/api/chats/${encodeURIComponent(b._id)}?limit=${STEP_PAGE}`); } catch { b._loaded = false; return; }
  const box = b.querySelector(':scope > .sub-steps');
  box.innerHTML = '';
  b._subs = r.subagents || {};
  const appendSteps = (msgs, target) => {
    for (const m of msgs) {
      if (m.role === 'assistant') target.appendChild(renderAssistant(m));
      else if (m.role === 'tool') finishTool(m, b);
      else if (m.role === 'error' || m.role === 'notice') target.appendChild(el('div', 'msg ' + m.role, esc(m.content)));
    }
    target.querySelectorAll('.tool .st.run').forEach((s) => { s.className = 'st'; });
  };
  let curStart = r.start || 0;
  const addMore = () => {
    if (curStart <= 0) return;
    const mb = el('button', 'link sub-more', `Show earlier (${curStart} more)`);
    mb.type = 'button';
    mb.onclick = async () => {
      mb.disabled = true;
      mb.textContent = 'Loading…';
      try {
        const prev = await api(`/api/chats/${encodeURIComponent(b._id)}?limit=${STEP_PAGE}&before=${curStart}`);
        curStart = prev.start || 0;
        mb.remove();
        const wrap = el('div', 'sub-steps-older');
        appendSteps(prev.messages || [], wrap);
        box.prepend(wrap);
        addMore();
      } catch (e) { mb.disabled = false; mb.textContent = e.message || 'Retry'; }
    };
    box.prepend(mb);
  };
  appendSteps(r.messages || [], box);
  addMore();
}

/* a chat imported from ChatGPT, Claude.ai, Claude Code or Codex (harness/chat_import.py): its turns are stored as parts */
function importedNodes(m) {
  const out = [];
  (m.parts || []).forEach((p, i) => {
    if (p.kind === 'text') { const d = el('div', 'prose'); d.innerHTML = md(p.text); window.KCVis?.mountVisuals?.(d); out.push(d); }
    else if (p.kind === 'thinking') out.push(thinkBlock(p.text, false));
    else if (p.kind === 'tool') {
      const json = /^\s*[{[]/.test(p.input || '');
      const row = toolRow({ id: `${m.id}-${i}`, name: p.name || 'tool', arguments: json ? p.input : '', raw: json ? undefined : (p.input || ''), subagents: p.subagents });
      if (!json && p.input) row.querySelector('.arg').textContent = p.input.split('\n')[0].slice(0, 160);
      if (p.subagents?.length) row.querySelector('.meta').innerHTML = `<span>${p.subagents.length === 1 ? 'subagent' : p.subagents.length + ' subagents'}</span>`;
      row._res = { content: p.output ?? '', ok: !p.error, ui: p.diff ? { diff: p.diff } : undefined };
      row.querySelector('.st').className = 'st ' + (p.output === undefined ? '' : p.error ? 'bad' : 'ok');
      if (p.error) row.classList.add('failed');
      out.push(row);
    } else out.push(importedFile(p));
  });
  return out;
}
function importedFile(p) {
  if (p.kind === 'image' && p.path) {
    const img = el('img', 'imp-img');
    img.src = '/api/files/raw?path=' + encodeURIComponent(p.path);
    img.alt = p.name || 'image';
    img.title = p.name || '';
    img.onclick = () => zoom(img.src);
    return img;
  }
  const d = el('div', 'imp-file');
  const b = el(p.path ? 'button' : 'span', p.path ? 'link' : '');
  b.textContent = (p.kind === 'image' ? 'Image: ' : 'File: ') + (p.name || '') + (p.missing ? ' (not in the export)' : '');
  if (p.path) b.onclick = () => window.KCF?.openFile(p.path);
  d.appendChild(b);
  if (p.text) {
    const det = el('details');
    det.appendChild(el('summary', '', 'Its text'));
    const pre = el('pre');
    pre.textContent = p.text;
    det.appendChild(pre);
    d.appendChild(det);
  }
  return d;
}

function assistantNodes(m) {
  if (m.parts) return importedNodes(m);
  const out = [];
  if (m.reasoning_content) out.push(thinkBlock(m.reasoning_content, false));
  if (m.content) {
    const p = el('div', 'prose');
    p.innerHTML = md(m.content);
    window.KCVis?.mountVisuals?.(p);
    out.push(p);
  }
  for (const c of m.tool_calls || []) {
    const row = toolRow(c);
    row.querySelector('.st').className = 'st run';
    out.push(row);
  }
  return out;
}
/* one step as a block of its own (a subagent's steps, inside its Agent block); the chat itself uses flowAssistant */
function renderAssistant(m, step) {
  step = step || el('div', 'step');
  step.innerHTML = '';
  step.dataset.mid = m.id || '';
  for (const n of assistantNodes(m)) step.appendChild(n);
  const s = m.parts ? null : statsLine(m.stats, m.endpoint);
  if (s) step.appendChild(s);
  return step;
}

function renderMessage(m) {
  const box = $('msgs');
  const subRep = m.role === 'user' ? subReportOf(m.content) : null;
  if (subRep) {
    // A subagent's report to this chat's model is not something the user wrote: render as a compact expandable line.
    const det = el('details', 'sub-report late');
    const preview = subRep.body.replace(/\s+/g, ' ').trim().slice(0, 110);
    det.innerHTML = `<summary><span class="sub-rep-top"><span class="sub-rep-tag">Subagent report</span>` +
      `<span class="sub-rep-who">${esc(subRep.who)}${subRep.topic ? ' · ' + esc(subRep.topic) : ''}</span></span>` +
      `<span class="sub-rep-prev">${esc(preview)}</span></summary>` +
      `<div class="sub-rep-body prose">${md(subRep.body)}</div>`;
    det.dataset.id = m.id || '';
    window.KCVis?.mountVisuals?.(det);
    box.appendChild(det);
    return;
  }
  if (m.role === 'user' && m.auto) {
    const first = m.gate ? (m.content || '').split('\n')[0] : m.content;     // the gate's errors are shown in its check line
    const d = el('div', 'stats', `<span>The harness: ${esc(first)}</span>`);
    if (m.gate) d.title = m.content || '';
    lastTurn().appendChild(d);
    return;
  }
  if (m.role === 'ask') { lastTurn().appendChild(askCard(m)); return; }
  if (m.role === 'ask_answer') { const c = box.querySelector(`.ask[data-ask="${m.ask}"]`); if (c) askDone(c, m.answer, m.why); return; }
  if (m.role === 'check') { lastTurn().appendChild(checkLine(m)); return; }
  if (m.role === 'user') {
    if (m.fold_brief) {
      const det = el('details', 'm-brief');
      const metaBits = [m.brief_name || 'Brief', m.sent_when || ''].filter(Boolean).join(' · ');
      det.innerHTML = `<summary><span class="m-brief-tag">Brief</span><span class="m-brief-meta">${esc(metaBits)}</span><span class="m-brief-tog">Read</span></summary><div class="m-brief-body"></div>`;
      det.querySelector('.m-brief-body').textContent = m.content || '';
      det.addEventListener('toggle', () => { det.querySelector('.m-brief-tog').textContent = det.open ? 'Hide' : 'Read'; });
      box.appendChild(det);
      return;
    }
    const d = el('div', 'msg user');
    if (m.id) d.dataset.mid = m.id;
    if (m.sent_when || m.sent_from) {
      const metaTxt = [m.sent_when || '', m.sent_from ? `from ${m.sent_from}` : ''].filter(Boolean).join(' · ');
      d.appendChild(el('div', 'm-sent-meta', esc(metaTxt)));
    }
    const body = el('div', 'm-user-txt');
    body.textContent = m.content || '';
    d.appendChild(body);
    // sent while the model worked (the server marks it when it takes it from the queue): the turn carries on below it
    if (m.steer) {
      d.classList.add('steer');
      d.dataset.note = 'Sent while it worked';
      const t = [...box.children].reverse().find((n) => !n.classList.contains('working'));
      if (t?.classList.contains('turn')) t._cont = true;
    }
    if (m.files?.length) {
      const f = el('div', 'files');
      for (const x of m.files) f.appendChild(chipEl(x.path, null));
      d.appendChild(f);
    }
    if (m.parts) {                         // imported: attachments and images under the text
      const f = el('div', 'files imp');
      for (const p of m.parts) if (p.kind !== 'text') f.appendChild(importedFile(p));
      d.appendChild(f);
    }
    const acts = el('div', 'msg-acts');
    {
      const ed = el('button', 'iconbtn sm edit', '<svg viewBox="0 0 16 16"><path d="M10.5 2.5l3 3L6 13H3v-3z"/></svg>');
      ed.type = 'button';
      ed.title = 'Edit and send again from here';
      ed.onclick = () => editFrom(m);
      acts.appendChild(ed);
    }
    if (acts.children.length) d.appendChild(acts);
    box.appendChild(d);
  } else if (m.role === 'assistant') {
    flowAssistant(m, lastTurn());
  } else if (m.role === 'tool') {
    finishTool(m);
  } else if (m.role === 'compaction' && m.why === 'fork') {
    box.appendChild(forkBar(m));
  } else if (m.role === 'compaction') {
    const d = el('div', 'compacted', `<span>Compacted (${fmtK(m.tokens_before)} tokens summarised)</span><button class="link">Summary</button>`);
    const s = el('div', 'summary');
    s.textContent = m.summary || '';
    d.querySelector('button').onclick = () => s.classList.toggle('open');
    box.appendChild(d);
    box.appendChild(s);
  } else if (m.role === 'sub_report') {
    const det = el('details', 'sub-report');
    const who = m.sub_name || m.model || 'Subagent';
    const preview = String(m.content || '').replace(/\s+/g, ' ').trim().slice(0, 140);
    det.innerHTML = `<summary><span class="sub-rep-top"><span class="sub-rep-tag">Subagent</span>` +
      `<span class="sub-rep-who">${esc(who)}</span><span class="spacer"></span>` +
      (m.sub_id ? `<button class="link sub-rep-open" type="button">Open chat</button>` : '') +
      `</span><span class="sub-rep-prev">${esc(preview)}</span></summary>` +
      `<div class="sub-rep-body prose">${md(m.content || '')}</div>`;
    const ob = det.querySelector('.sub-rep-open');
    if (ob && m.sub_id) {
      ob.onclick = (e) => { e.preventDefault(); e.stopPropagation(); openChat(m.sub_id); };
    }
    window.KCVis?.mountVisuals?.(det);
    box.appendChild(det);
  } else if (m.role === 'group_post') {
    const node = window.KCG?.renderGroupPost?.(m);
    if (node) box.appendChild(node);
  } else if (m.role === 'error') {
    const d = el('div', 'msg error');
    d.textContent = m.content;
    box.appendChild(d);
  } else if (m.role === 'notice') {
    const d = el('div', 'msg notice');
    d.appendChild(document.createTextNode(m.content || ''));
    if (m.undo_section) {
      const u = el('button', 'link notice-undo', 'Undo');
      u.onclick = async () => {
        u.disabled = true;
        try {
          const r = await api('/api/sections/undo', { id: m.undo_section });
          if (r.ok) { u.textContent = 'Undone'; await KCH.loadChats?.(); }
          else { toast(r.error || 'Could not undo'); u.disabled = false; }
        } catch (e) { toast(e.message); u.disabled = false; }
      };
      d.appendChild(document.createTextNode(' '));
      d.appendChild(u);
    }
    box.appendChild(d);
  }
}

/* a question the harness asks while a tool waits (harness/approvals.py): one button per option, the first is the yes */
function askCard(m) {                  // looks like Computer use's question (web/computer.css .cu-ask), but stays in the chat
  const d = el('div', 'ask cu-ask control');
  d.dataset.ask = m.id;
  d.innerHTML = '<div class="cu-ic"><svg viewBox="0 0 16 16"><path d="M10.5 2.5l3 3L6 13H3v-3z"/></svg></div>' +
    '<div class="cu-tx"><b></b><span></span></div><div class="cu-btns ask-btns"></div><div class="ask-said"></div>';
  d.querySelector('.cu-tx b').textContent = m.title || 'The model is asking';
  d.querySelector('.cu-tx span').textContent = m.text || '';
  const btns = d.querySelector('.ask-btns');
  (m.options || ['Allow', 'Deny']).forEach((o, i) => {
    const b = el('button', i === 0 ? 'primary' : 'ghost');
    b.textContent = o;
    b.onclick = async () => {
      btns.querySelectorAll('button').forEach((x) => { x.disabled = true; });
      let r = {};
      try { r = await api(`/api/chats/${S.chat}/answer`, { ask: m.id, answer: o }); } catch { /* shown below */ }
      if (!r.ok && !d.classList.contains('done')) askDone(d, null, 'This question is no longer waiting.');
    };
    btns.appendChild(b);
  });
  return d;
}
function askDone(d, answer, why) {
  d.classList.add('done');
  d.querySelectorAll('.ask-btns button').forEach((b) => { b.disabled = true; b.classList.toggle('chosen', b.textContent === answer); });
  const said = { Allow: 'Allowed', Deny: 'Denied' }[answer] || answer || '';
  const note = why === 'stopped' ? ' (the turn was stopped)' : why === 'no answer' ? ' (no answer in time)' : '';
  d.querySelector('.ask-said').textContent = answer ? said + note : (why || '');
}

/* the verification gate's line under a reply (agent._gate): what it checked, and any errors */
function checkLine(m) {
  const bad = (m.files || []).filter((f) => f.status === 'error');
  const failed = bad.length || (m.test && !m.test.ok);
  const d = el('div', 'stats check ' + (failed ? 'bad' : 'ok'));
  const s = el('span');
  s.textContent = m.summary || '';
  d.appendChild(s);
  for (const f of bad) { const p = el('pre', 'check-err'); p.textContent = f.error || ''; d.appendChild(p); }
  if (m.test && !m.test.ok) { const p = el('pre', 'check-err'); p.textContent = (m.test.tail || '').slice(-1500); d.appendChild(p); }
  return d;
}

/* where an imported fork or side session came from: a line at the top of the chat, with the chat it points at */
function forkBar(m) {
  const b = S.meta?.branch || {};
  const kind = b.kind === 'side' ? 'Side session of' : b.kind === 'handoff' ? 'Handed off from' : (m || b.kind === 'fork') ? 'Forked from' : 'Came from';
  const name = b.of || b.by || 'another session';
  const target = b.of_chat || S.meta?.parent;
  const at = m?.fork_at || b.at;
  const d = el('div', 'forkbar');
  d.innerHTML = `<div class="l"><svg viewBox="0 0 16 16"><path d="M4.5 2.5v5a3.5 3.5 0 0 0 3.5 3.5h4.5M10 8.5l2.5 2.5-2.5 2.5"/></svg><span>${esc(kind)} <b></b>` +
    `${at ? ` on ${esc(new Date(at * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }))}` : ''}` +
    `${m?.before ? `, after ${m.before.toLocaleString()} messages there` : ''}</span><span class="sp"></span>` +
    (target ? '<button class="link" data-open>Open it</button>' : '') + (m ? '<button class="link" data-sum>Summary</button>' : '') + '</div>' +
    (m?.fork_after ? '<div class="after"></div>' : '');
  d.querySelector('b').textContent = name;
  if (m?.fork_after) d.querySelector('.after').textContent = `Last request there before the fork: "${m.fork_after}"`;
  d.querySelector('[data-open]')?.addEventListener('click', () => openChat(target));
  if (m) {
    const s = el('div', 'summary');
    s.textContent = m.summary || '';
    d.querySelector('[data-sum]').onclick = () => s.classList.toggle('open');
    const w = el('div');
    w.append(d, s);
    return w;
  }
  return d;
}

function renderChat(opts = {}) {
  const box = $('msgs');
  box.innerHTML = '';
  S.live = {};
  if (!S.msgs.length) {
    if (S.meta?.branch) box.appendChild(forkBar(null));
    box.appendChild(emptyState());
  } else {
    if (S.msgStart > 0) box.appendChild(earlierBtn());
    else {
      if (S.meta?.branch && !S.msgs.some((m) => m.role === 'compaction' && m.why === 'fork')) box.appendChild(forkBar(null));
    }
    for (const m of S.msgs) renderMessage(m);
    paintTurnSums(box, S.busy.has(S.chat));
    box.querySelectorAll('.ask:not(.done)').forEach((c) => { if (!(S.asks || []).includes(c.dataset.ask)) askDone(c, null, 'No answer was given.'); });
    stampLast();
  }
  if (S.busy.has(S.chat)) showWorking(true);
  if (opts.scrollTop != null) {
    const c = $('conv');
    requestAnimationFrame(() => { c.scrollTop = opts.scrollTop; });
  } else {
    scrollDown(true);
  }
}

/* a long chat opens with its newest turns; earlier ones load when the user scrolls up to this */
function earlierBtn() {
  const b = el('button', 'earlier', `Show earlier messages (${S.msgStart.toLocaleString()} more)`);
  b.onclick = loadEarlier;
  const io = new IntersectionObserver((es) => { if (es.some((x) => x.isIntersecting)) loadEarlier(); }, { root: $('conv'), rootMargin: '400px 0px 0px 0px' });
  setTimeout(() => { if (b.isConnected) io.observe(b); }, 300);
  b._io = io;
  return b;
}
let loadingEarlier = false;
async function loadEarlier() {
  if (loadingEarlier || !S.chat || S.msgStart <= 0) return;
  loadingEarlier = true;
  const id = S.chat, box = $('msgs'), conv = $('conv');
  try {
    const r = await api(`/api/chats/${id}?limit=300&before=${S.msgStart}`);
    if (id !== S.chat) return;
    const old = box.querySelector('.earlier');
    old?._io?.disconnect();
    old?.remove();
    const fromBottom = conv.scrollHeight - conv.scrollTop;
    const rest = document.createDocumentFragment();
    while (box.firstChild) rest.appendChild(box.firstChild);
    S.msgStart = r.start;
    if (r.start > 0) box.appendChild(earlierBtn());
    else {
      if (S.meta?.branch && !r.messages.some((m) => m.role === 'compaction' && m.why === 'fork')) box.appendChild(forkBar(null));
    }
    for (const m of r.messages) renderMessage(m);
    paintTurnSums(box, false);
    box.appendChild(rest);
    S.msgs = r.messages.concat(S.msgs);
    conv.scrollTop = conv.scrollHeight - fromBottom;
  } catch (e) { toast(e.message); } finally { loadingEarlier = false; }
}

function quickConnectCard() {
  const wrap = el('div', 'qc-card');
  wrap.innerHTML =
    '<div class="qc-head"><h2>Connect a model to start</h2>' +
    '<p class="muted">Plug in an API key, a local server (Ollama, LM Studio, llama.cpp), or your Claude Code / Codex / Antigravity CLI subscription. Keys stay local in <code>keys/</code> (mode 600), never in JSON.</p></div>' +
    '<div class="qc-pills" data-pills></div>' +
    '<div class="qc-form" data-form></div>';
  const pillsEl = wrap.querySelector('[data-pills]');
  const formEl = wrap.querySelector('[data-form]');
  const FEATURED = ['anthropic', 'openai', 'gemini', 'deepseek', 'openrouter', 'ollama', 'lmstudio', 'llamacpp'];
  let active = null;

  function renderForm(p) {
    active = p;
    pillsEl.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.p === p.preset));
    const isLocal = /127\.0\.0\.1|localhost/.test(p.base || '');
    const envMsg = p.env_key_present ? `<span class="qc-env">Detected in ${esc(p.key_env)}</span>` : '';
    formEl.innerHTML =
      `<div class="qc-row"><label>Provider</label><div class="qc-val"><b>${esc(p.name)}</b> ${envMsg}<div class="muted">${esc(p.help || '')}</div></div></div>` +
      (isLocal ? `<div class="qc-row"><label>Server URL</label><input type="text" data-base value="${esc(p.base || '')}" placeholder="http://127.0.0.1:11434/v1"></div>` : '') +
      `<div class="qc-row"><label>Model</label><input type="text" data-model value="${esc(p.model || '')}" placeholder="model name"></div>` +
      `<div class="qc-row"><label>API key</label><input type="password" data-key autocomplete="off" placeholder="${p.env_key_present ? `Using ${p.key_env} (paste to override)` : isLocal ? 'Optional for local servers' : `Paste your ${p.name} API key`}"></div>` +
      `<div class="qc-acts"><button type="button" class="primary" data-go>Connect &amp; start</button><button type="button" class="ghost" data-more>All providers &amp; subscriptions…</button></div>` +
      '<div class="qc-status muted" data-status></div>';
    formEl.querySelector('[data-more]').onclick = () => window.KCS?.open('connections', true);
    const goBtn = formEl.querySelector('[data-go]');
    const stEl = formEl.querySelector('[data-status]');
    goBtn.onclick = async () => {
      const model = formEl.querySelector('[data-model]').value.trim() || p.model || '';
      const key = formEl.querySelector('[data-key]').value.trim();
      const baseVal = formEl.querySelector('[data-base]')?.value.trim() || p.base;
      if (!isLocal && !key && !p.env_key_present) {
        stEl.textContent = `Paste an API key (or set ${p.key_env || 'an env var'}) to connect.`;
        formEl.querySelector('[data-key]').focus();
        return;
      }
      goBtn.disabled = true;
      stEl.textContent = 'Saving connection…';
      try {
        const epPayload = {
          preset: p.preset,
          name: p.name,
          kind: p.kind,
          base: baseVal,
          model,
          context: p.context || 128000,
          max_output: p.max_output || 16384,
          llama: !!p.llama,
          vision: !!p.vision,
          show_in_picker: true,
        };
        if (key) epPayload.key = key;
        const r = await api('/api/endpoints', { endpoint: epPayload });
        S.cfg = r.config;
        if (r.id) {
          await api('/api/config', { endpoint: r.id });
          if (S.chat) await api(`/api/chats/${S.chat}/meta`, { endpoint: r.id });
          S.cfg.endpoint = r.id;
        }
        toast(`Connected ${p.name}`);
        await KCH.repaint?.();
        $('input')?.focus();
      } catch (e) {
        stEl.textContent = e.message || 'Could not save connection';
        goBtn.disabled = false;
      }
    };
  }

  api('/api/presets').then((r) => {
    const all = r.presets || [];
    const picks = FEATURED.map((id) => all.find((x) => x.preset === id)).filter(Boolean);
    pillsEl.innerHTML = '';
    for (const p of picks) {
      const b = el('button', 'qc-pill', esc(p.name.replace(/ \(.*\)$/, '')) + (p.env_key_present ? ' •' : ''));
      b.type = 'button';
      b.dataset.p = p.preset;
      b.title = p.env_key_present ? `${p.name} (${p.key_env} detected)` : p.name;
      b.onclick = () => renderForm(p);
      pillsEl.appendChild(b);
    }
    const firstEnv = picks.find((x) => x.env_key_present);
    if (picks.length) renderForm(firstEnv || picks[0]);
  }).catch(() => {
    const b = el('button', 'primary', 'Add a connection');
    b.onclick = () => window.KCS?.open('connections', true);
    formEl.appendChild(b);
  });
  return wrap;
}

function emptyState() {
  if (S.meta?.mode === 'group' && window.KCG?.emptyStateEl) {
    return window.KCG.emptyStateEl(S.meta);
  }
  if (!S.cfg?.endpoints?.length) {           // first run: nothing connected yet
    const d = el('div', 'empty-chat empty-onboard');
    d.appendChild(quickConnectCard());
    return d;
  }
  const ep = curEp();
  const needsKey = ep && ['openai', 'anthropic', 'opencode-go'].includes(ep.kind) && !ep.has_key && !/127\.0\.0\.1|localhost/.test(ep.base || '');
  const ideas = ['Build a landing page for a coffee shop', 'Find and fix the bug in…', 'Explain this project to me', 'Make a chart comparing performance metrics'];
  const d = el('div', 'empty-chat', `<h2>What are we building?</h2><div>Working in <b>${esc(base(S.project))}</b> with ${esc(ep?.name || 'the model')}.</div><div class="ideas"></div>`);
  if (needsKey) {
    const warn = el('div', 'qc-warn');
    warn.innerHTML = `<span><b>${esc(ep.name)}</b> needs an API key before sending a message.</span> <button type="button" class="primary">Add API key</button>`;
    warn.querySelector('button').onclick = () => window.KCS?.open('connections');
    d.appendChild(warn);
  }
  for (const i of ideas) {
    const b = el('button', '', esc(i));
    b.onclick = () => { $('input').value = i; $('input').focus(); autoSize(); };
    d.querySelector('.ideas').appendChild(b);
  }
  return d;
}

let stickBottom = true;
$('conv').addEventListener('scroll', () => {
  const c = $('conv');
  stickBottom = c.scrollHeight - c.scrollTop - c.clientHeight < 80;
});
function scrollDown(force) {
  const c = $('conv');
  if (force || stickBottom) requestAnimationFrame(() => { c.scrollTop = c.scrollHeight; });
}

function showWorking(on, text) {
  let w = $('msgs').querySelector('.working');
  if (!on) { w?.remove(); return; }
  if (!w) {
    w = el('div', 'working', '<span class="bars"><i></i><i></i><i></i></span><span class="tx"></span><span class="wl"></span>');
    $('msgs').appendChild(w);
  } else {
    $('msgs').appendChild(w);           // keep it last
  }
  w._text = text || 'Working';
  paintWorking(w);
}

/* What a working chat is doing (harness/activity.py): the step running longest and since when, its last output line,
   or how long since its last step. Times come as seconds from the app ("ago"), never compared with this device's clock. */
S.act = {};
function setAct(cid, a) {
  if (a) S.act[cid] = { ...a, at: Date.now() - (a.ago || 0) * 1000 };
  else S.act[cid] = { what: '', at: Date.now(), idle: true };
}
function agoText(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return '';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm';
  return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0');
}
function actAgo(cid) {                   // '' under a minute: a chat that is moving needs no number
  const a = S.act[cid];
  return a ? agoText(Date.now() - a.at) : '';
}
function actLabel(cid) {                 // for titles and chips: "Bash: python bench.py, 31m" / "last step 14m ago"
  const a = S.act[cid];
  if (!a) return '';
  const ago = actAgo(cid);
  if (a.what) return a.what + (ago ? ' · ' + ago : '');
  return ago ? 'last step ' + ago + ' ago' : '';
}
// A chat that is free but still has background work running in its session (a Neon command or timer over acp):
// the list says "bg · 12m" instead of nothing, so a free chat with a job on is not mistaken for idle or stuck.
function bgChats() { return Object.keys(S.act).filter((c) => S.act[c].bg && !S.busy.has(c)); }
function bgLabel(cid) {
  const a = S.act[cid];
  if (!a || !a.bg || S.busy.has(cid)) return '';
  return 'bg' + (actAgo(cid) ? ' · ' + actAgo(cid) : '');
}
KCH.actAgo = actAgo; KCH.actLabel = actLabel; KCH.bgLabel = bgLabel;
function paintWorking(w) {
  w = w || $('msgs').querySelector('.working');
  if (!w) return;
  const a = S.act[S.chat], ago = actAgo(S.chat);
  let tx = w._text || 'Working', line = '';
  if (a && a.what) {
    tx = a.what + (ago ? ' · ' + ago : '');
    if (a.left > 0 && a.until) tx += ' (' + Math.round(a.left / 60) + 'm left)';
    line = a.line || '';
  } else if (ago) {
    tx = tx + ' · last step ' + ago + ' ago';
  }
  w.querySelector('.tx').textContent = tx;
  const wl = w.querySelector('.wl');
  if (wl) { wl.textContent = line; wl.title = line; }
}
setInterval(() => { if (S.busy.size) { paintWorking(); paintList(); } }, 15000);

/* live streaming of one model step */
function liveStep(id) {
  if (S.live[id]) return S.live[id];
  $('msgs').querySelector('.empty-chat')?.remove();
  const L = S.live[id] = { turn: lastTurn(), text: '', reasoning: '', think: null, prose: null, drafts: {}, t: 0 };
  showWorking(true, 'Reading');
  return L;
}
function paintLive(L) {
  if (L.t) return;
  L.t = requestAnimationFrame(() => {
    L.t = 0;
    if (L.reasoning) {
      if (!L.think) { L.think = thinkBlock('', true); flowNode(L.turn, L.think); }
      setThought(L.think, L.reasoning);
      const b = L.think.querySelector('.body');
      b.scrollTop = b.scrollHeight;
      const g = L.think.closest('.acts');
      if (g) paintGroup(g);
    }
    if (L.text) {
      if (!L.prose) { L.prose = el('div', 'prose'); flowNode(L.turn, L.prose); }
      const html = md(L.text, { streaming: true });
      if (window.KCVis?.patchLive) window.KCVis.patchLive(L.prose, html);
      else L.prose.innerHTML = html;
      L.prose.lastElementChild?.classList.add('cursor');
    }
    scrollDown();
  });
}

/* ---------------------------------------------------------------- events */
let connectedOnce = false;
let _es = null;
let _esRetry = null;
let _lastSseAt = 0;
let _hiddenAt = 0;
let _resumeTimer = null;
let _catchUpP = null;

function lastKnownMsgId() {
  for (let i = (S.msgs || []).length - 1; i >= 0; i--) {
    if (S.msgs[i] && S.msgs[i].id) return S.msgs[i].id;
  }
  return null;
}

function hasMsgId(id) {
  if (!id) return false;
  for (let i = (S.msgs || []).length - 1; i >= Math.max(0, (S.msgs || []).length - 60); i--) {
    if (S.msgs[i] && S.msgs[i].id === id) return true;
  }
  return false;
}

function dropMatchingLive(m) {
  if (!m || m.role !== 'assistant') return null;
  const content = String(m.content || '').trim();
  const thought = String(m.reasoning_content || '').trim();
  for (const [k, L] of Object.entries(S.live)) {
    const lt = String(L.text || '').trim();
    const lr = String(L.reasoning || '').trim();
    const textMatch = lt && content && (content === lt || content.startsWith(lt) || lt.startsWith(content));
    const thinkMatch = !lt && lr && thought && (thought === lr || thought.startsWith(lr));
    if (textMatch || thinkMatch) {
      cancelAnimationFrame(L.t);
      unflow(L.think);
      unflow(L.prose);
      delete S.live[k];
      return L.turn;
    }
  }
  return null;
}

function applyMissedMessage(m) {
  if (!m || (m.id && hasMsgId(m.id))) return false;
  S.msgs.push(m);
  if (m.role === 'assistant') {
    const matchedTurn = dropMatchingLive(m);
    flowAssistant(m, matchedTurn || lastTurn());
    showWorking(!!(m.tool_calls || []).length, 'Running tools');
    if (m.stats) S.lastStats = m.stats;
  } else if (m.role === 'user') {
    $('msgs').querySelector('.empty-chat')?.remove();
    renderMessage(m);
  } else {
    renderMessage(m);
    if (m.role === 'tool' && /^(Write|Edit)$/.test(m.name)) window.KCF?.refreshSoon();
  }
  return true;
}

async function catchUpChat(cid, opts = {}) {
  if (!cid || cid !== S.chat) return false;
  if (_catchUpP) return _catchUpP;
  _catchUpP = (async () => {
    const afterId = opts.forceFull ? null : lastKnownMsgId();
    if (!afterId || String(cid).startsWith('neon-')) {
      if (!Object.keys(S.live).length) await openChat(cid, { preserveScroll: true }).catch(() => {});
      return true;
    }
    let r;
    try {
      r = await api(`/api/chats/${encodeURIComponent(cid)}?after=${encodeURIComponent(afterId)}`);
    } catch { return false; }
    if (cid !== S.chat || !r) return false;
    if (r.truncated || !Array.isArray(r.messages)) {
      if (!Object.keys(S.live).length) await openChat(cid, { preserveScroll: true }).catch(() => {});
      return true;
    }
    let added = 0;
    for (const m of r.messages) {
      if (applyMissedMessage(m)) added += 1;
    }
    if (typeof r.total === 'number' && r.total > (S.msgTotal || 0)) S.msgTotal = r.total;
    if (r.meta) {
      const prevCtx = S.meta?.ctx;
      S.meta = { ...(S.meta || {}), ...r.meta };
      if (r.meta.ctx && r.meta.ctx !== prevCtx) paintCtx();
      if (r.meta.todos) paintTodos(r.meta.todos);
      if (r.meta.mode === 'group') window.KCG?.paintGroupBar?.();
    }
    if (Array.isArray(r.queued)) { S.queue = r.queued; paintQueue(); }
    if (added > 0) {
      if (typeof stampLast === 'function') stampLast();
      scrollDown();
    }
    return added > 0;
  })();
  try { return await _catchUpP; } finally { _catchUpP = null; }
}

function connect() {
  if (_esRetry) { clearTimeout(_esRetry); _esRetry = null; }
  if (_es) { try { _es.close(); } catch { /* ignore */ } _es = null; }
  const es = new EventSource('/api/events');
  _es = es;
  es.addEventListener('ping', () => { _lastSseAt = Date.now(); });
  es.onmessage = (e) => {
    _lastSseAt = Date.now();
    try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
  es.onerror = () => {
    if (_es !== es) return;
    es.close();
    _es = null;
    if (!_esRetry) _esRetry = setTimeout(connect, 2000);
  };
  // after a reconnect (the app restarted, the network blipped) events may have been missed: ask the app what runs
  es.onopen = () => {
    if (_es !== es) return;
    _lastSseAt = Date.now();
    if (connectedOnce) { syncBusy(true); window.KCC?.load(); KCH.onReconnect?.(); }
    connectedOnce = true;
  };
}

function reconnectEvents(force = false) {
  const stale = !_es || _es.readyState !== EventSource.OPEN || (_lastSseAt > 0 && Date.now() - _lastSseAt > 15000);
  if (force || stale) connect();
}

function onAppResume(why) {
  if (_resumeTimer) clearTimeout(_resumeTimer);
  _resumeTimer = setTimeout(() => {
    _resumeTimer = null;
    if (document.visibilityState === 'hidden') return;
    const wasHiddenMs = _hiddenAt ? (Date.now() - _hiddenAt) : 0;
    _hiddenAt = 0;
    const sseStale = !_es || _es.readyState !== EventSource.OPEN || (_lastSseAt > 0 && Date.now() - _lastSseAt > 12000);
    if (why === 'online' || sseStale || wasHiddenMs > 5000) reconnectEvents(true);
    syncBusy(true);
    KCH.onResume?.(why);
  }, 150);
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') { _hiddenAt = Date.now(); }
  else if (connectedOnce) { onAppResume('visible'); }
});
window.addEventListener('pageshow', (e) => { if (e.persisted || connectedOnce) onAppResume('pageshow'); });
window.addEventListener('focus', () => { if (connectedOnce) onAppResume('focus'); });
window.addEventListener('online', () => onAppResume('online'));

// The page's idea of which chats are working comes from events; check it against the app so a lost turn_end or missed
// SSE events while a Neon ACP chat stays busy never leave the page showing stale messages until the next send.
async function syncBusy(reload) {
  let r;
  try { r = await api('/api/busy' + (S.chat ? `?chat=${encodeURIComponent(S.chat)}` : '')); } catch { return; }
  const now = new Set([...(r.chats || []), ...(r.subagents || [])]);
  for (const [cid, a] of Object.entries(r.activity || {})) setAct(cid, a);
  for (const cid of Object.keys(S.act)) if (S.act[cid].bg && !(r.background || {})[cid] && !now.has(cid)) delete S.act[cid];
  for (const [cid, a] of Object.entries(r.background || {})) setAct(cid, a);
  const wasMine = S.busy.has(S.chat);
  for (const id of [...S.busy]) if (!now.has(id)) onEvent({ type: 'turn_end', chat: id });
  for (const id of now) S.busy.add(id);
  const tail = S.chat ? (r.tails || {})[S.chat] : null;
  const curLast = lastKnownMsgId();
  const tailMoved = !!(tail && (
    (tail.last_id && tail.last_id !== curLast) ||
    (typeof tail.total === 'number' && tail.total > (S.msgTotal || S.msgs.length))
  ));
  if (S.chat && (tailMoved || reload || (wasMine && !now.has(S.chat)))) {
    await catchUpChat(S.chat).catch(() => {});
  }
  paintList(); setBusy(); paintSubChrome();
}
setInterval(() => {
  if (document.visibilityState === 'hidden') return;
  if (_lastSseAt > 0 && Date.now() - _lastSseAt > 20000) reconnectEvents(true);
  if (S.busy.size || bgChats().length || S.chat) syncBusy(false);
}, 12000);
KCH.syncBusy = syncBusy;
KCH.catchUpChat = catchUpChat;
KCH.reconnectEvents = reconnectEvents;
KCH.onAppResume = onAppResume;

function onEvent(ev) {
  if (ev.chat && ev.type !== 'activity' && ev.type !== 'turn_end') {   // anything it shows is a step: "last step" restarts
    const a = S.act[ev.chat];
    if (!a || !a.what) S.act[ev.chat] = { what: '', at: Date.now(), idle: true };
  }
  if (ev.type === 'runtime') { runtimeEvent(ev.runtime); return; }
  if (ev.type === 'statuses' && ev.statuses) { S.statuses = ev.statuses; paintModel(); KCH.onStatuses?.(); return; }
  if (ev.type === 'file') { window.KCF?.onFileEvent(ev); return; }
  if (ev.type === 'sections_changed') { KCH.loadChats?.(); return; }
  if (ev.type === 'burn_alert') {
    S.burnAlerts = Array.isArray(ev.alerts) ? ev.alerts : (ev.alert ? [ev.alert] : []);
    paintBurnBanner();
    return;
  }
  if (ev.type === 'external_sync') {
    KCH.loadChats?.();
    const hit = ev.all || (Array.isArray(ev.chats) && S.chat && ev.chats.includes(S.chat));
    if (hit && S.chat) {
      catchUpChat(S.chat).catch(() => {});
    }
    return;
  }
  if (ev.type === 'subagents_changed') {
    if (S.cfg?.subagents) S.cfg.subagents.presets = ev.presets || [];
    KCH.onSubagentsChanged?.(ev.presets || []);
    return;
  }
  if (ev.type === 'sub_chip') {
    if (ev.chat && ev.chat === S.chat && ev.chip) {
      const idx = (S.embeddedSubs || []).findIndex((x) => x.id === ev.chip.id);
      if (idx >= 0) S.embeddedSubs[idx] = ev.chip;
      else S.embeddedSubs.unshift(ev.chip);
      S.embeddedSubs.sort((a, b) => (a.busy === b.busy ? (b.updated || 0) - (a.updated || 0) : a.busy ? -1 : 1));
      paintSubChrome();
      KCH.onSubChip?.(ev.chip);
    }
    return;
  }
  if (ev.type === 'neon_update') {
    if (ev.running) S.busy.add(ev.chat); else S.busy.delete(ev.chat);
    if (ev.chat === S.chat) openChat(S.chat, { preserveScroll: true }).catch(() => {});
    if (S.meta?.sub_of && ev.chat === S.meta.sub_of && S.parentInfo) {
      S.parentInfo.busy = !!ev.running;
      paintSubChrome();
    }
    KCH.loadChats?.();
    setBusy();
    return;
  }
  if (ev.type?.startsWith('computer_')) { window.KCC?.onEvent(ev); return; }     // computer.js: questions, banner
  if (ev.type?.startsWith('group_') && window.KCG?.onEvent?.(ev)) return;        // group-chat.js: live room state & activity
  const mine = ev.chat && ev.chat === S.chat;
  const isParent = !!(S.meta?.sub_of && ev.chat === S.meta.sub_of);
  switch (ev.type) {
    case 'step':
      S.busy.add(ev.chat); paintList(); setBusy();
      if (isParent && S.parentInfo) { S.parentInfo.busy = true; paintSubChrome(); }
      if (mine) liveStep(ev.id);
      break;
    case 'delta':
      if (mine) { const L = liveStep(ev.id); L.text += ev.text; showWorking(false); paintLive(L); }
      break;
    case 'reasoning':
      if (mine) { const L = liveStep(ev.id); L.reasoning += ev.text; showWorking(true, 'Thinking'); paintLive(L); }
      break;
    case 'retry':
      if (mine) showWorking(true, `The model is busy, trying again (${ev.attempt})`);
      break;
    case 'tool_draft':
      if (mine) showWorking(true, `Writing ${ev.name || 'a tool call'}${ev.chars > 400 ? ` (${fmtK(ev.chars)} characters)` : ''}`);
      break;
    case 'assistant':
      if (mine) {
        if (ev.message?.id && hasMsgId(ev.message.id)) {
          const L0 = S.live[ev.temp];
          if (L0) { cancelAnimationFrame(L0.t); unflow(L0.think); unflow(L0.prose); delete S.live[ev.temp]; }
          break;
        }
        S.msgs.push(ev.message);
        const L = S.live[ev.temp];
        if (L) { cancelAnimationFrame(L.t); unflow(L.think); unflow(L.prose); flowAssistant(ev.message, L.turn); delete S.live[ev.temp]; }
        else flowAssistant(ev.message, lastTurn());
        showWorking(!!(ev.message.tool_calls || []).length, 'Running tools');
        scrollDown();
      }
      if (ev.ctx && mine) { S.meta = { ...S.meta, ctx: ev.ctx }; paintCtx(); }
      if (mine) S.lastStats = ev.message.stats;
      break;
    case 'stats':                    // a Claude call's final token count, after its blocks: into this turn's total
      if (mine && ev.message?.stats) { S.msgs.push(ev.message); (lastTurn()._stats ||= []).push(ev.message.stats); }
      break;
    case 'tool_start':
      if (mine) {
        const d = findRow(null, ev.call.id);
        if (d) { d.querySelector('.st').className = 'st run'; const g = d.closest('.acts'); if (g) paintGroup(g); }
        showWorking(true, `Running ${ev.call.name}`);
      }
      break;
    case 'message':
      if (mine) {
        const m = ev.message;
        if (m?.id && hasMsgId(m.id)) {
          if (ev.queued_id) dropQueued(ev.queued_id);
          break;
        }
        S.msgs.push(m);
        if (ev.queued_id) dropQueued(ev.queued_id);       // the model has it now: it is the user's message in the chat
        if (m.role === 'user') { $('msgs').querySelector('.empty-chat')?.remove(); renderMessage(m); scrollDown(true); }
        else renderMessage(m);
        if (m.role === 'group_post') {
          const w = $('msgs').querySelector('.working');
          if (w) {
            if (S.busy.has(S.chat)) $('msgs').appendChild(w);
            else w.remove();
          }
        }
        if (m.role === 'user' || m.role === 'assistant' || m.role === 'group_post') stampLast();
        if (m.role === 'tool' && /^(Write|Edit)$/.test(m.name)) window.KCF?.refreshSoon();
        scrollDown(m.role === 'ask');
      } else if (ev.message?.role === 'ask') {
        const c = S.chats.find((x) => x.id === ev.chat);
        toast(`"${c?.title || 'A chat'}" is waiting for your answer: ${ev.message.title || ''}`, 8000);
      }
      break;
    case 'sub':
      if (mine) onSub(null, ev);
      break;
    case 'queued':
      if (mine && !_droppedQueued.has(ev.id) && !(S.queue || []).some((q) => q.id === ev.id)) { (S.queue ||= []).push(ev); paintQueue(); }
      break;
    case 'unqueued':
      if (mine) dropQueued(ev.id);
      break;
    case 'todos':
      if (mine) paintTodos(ev.todos);
      break;
    case 'notes':
      if (mine) window.KCF?.setChatNotes(ev.text);
      break;
    case 'title':
      { const c = S.chats.find((x) => x.id === ev.chat); if (c) { c.title = ev.title; paintList(); } if (mine) $('chatTitle').textContent = ev.title; }
      break;
    case 'compacting':
      if (mine) showWorking(true, 'Compacting the chat');
      break;
    case 'waiting':
      S.busy.add(ev.chat); paintList(); setBusy();
      if (mine) showWorking(true, ev.text);
      break;
    case 'activity': {
      const wasBg = !!S.act[ev.chat]?.bg;
      setAct(ev.chat, ev.activity);
      if (mine) paintWorking();
      if (!S.busy.has(ev.chat) && (wasBg || ev.activity?.bg)) paintList();   // a free chat's background work changed
      break;
    }
    case 'step_cancelled':
      if (mine) {
        const L = S.live[ev.id];
        if (L) { L.prose?.lastElementChild?.classList.remove('cursor'); L.think?.classList.remove('live'); unflowEmpty(L); delete S.live[ev.id]; }
      }
      break;
    case 'turn_end':
      S.busy.delete(ev.chat);
      if (isParent && S.parentInfo) {
        const wasBusy = S.parentInfo.busy;
        S.parentInfo.busy = false;
        paintSubChrome();
        if (wasBusy) toast('Main chat finished');
      }
      if (mine) {
        showWorking(false);
        if (ev.meta) { S.meta = { ...S.meta, ...ev.meta }; paintCtx(); if (S.meta.mode === 'group') window.KCG?.paintGroupBar?.(); }
        if (ev.log !== undefined) window.KCF?.setChatLog(ev.log);
        for (const id in S.live) { S.live[id].prose?.lastElementChild?.classList.remove('cursor'); }
        S.live = {};
        document.querySelectorAll('.tool .st.run').forEach((s) => { s.className = 'st'; });
        document.querySelectorAll('.subagent.running').forEach((b) => { b._st.status = 'stopped'; paintSubStats(b); });
        document.querySelectorAll('#msgs .think.live').forEach((t) => { t.classList.remove('live'); t.querySelector('button span').textContent = 'Thought'; setThought(t, t.querySelector('.body').textContent); });
        document.querySelectorAll('#msgs .acts').forEach(paintGroup);
        paintTurnSums($('msgs'), false);
        if ((S.queue || []).length) {
          const left = S.queue.filter((q) => q.readonly || q.when === 'neon' || q.when === 'slot');
          if (left.length !== S.queue.length) { S.queue = left; paintQueue(); }
        }
      }
      { const c = S.chats.find((x) => x.id === ev.chat); if (c) c.updated = Date.now() / 1000; }
      paintList(); setBusy();
      break;
    default:
      break;
  }
}

function setBusy() {
  const b = S.busy.has(S.chat);
  const hasDraft = !!($('input').value.trim() || S.chips.length);
  $('stopBtn').hidden = !b;
  $('sendBtn').hidden = b && !hasDraft;
  $('sendBtn').title = b ? 'Steer (send while working)' : 'Send (Enter)';
  document.body.classList.toggle('busy', S.busy.size > 0);
  const rs = $('runState');
  const ep = curEp();
  const intEng = ep?.kind === 'codex' || ep?.kind === 'agy';
  rs.textContent = (b && !document.body.classList.contains('phone')) ? (intEng
    ? 'Working… type to steer (interrupts and resumes with your message). Esc stops.'
    : 'Working… type to steer (reaches the model at its next step). Esc stops.') : '';
  rs.classList.toggle('on', b);
  KCH.onBusyChange?.(b);
}

/* ---------------------------------------------------------------- chats list */
function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  return diff === 0 ? 'Today' : diff === 1 ? 'Yesterday' : diff < 7 ? d.toLocaleDateString(undefined, { weekday: 'long' })
    : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
}
let _listDirty = false;
let _listTimer = null;
function _doPaintList() {
  clearTimeout(_listTimer);
  _listTimer = null;
  if (document.body.classList.contains('phone') && KCH.sideShown && !KCH.sideShown()) {
    _listDirty = true;
    return;
  }
  _listDirty = false;
  window.KCL?.paint();
}
function paintList(immediate = false) {
  if (immediate) return _doPaintList();
  if (document.body.classList.contains('phone') && KCH.sideShown && !KCH.sideShown()) {
    _listDirty = true;
    return;
  }
  if (_listTimer) return;
  _listTimer = setTimeout(_doPaintList, 140);
}
KCH.flushList = () => { if (_listDirty) { _listDirty = false; clearTimeout(_listTimer); _listTimer = null; window.KCL?.paint(); } };
$('chatSearch').addEventListener('input', (e) => { S.filter = e.target.value; paintList(true); });

async function loadChats() {
  const r = await api('/api/chats?all=1');
  S.chats = r.chats || [];
  S.groups = r.groups || [];
  if (r.sections) S.sections = r.sections;
  if (r.sources_synced) S.sourcesSynced = r.sources_synced;
  paintList(true);
  sayTrouble(r.trouble);
}
window.loadChats = loadChats;
KCH.loadChats = loadChats;

// The app leaves out a part of the list it could not read instead of failing whole: say so once per problem.
function sayTrouble(list) {
  const text = (list || []).map((t) => t.part).join(', ');
  if (!text) { sayTrouble.last = ''; return; }
  if (text === sayTrouble.last) return;
  sayTrouble.last = text;
  toast('Some of the list could not be read (' + text + '). Everything else works, and it has been logged.', 9000);
}

const PAGE = 80;                 // messages a chat opens with; earlier ones come as the user scrolls up
let openSeq = 0;
async function openChat(id, opts = {}) {
  if (!id) return newChat();
  const t0 = performance.now();
  const sameChat = id === S.chat;
  const keepPos = opts.preserveScroll && sameChat && !stickBottom ? $('conv').scrollTop : null;
  const seq = ++openSeq;
  let loadMark = null;
  if (!sameChat) {
    const known = (S.chats || []).find((c) => c.id === id)
      || (S.embeddedSubs || []).find((c) => c.id === id)
      || (S.parentInfo?.id === id ? S.parentInfo : null);
    if (known?.title || known?.name) $('chatTitle').textContent = known.title || known.name;
    loadMark = setTimeout(() => { if (seq === openSeq) $('chatHead')?.classList.add('loading'); }, 80);
  }
  let r;
  try {
    r = await api(`/api/chats/${id}?limit=${PAGE}`);
  } finally {
    if (loadMark) clearTimeout(loadMark);
    if (seq === openSeq) $('chatHead')?.classList.remove('loading');
  }
  if (seq !== openSeq) return;
  const tFetch = performance.now() - t0;
  const tRender0 = performance.now();
  S.chat = id;
  S.meta = r.meta;
  S.msgs = r.messages;
  S.subs = r.subagents || {};
  S.embeddedSubs = r.embedded_subagents || [];
  S.parentInfo = r.parent_info || null;
  S.asks = r.asks || [];
  S.queue = r.queued || [];
  paintQueue();
  S.msgStart = r.start || 0;
  S.msgTotal = r.total || r.messages.length;
  if (r.busy) S.busy.add(id); else S.busy.delete(id);
  if (!r.meta.sub_of && !r.meta.group_member_of) LS.set('chat:' + S.project, id);
  $('chatTitle').textContent = r.meta.title || 'New chat';
  const acts = document.querySelector('#chatHead .acts');
  if (acts) {
    const restricted = !!r.meta.sub_of || !!r.meta.group_member_of;
    acts.hidden = false;
    for (const bid of ['subHeadBtn', 'compactBtn', 'renameBtn', 'deleteBtn']) {
      const btn = $(bid);
      if (btn) btn.hidden = restricted;
    }
    const fbtn = $('forkBtn');
    if (fbtn) fbtn.hidden = false;
  }
  paintSub();
  paintSubChrome();
  window.KCF?.setChatNotes(r.notes || '');
  window.KCF?.setChatLog(r.log || '');
  paintTodos(r.meta.todos || []);
  renderChat(keepPos != null ? { scrollTop: keepPos } : {});
  paintList(true);
  paintModel();
  paintReason();
  paintCtx();
  setBusy();
  KCH.onOpenChat?.(id, r.meta);
  window.KCG?.onOpenChat?.(id, r.meta, r.group_live);
  const tRender = performance.now() - tRender0;
  const tTotal = performance.now() - t0;
  if (tTotal >= SLOW_MS) reportSlow('open', id, tTotal, `fetch=${Math.round(tFetch)}ms render=${Math.round(tRender)}ms msgs=${(r.messages || []).length}`);
  if (!document.body.classList.contains('phone') && !opts.preserveScroll) $('input').focus();
}

/* under the title: the project, and the group the chat is in */
function paintSub() {
  const g = (S.groups || []).find((x) => x.id === (S.chats.find((c) => c.id === S.chat)?.group ?? S.meta?.group));
  $('chatSub').textContent = base(S.meta?.project) + (g ? ` · ${g.name}` : '');
}

/* ---------------------------------------------------------------- embedded subagent bar & thread strip */
const subBackBar = el('div', 'sub-backbar');
subBackBar.id = 'subBackBar';
subBackBar.hidden = true;
const subStrip = el('div', 'sub-strip');
subStrip.id = 'subStrip';
subStrip.hidden = true;
$('chatHead')?.after(subBackBar, subStrip);

const subHeadBtn = el('button', 'link', 'Subagents');
subHeadBtn.id = 'subHeadBtn';
subHeadBtn.title = 'Open or start an embedded subagent chat';
subHeadBtn.onclick = () => KCH.openSubagentsSheet?.();
document.querySelector('#chatHead .acts')?.prepend(subHeadBtn);

async function spawnSubagent(opts = {}) {
  let parentId = opts.parent || S.chat;          // "this chat", not the main chat when inside a subagent
  if (!parentId) {
    await newChat();
    parentId = S.chat;
  }
  const r = await api(`/api/chats/${encodeURIComponent(parentId)}/subagents`, {
    model: opts.model || null,
    preset_id: opts.preset_id || null,
    name: opts.name || '',
    instructions: opts.instructions || '',
    prompt: opts.prompt || '',
    fork_context: opts.fork_context !== false,
    effort: opts.effort || '',
  });
  if (r.id) {
    await openChat(r.id);
    $('input')?.focus();
  }
  return r;
}

function latestSubagentReply() {
  for (let i = (S.msgs || []).length - 1; i >= 0; i--) {
    const m = S.msgs[i];
    if (m.role === 'assistant' && (m.content || '').trim()) return m.content.trim();
  }
  return '';
}

function openReportMainDialog() {
  KCH.openReportMainSheet?.();
}

function openSubagentsDialog() {
  KCH.openSubagentsSheet?.();
}

function paintSubChrome() {
  const parentId = S.meta?.sub_of || S.meta?.group_member_of;
  const isSub = !!S.meta?.sub_of;
  const isGroupTrace = !isSub && !!S.meta?.group_member_of;
  subBackBar.hidden = !parentId;
  if (parentId) {
    const pTitle = S.parentInfo?.title || (isGroupTrace ? 'Group chat' : 'Main chat');
    const pBusy = !!(S.parentInfo?.busy || S.busy.has(parentId));
    const ctxTag = isGroupTrace ? 'Group trace' : (S.meta.sub_forked ? `Forked${S.meta.sub_ctx_k ? ` ~${S.meta.sub_ctx_k}k` : ''}` : 'Subagent');
    const backTitle = pBusy ? `Back to ${pTitle} (working)` : `Back to ${pTitle}`;
    subBackBar.innerHTML = `<button class="sub-back-btn${pBusy ? ' busy' : ''}" type="button" data-back title="${esc(backTitle)}" aria-label="${esc(backTitle)}"><svg viewBox="0 0 16 16"><path d="M10 3.5L5.5 8 10 12.5"/></svg>${pBusy ? '<i class="sub-dot run sub-back-busy-dot" aria-hidden="true"></i>' : ''}<span>${esc(pTitle)}</span></button>` +
      `<span class="sub-back-meta">${pBusy ? '<span class="sub-main-busy"><i class="sub-dot run"></i>Working</span>' : `<span class="sub-ctx-tag">${esc(ctxTag)}</span>`}</span>` +
      (isSub ? `<button class="ghost sub-rep-btn" type="button" data-report>Report back</button>` : '');
    subBackBar.querySelector('[data-back]').onclick = () => openChat(parentId);
    const repBtn = subBackBar.querySelector('[data-report]');
    if (repBtn) repBtn.onclick = () => openReportMainDialog();
  }
  const subs = parentId ? [] : (S.embeddedSubs || []);
  subHeadBtn.textContent = subs.length ? `Subagents (${subs.length})` : 'Subagents';
  KCH.paintHeadSubs?.();                         // the phone shows a small "N agents" button in the title row instead
  subStrip.hidden = !subs.length;
  if (subs.length) {
    subStrip.innerHTML = '';
    for (const c of subs) {
      const chip = el('button', 'sub-chip' + (c.busy ? ' run' : ''));
      chip.type = 'button';
      chip.title = `${c.name} (${c.model_name})${c.description ? ' - ' + c.description : ''}`;
      chip.innerHTML = `<span class="sub-dot ${c.busy ? 'run' : c.status}"></span><span class="sub-chip-nm">${esc(c.name)}</span>` +
        `<span class="sub-chip-st">${esc(c.busy ? 'Working' : c.model_name.split(' ')[0])}</span>`;
      chip.onclick = () => openChat(c.id);
      subStrip.appendChild(chip);
    }
    const add = el('button', 'sub-chip sub-chip-add', '+');
    add.type = 'button';
    add.title = 'Start another subagent chat';
    add.onclick = () => KCH.openSubagentsSheet?.();
    subStrip.appendChild(add);
  }
}

async function newChat(opts = {}) {
  const group = opts.group || null;
  const section = opts.section || KCH.activeSection?.() || null;
  const r = await api('/api/chats', { project: S.project, endpoint: S.cfg.endpoint, reasoning: S.cfg.reasoning, group, section, effort: S.draftEffort || null });
  S.draftEffort = null;
  S.chats.unshift({ id: r.id, title: 'New chat', created: Date.now() / 1000, updated: Date.now() / 1000, group, section });
  if (group) { const g = S.groups.find((x) => x.id === group); if (g) g.collapsed = false; }
  await openChat(r.id);
  $('chatList').querySelector(`.chat-item[data-id="${CSS.escape(r.id)}"]`)?.classList.add('new');
  if (group) $('input').focus();
}
$('newChat').onclick = () => newChat();

async function editFrom(m) {
  if (S.busy.has(S.chat)) return toast('Stop it first');
  await api(`/api/chats/${S.chat}/truncate`, { from: m.id });
  $('input').value = m.content || '';
  S.chips = (m.files || []).map((f) => f.path);
  paintChips();
  await openChat(S.chat);
  autoSize();
}

$('renameBtn').onclick = async () => {
  const name = window.prompt('Chat name', S.meta?.title || '');
  if (!name) return;
  await api(`/api/chats/${S.chat}/meta`, { title: name });
  $('chatTitle').textContent = name;
  const c = S.chats.find((x) => x.id === S.chat); if (c) c.title = name;
  paintList();
};
async function deleteChat(id) {
  if (!id || !window.confirm('Delete this chat? (Its files on disk stay.)')) return;
  await api(`/api/chats/${id}/meta`, { deleted: true });
  S.chats = S.chats.filter((c) => c.id !== id);
  if (S.chat !== id) return paintList();
  S.chat = null;
  const next = S.chats.find((c) => !c.archived) || S.chats[0];
  if (next) openChat(next.id); else newChat();
}
async function forkChat(id, fromMsgId) {
  const cid = id || S.chat;
  if (!cid) return;
  try {
    const body = {};
    if (fromMsgId) body.from = fromMsgId;
    const sec = KCH.activeSection?.();
    if (sec && cid === S.chat) body.section = sec;
    const r = await api(`/api/chats/${encodeURIComponent(cid)}/fork`, body);
    await loadChats();
    await openChat(r.id);
    toast('Forked');
    return r;
  } catch (e) {
    toast(e.message || 'Could not fork');
  }
}
$('forkBtn').onclick = () => forkChat(S.chat);
$('deleteBtn').onclick = () => deleteChat(S.chat);
$('compactBtn').onclick = compactNow;
$('ctxBtn').onclick = compactNow;
async function compactNow() {
  if (!S.chat || S.busy.has(S.chat)) return toast('It is busy; compaction runs by itself when the window fills');
  if (!S.msgs.length) return;
  toast('Compacting: the model is writing a summary of the chat so far');
  await api(`/api/chats/${S.chat}/compact`, {});
}

/* ---------------------------------------------------------------- composer */
const input = $('input');
function autoSize() {
  input.style.height = 'auto';
  const isPhone = document.body.classList.contains('phone');
  let maxH = window.innerHeight * 0.4;
  if (isPhone) {
    const cs = getComputedStyle(input);
    const lh = parseFloat(cs.lineHeight) || 24;
    const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    maxH = Math.round(lh * 5 + pad);
  }
  const sh = input.scrollHeight;
  input.style.height = Math.min(sh, maxH) + 'px';
  input.style.overflowY = sh > maxH + 1 ? 'auto' : 'hidden';
  setBusy();
}
window.addEventListener('resize', autoSize);
input.addEventListener('input', () => { autoSize(); mention(); });
input.addEventListener('keydown', (e) => {
  if (mentionKeys(e)) return;
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (window.KCD?.active()) KCD.stopThenSend(); else send(); }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && S.busy.has(S.chat) && !$('dialog').open && !document.querySelector('.zoom')) { e.preventDefault(); stop(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') { e.preventDefault(); toggleSide(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n' && !e.shiftKey) { e.preventDefault(); newChat(); }
});
$('sendBtn').onclick = () => (window.KCD?.active() ? KCD.stopThenSend() : send());
$('stopBtn').onclick = stop;

async function send(opts = {}) {
  const text = (opts.text !== undefined ? opts.text : input.value).trim();
  const files = opts.files !== undefined ? opts.files.slice() : S.chips.slice();
  if (!text && !files.length) return;
  if (!S.chat) await newChat();
  if (!opts.skipGuard && KCH.beforeSend) {
    const allow = await KCH.beforeSend(text, files);
    if (allow === false) return;
  }
  const clearComposer = opts.text === undefined;
  if (clearComposer) input.value = '';
  if (opts.files === undefined) { S.chips = []; paintChips(); }
  window.KCD?.onSend?.(clearComposer);
  autoSize();
  stickBottom = true;
  const chatId = S.chat;
  const payload = { text, files, reasoning: S.meta?.reasoning || S.cfg.reasoning, key: sendKey() };
  if (S.meta?.endpoint) payload.endpoint = S.meta.endpoint;
  try {
    const r = await sendThrough(chatId, payload);
    if (S.chat !== chatId) return;                 // the user moved to another chat while it was on its way
    if (r && r.queued && r.id && !_droppedQueued.has(r.id) && !(S.queue || []).some((q) => q.id === r.id)) {
      (S.queue ||= []).push({ id: r.id, text, files, when: r.when || 'step', limit: r.limit });
      paintQueue();
    } else if (r && r.queued === false && !S.busy.has(S.chat)) {     // working from now (Claude Code takes a few seconds to say so)
      S.busy.add(S.chat); paintList(); setBusy(); showWorking(true, 'Starting');
    }
  } catch (e) {
    toast(e.message);
    if (opts.text === undefined && S.chat === chatId && !input.value.trim()) input.value = text;
    else if (opts.text === undefined) toast('Not sent: ' + text.slice(0, 60), 8000);
  }
}

/* A message is never handed back because the connection dropped for a moment (e.g. during a brief server restart).
   It is sent again until it gets through, for SEND_TRY_MS, and is shown as "Sending" meanwhile. The key lets the server
   take it only once when an earlier try did arrive and only its answer was lost. Only a refusal from the server itself
   (4xx) ends the tries at once. */
const SEND_TRY_MS = 180000;
const sending = new Map();                         // key -> {chat, text, tries}
function sendKey() { return 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }
function transient(e) { return !e.status || e.status === 408 || e.status === 429 || e.status >= 500; }
async function sendThrough(chatId, payload) {
  const t0 = Date.now();
  let tries = 0;
  for (;;) {
    try {
      const r = await api(`/api/chats/${chatId}/send`, payload);
      if (sending.delete(payload.key)) paintSending();
      return r;
    } catch (e) {
      tries += 1;
      if (!transient(e) || Date.now() - t0 > SEND_TRY_MS) {
        if (sending.delete(payload.key)) paintSending();
        throw e;
      }
      sending.set(payload.key, { chat: chatId, text: payload.text, tries });
      paintSending();
      await new Promise((res) => setTimeout(res, Math.min(4000, 700 * tries)));
    }
  }
}
function paintSending() {
  let box = $('sendingBox');
  if (!box) {
    box = el('div', 'q-sending');
    box.id = 'sendingBox';
    $('queued').before(box);
  }
  box.innerHTML = '';
  const mine = [...sending.values()].filter((x) => x.chat === S.chat);
  box.hidden = !mine.length;
  for (const x of mine) {
    const row = el('div', 'q-sending-row');
    row.append(el('span', 'q-sending-dot'), el('span', 'q-sending-text', 'Sending (no answer from the PC yet, trying again): ' + x.text.slice(0, 90)));
    box.append(row);
  }
}
KCH.sendThrough = sendThrough;
/* messages sent while the model works: shown above the composer until the model takes them in (then they are the
   user's message in the chat), each with Edit (back into the composer) and Remove */
/* A subagent's report waiting for (or handed to) this chat's model: "[Report from subagent WHO on "TOPIC". ...]\n\nbody".
   Returns {who, topic, body} or null. */
function subReportOf(text) {
  // Two forms reach a main chat: "[Report from subagent WHO on "TOPIC". ...]" (a finished task) and
  // "[Findings from subagent WHO]" (the subagent's own report-to-main). Both are the subagent's words, never the user's.
  const m = /^\[(?:Report|Findings) from subagent (.+?)(?: on "([^"]*)")?(?:\. [\s\S]*?)?\]\s*/.exec(String(text || ''));
  if (!m) return null;
  return { who: m[1], topic: m[2] || '', body: String(text).slice(m[0].length) };
}

function paintQueue() {
  const box = $('queued');
  box.innerHTML = '';
  const all = S.queue || [];
  // Reports from subagents are not the user's messages: they are never listed as queued or steering. They wait
  // quietly (one small line) and show in the chat as a folded line once the model has them.
  const reps = all.filter((x) => subReportOf(x.text));
  const q = all.filter((x) => !subReportOf(x.text));
  box.hidden = !all.length;
  if (reps.length) {
    const line = el('div', 'q-subrep');
    line.textContent = reps.length === 1
      ? `1 subagent report waiting (${subReportOf(reps[0].text).who}): read at the next break`
      : `${reps.length} subagent reports waiting: read at the next break`;
    box.appendChild(line);
  }
  if (!q.length) return;
  const neonWaitItems = q.filter((x) => x.when === 'neon' && !x.readonly);
  const allExt = q.every((x) => x.readonly);
  const isPhone = document.body.classList.contains('phone');
  // Collapse multiple queued items (or long readonly Neon queue items on phone) into a single compact bar so
  // #queued can never crush #conv or push the composer box off screen.
  const collapsible = q.length > 1 || (isPhone && allExt);
  const open = !collapsible || !!S.queueOpen[S.chat];

  if (collapsible) {
    const bar = el('div', 'q-bar' + (allExt ? ' ext' : '') + (open ? ' open' : ''));
    const latest = q[q.length - 1];
    const prev = String(latest?.text || '').replace(/\s+/g, ' ').trim();
    const label = allExt
      ? `${q.length} queued in Neon`
      : q.length === 1 ? '1 queued' : `${q.length} queued`;
    const nowBtn = neonWaitItems.length
      ? `<button class="link q-now" type="button" data-now-all>${esc(neonWaitItems.length > 1 ? `Stop & send (${neonWaitItems.length})` : 'Stop & send')}</button>`
      : '';
    bar.innerHTML = `<span class="q-dot"></span><span class="q-cnt">${esc(label)}</span>` +
      `<span class="q-prev">${esc(prev)}</span>${nowBtn}` +
      `<button class="link q-tog" type="button" data-qtog>${open ? 'Hide' : 'Show'}</button>`;
    bar.querySelector('[data-qtog]').onclick = () => {
      S.queueOpen[S.chat] = !open;
      paintQueue();
    };
    bar.querySelector('[data-now-all]')?.addEventListener('click', async (e) => {
      const b = e.currentTarget;
      b.disabled = true;
      b.textContent = 'Sending…';
      try {
        const r = await api(`/api/chats/${S.chat}/steer-now`, neonWaitItems.length > 1 ? {} : { id: neonWaitItems[0].id });
        if (!r.ok) { toast(r.error || 'Could not stop and send'); paintQueue(); }
        else await openChat(S.chat);
      } catch (err) {
        toast(err.message);
        paintQueue();
      }
    });
    box.appendChild(bar);
    if (!open) return;
  }

  const list = el('div', 'q-list');
  let shownStopBtn = collapsible && neonWaitItems.length > 0;
  for (const item of q) {
    const d = el('div', 'qmsg' + (item.readonly ? ' ext' : ''));
    const isNeonWait = item.when === 'neon' && !item.readonly;
    const showNow = isNeonWait && !shownStopBtn;
    if (showNow) shownStopBtn = true;
    const nowLbl = neonWaitItems.length > 1 ? `Stop and send all (${neonWaitItems.length})` : 'Stop and send';
    const actionsHtml = item.readonly
      ? ''
      : (showNow ? `<button class="link q-now" data-now>${esc(nowLbl)}</button>` : '') +
        '<button class="link" data-edit>Edit</button>' +
        '<button class="iconbtn sm" data-x title="Remove"><svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>';
    d.innerHTML = `<div class="q-top"><span class="q-tag"></span><span class="sp"></span>${actionsHtml}</div><div class="q-text"></div>`;
    const extMeta = [item.sent_from ? `from ${item.sent_from}` : '', item.queued_at || ''].filter(Boolean).join(' · ');
    const labels = {
      turn: 'Queued: sent when this turn ends',
      interrupt: 'Steering: interrupts and resumes within seconds (work so far is kept)',
      neon: 'Queued: waits for Neon\'s current turn to finish',
      external: extMeta ? `Queued in Neon (${extMeta})` : 'Queued in Neon: waits for this turn to finish',
      slot: `Queued: ${item.limit || S.meta?.neon_limit || 6} Neon chats are already running (limit is ${item.limit || S.meta?.neon_limit || 6}). Sends automatically when a slot opens`,
    };
    d.querySelector('.q-tag').textContent = labels[item.when] || 'Steering: goes in at the next step (long commands stop for it)';
    d.querySelector('.q-text').textContent = item.text || (item.files?.length ? `${item.files.length} file(s)` : '');
    if (!item.readonly) {
      const take = async (edit) => {
        const eb = d.querySelector('[data-edit]');
        const xb = d.querySelector('[data-x]');
        if (eb) eb.disabled = true;
        if (xb) xb.disabled = true;
        let r = {};
        try { r = await api(`/api/chats/${S.chat}/unqueue`, { id: item.id }); }
        catch (e) {
          if (eb) eb.disabled = false;
          if (xb) xb.disabled = false;
          toast(e.message);
          return;
        }
        dropQueued(item.id);
        if (!r.ok) { toast('Too late: the model already has it'); return; }
        if (edit) { input.value = r.text + (input.value.trim() ? '\n\n' + input.value : ''); autoSize(); input.focus(); }
      };
      d.querySelector('[data-now]')?.addEventListener('click', async () => {
        const allBtns = [...box.querySelectorAll('[data-now]')];
        allBtns.forEach((b) => { b.disabled = true; b.textContent = 'Sending…'; });
        try {
          const payload = neonWaitItems.length > 1 ? {} : { id: item.id };
          const r = await api(`/api/chats/${S.chat}/steer-now`, payload);
          if (!r.ok) {
            toast(r.error || 'Could not stop and send');
            paintQueue();
          } else {
            await openChat(S.chat);
          }
        } catch (e) {
          toast(e.message);
          paintQueue();
        }
      });
      d.querySelector('[data-edit]').onclick = () => take(true);
      d.querySelector('[data-x]').onclick = () => take(false);
    }
    list.appendChild(d);
  }
  box.appendChild(list);
}
const _droppedQueued = new Set();
function dropQueued(id) {
  if (id) {
    if (_droppedQueued.size > 200) _droppedQueued.clear();
    _droppedQueued.add(id);
  }
  const before = (S.queue || []).length;
  S.queue = (S.queue || []).filter((q) => q.id !== id);
  if (S.queue.length !== before) paintQueue();
}
async function stop() {
  if (!S.chat) return;
  const id = S.chat;
  let r = {};
  try { r = await api(`/api/chats/${id}/stop`, {}); } catch (e) { toast(e.message); }
  if (r.dropped?.length && !input.value.trim()) { input.value = r.dropped.join('\n\n'); autoSize(); }
  if (id === S.chat) { S.queue = []; paintQueue(); }
  if (!r.ok) onEvent({ type: 'turn_end', chat: id });    // nothing was running there: free the composer now
}

const IMG_RE = /\.(png|jpe?g|gif|webp|bmp)$/i;
const rawUrl = (p) => '/api/files/raw?path=' + encodeURIComponent(p);
function chipEl(path, onRemove) {
  const isImg = IMG_RE.test(path);
  const label = /[\\/]attachments[\\/]/i.test(path) ? base(path) : rel(path);
  const c = el('span', 'chip' + (isImg ? ' img' : ''), isImg
    ? `<img alt="" src="${rawUrl(path)}"><span title="${esc(path)}">${esc(base(path))}</span>`
    : `<svg viewBox="0 0 16 16">${IC.Write}</svg><span title="${esc(path)}">${esc(label)}</span>`);
  if (isImg) c.querySelector('img').onclick = () => zoom(rawUrl(path));
  if (onRemove) {
    const x = el('button', '', '<svg viewBox="0 0 16 16" style="width:11px;height:11px"><path d="M4 4l8 8M12 4l-8 8"/></svg>');
    x.title = 'Remove';
    x.onclick = onRemove;
    c.appendChild(x);
  }
  return c;
}
function paintChips() {
  const box = $('chips');
  box.innerHTML = '';
  S.chips.forEach((p, i) => box.appendChild(chipEl(p, () => { S.chips.splice(i, 1); paintChips(); })));
  KCH.paintPendingChips?.(box);
  setBusy();
}
KCH.paintChips = paintChips;
/* a pasted or dropped image: saved with the chat (attachments/) and attached to the next message */
async function attachImage(file, name) {
  if (!S.chat) await newChat();
  const data = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(',')[1] || ''); r.readAsDataURL(file); });
  try {
    const r = await api(`/api/chats/${S.chat}/attach`, { data, mime: file.type, name: name || '' });
    addChip(r.path);
  } catch (err) { toast(err.message); }
}
input.addEventListener('paste', (e) => {
  const cd = e.clipboardData;
  if (!cd || cd.getData('text/plain')) return;           // text (even with a picture of it, as Office copies) pastes as text
  const imgs = [...(cd.files || [])].filter((f) => f.type.startsWith('image/'));
  if (!imgs.length) return;
  e.preventDefault();
  (async () => { for (const f of imgs) await attachImage(f, f.name && f.name !== 'image.png' ? f.name : ''); })();
});
function addChip(path) {
  if (!S.chips.includes(path)) S.chips.push(path);
  paintChips();
  if (!document.body.classList.contains('phone')) input.focus();
}
KCH.addChip = addChip;
$('attachBtn').onclick = () => { input.value += (input.value && !input.value.endsWith(' ') ? ' ' : '') + '@'; input.focus(); mention(); };

/* drag files in: from the tree (a path) or from Windows (images kept with the chat, other files uploaded into the project folder) */
const box = $('dropBox');
['dragenter', 'dragover'].forEach((t) => box.addEventListener(t, (e) => { e.preventDefault(); box.classList.add('drop'); }));
['dragleave', 'drop'].forEach((t) => box.addEventListener(t, () => box.classList.remove('drop')));
box.addEventListener('drop', async (e) => {
  e.preventDefault();
  const p = e.dataTransfer.getData('text/kch-path');
  if (p) return addChip(p);
  for (const f of e.dataTransfer.files || []) {
    if (f.type.startsWith('image/') && IMG_RE.test(f.name)) { await attachImage(f, f.name); continue; }    // kept with the chat
    const data = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(',')[1] || ''); r.readAsDataURL(f); });
    const dest = S.project.replace(/[\\/]$/, '') + '\\' + f.name;
    try { await api('/api/files/upload', { path: dest, data }); addChip(dest); window.KCF?.refreshSoon(); } catch (err) { toast(err.message); }
  }
});

/* @ mentions */
let mState = null;
async function mention() {
  const pos = input.selectionStart, before = input.value.slice(0, pos);
  const m = before.match(/(^|\s)@([^\s@]*)$/);
  if (!m) { closeMention(); return; }
  const q = m[2];
  const r = await api(`/api/files/search?root=${encodeURIComponent(S.project)}&q=${encodeURIComponent(q)}`).catch(() => ({ files: [] }));
  if (!input.value.slice(0, input.selectionStart).endsWith('@' + q)) return;
  if (!r.files.length) { closeMention(); return; }
  const pop = $('popover');
  pop.innerHTML = '';
  r.files.slice(0, 12).forEach((f, i) => {
    const b = el('button', 'item' + (i === 0 ? ' act' : ''), `${esc(f)}`);
    b.onmousedown = (ev) => { ev.preventDefault(); pickMention(f); };
    pop.appendChild(b);
  });
  const rc = $('composer').getBoundingClientRect();
  pop.style.left = rc.left + 'px';
  pop.style.top = 'auto';
  pop.style.bottom = (window.innerHeight - rc.top + 6) + 'px';
  pop.hidden = false;
  mState = { files: r.files.slice(0, 12), i: 0, q };
}
function pickMention(f) {
  const pos = input.selectionStart;
  const before = input.value.slice(0, pos).replace(/@([^\s@]*)$/, '@' + (/\s/.test(f) ? `"${f}"` : f) + ' ');
  input.value = before + input.value.slice(pos);
  input.selectionStart = input.selectionEnd = before.length;
  closeMention();
  input.focus();
}
function closeMention() { if (mState) { $('popover').hidden = true; mState = null; } }
function mentionKeys(e) {
  if (!mState) return false;
  const items = [...$('popover').querySelectorAll('.item')];
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    mState.i = (mState.i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items.forEach((x, i) => x.classList.toggle('act', i === mState.i));
    return true;
  }
  if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pickMention(mState.files[mState.i]); return true; }
  if (e.key === 'Escape') { closeMention(); return true; }
  return false;
}

/* speech: see dictation.js (live dictation into the composer) */

/* ---------------------------------------------------------------- task list */
function paintTodos(todos) {
  const box = $('todos');
  if (!todos || !todos.length || todos.every((t) => t.status === 'completed')) { box.hidden = true; return; }
  box.hidden = false;
  const done = todos.filter((t) => t.status === 'completed').length;
  box.innerHTML = `<div class="h"><span>Tasks</span><span class="spacer"></span><span>${done}/${todos.length}</span></div>` +
    todos.map((t) => `<div class="t ${t.status}"><i></i><span>${esc(t.content)}</span></div>`).join('');
}

/* ---------------------------------------------------------------- model, GPU, reasoning, context */
function curEp() {
  const id = S.meta?.endpoint || S.cfg?.endpoint;
  return S.cfg?.endpoints.find((e) => e.id === id) || S.cfg?.endpoints[0];
}
KCH.curEp = curEp;
function epState(ep) {
  if (!ep) return ['', ''];
  if (ep.runtime) {
    const r = S.runtime || {};
    const what = r.choice_label || r.label || 'server';
    const cost = r.rate ? ` · ${r.cost} ${r.unit || ''}`.trimEnd() : '';
    if (r.state === 'ready') return ['ok', `${what} · ${r.up_min ? Math.round(r.up_min) + ' min' + cost : 'ready'}`];
    if (r.state === 'external') return ['ok', 'running (started outside)'];
    if (r.state === 'starting' || r.state === 'warming') return ['wait', `${what}: ${r.detail || 'starting'}`];
    if (r.state === 'stopping') return ['wait', r.detail && r.detail !== 'stopping' ? `stopping: ${r.detail}` : 'stopping'];
    if (r.state === 'checking') return ['wait', r.detail || 'checking'];
    if (r.state === 'orphaned') return ['bad', `GPU on ${r.remote || 'the remote side'} running without its launcher`];
    if (r.state === 'failed') return ['bad', `${r.label || 'server'} failed to start`];
    return ['', `${r.label || 'server'} off`];
  }
  const h = S.health[ep.id];
  const ps = S.statuses[ep.id];
  if (ps) return [ps.state === 'ready' ? 'ok' : ps.state === 'signed out' ? 'wait' : 'bad', ps.state];
  if (ep.kind === 'claude-code') return ['ok', 'Claude Code'];
  if (ep.kind === 'codex') return ['ok', 'Codex'];
  if (ep.kind === 'agy') return ['ok', 'Antigravity'];
  if (ep.kind === 'opencode-go') return ['ok', 'OpenCode Go'];
  return h ? [h.ok ? 'ok' : 'bad', h.ok ? 'online' : 'offline'] : ['', ''];
}
function paintModel() {
  const ep = curEp();
  if (!ep) { $('modelDot').className = 'dot'; $('modelName').textContent = 'No model yet'; $('modelSub').textContent = 'add one'; KCH.onModelPaint?.(); return; }
  const [st, sub] = epState(ep);
  $('modelDot').className = 'dot ' + st;
  $('modelName').textContent = ep.name;
  $('modelSub').textContent = sub;
  meetTellModel(ep);
  KCH.onModelPaint?.();
}
/* Reasoning: Fast / Think / Deep for most connections; Claude Code and Codex connections list their CLI's own effort
   levels (ep.levels, from the server), picked per chat ({connection id: level} in the chat's `effort`). */
function effortOf(ep) {
  if (!ep?.effort_default) return null;
  const ids = (ep.levels || []).map((x) => x.id);
  const pick = (S.meta?.effort || {})[ep.id] ?? (S.chat ? null : S.draftEffort?.[ep.id]);
  const old = ep.effort_from?.[S.meta?.reasoning || S.cfg?.reasoning || 'fast'];   // same order as harness/effort.py
  return [pick, ep.effort, old].find((x) => ids.includes(x)) || ids[0];
}
KCH.effortOf = effortOf;
function paintReason() {
  const ep = curEp(), seg = $('reasonSeg');
  const native = !!ep?.effort_default;
  const levels = ep?.no_thinking ? ep.levels : native ? ep.levels : [{ id: 'fast', label: 'Fast', hint: 'No thinking: fastest' },
    { id: 'think', label: 'Think', hint: 'Thinks before it answers' }, { id: 'deep', label: 'Deep', hint: 'Thinks hard and checks its work' }];
  const key = (native || ep?.no_thinking ? ep.id : '') + levels.map((x) => x.id).join();
  if (seg.dataset.key !== key) {
    seg.dataset.key = key;
    seg.classList.toggle('native', native);
    seg.setAttribute('aria-label', native ? `${ep.name}: effort level` : 'How much the model thinks');
    seg.replaceChildren(...levels.map((x) => {
      const b = el('button', '', esc(x.label));
      b.dataset.r = x.id;
      b.title = native ? `Effort ${x.label}: ${x.hint}.
Applies from the next message, in the same session. The first message after a change reads the chat again without the cache (the system prompt stays cached).` : x.hint;
      b.onclick = () => pickReason(x.id);
      return b;
    }));
  }
  const r = ep?.no_thinking ? 'fast' : native ? effortOf(ep) : (S.meta?.reasoning || S.cfg?.reasoning || 'fast');
  seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.r === r));
  KCH.onReasonPaint?.(levels, r);
}
async function pickReason(r) {
  const ep = curEp();
  if (ep?.effort_default) {                       // a native level: this chat, this connection
    const eff = { [ep.id]: r };
    if (S.chat) {
      S.meta = { ...(S.meta || {}), effort: { ...(S.meta?.effort || {}), ...eff } };
      paintReason();
      await api(`/api/chats/${S.chat}/meta`, { effort: eff });
    } else {
      S.draftEffort = { ...(S.draftEffort || {}), ...eff };
      paintReason();
    }
    return;
  }
  S.meta = { ...(S.meta || {}), reasoning: r };
  paintReason();
  if (S.chat) await api(`/api/chats/${S.chat}/meta`, { reasoning: r });
  await api('/api/config', { reasoning: r });
  S.cfg.reasoning = r;
}
document.querySelectorAll('#reasonSeg button').forEach((b) => { b.onclick = () => pickReason(b.dataset.r); });
function paintCtx() {
  const ep = curEp();
  const limit = S.meta?.ctx?.limit || ep?.context || 250000;
  const used = S.meta?.ctx?.tokens || 0;
  const frac = Math.min(1, used / limit);
  $('ctxBar').style.width = (frac * 100).toFixed(1) + '%';
  $('ctxText').textContent = `${fmtK(used)} / ${fmtK(limit)}`;
  $('ctxBtn').classList.toggle('warm', frac > 0.6);
  $('ctxBtn').classList.toggle('hot', frac > 0.8);
}

function popAt(anchor, nodes, alignRight) {
  let pop = $('popover');
  if (!pop) { pop = el('div', 'pop'); pop.id = 'popover'; document.body.appendChild(pop); }
  pop.innerHTML = '';
  nodes.forEach((n) => pop.appendChild(n));
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  pop.style.bottom = 'auto';
  pop.style.top = r.bottom + 6 + 'px';
  if (alignRight) { pop.style.left = 'auto'; pop.style.right = Math.max(8, window.innerWidth - r.right) + 'px'; }
  else { pop.style.right = 'auto'; pop.style.left = Math.max(8, r.left) + 'px'; }
  setTimeout(() => document.addEventListener('mousedown', closePop, { once: true }), 0);
}
function closePop(e) {
  const pop = $('popover');
  if (!pop) return;
  if (e && pop.contains(e.target)) { document.addEventListener('mousedown', closePop, { once: true }); return; }
  pop.hidden = true;
}
KCH.popAt = popAt;
KCH.closePop = closePop;

$('modelBtn').onclick = () => KCH.openModelSheet?.();

const RUNNING = ['ready', 'starting', 'warming'];
const STOPPABLE = [...RUNNING, 'orphaned', 'checking'];
function runtimeEvent(rt) {
  const old = S.runtime || {};
  const was = old.state;
  S.runtime = rt;
  paintModel();
  const box = document.getElementById('rtBox');       // the model menu is open: show a change of state in place
  const changed = was !== rt.state || old.detail !== rt.detail || (old.orphans || []).length !== (rt.orphans || []).length;
  if (box && changed && (!$('popover').hidden || document.querySelector('.m-sheet.open'))) box.replaceWith(runtimeNodes()[0]);
  if (was === 'stopping' && rt.state !== 'stopping') toast(rt.state === 'orphaned' ? rt.detail : (rt.detail || 'Stopped'), 6000);
}
KCH.runtimeNodes = runtimeNodes;
function runtimeNodes() {
  const r = S.runtime || {};
  const box = el('div', '');
  box.id = 'rtBox';
  const out = [el('div', 'lbl', r.label || 'Model server')];
  const choices = Object.entries(r.choices || {});
  if (choices.length) {
    const grid = el('div', 'gpus');
    for (const [id, g] of choices) {
      const on = (r.choice === id && RUNNING.includes(r.state));
      const b = el('button', 'gpu' + (on ? ' on' : ''), `<b>${esc(g.label || id)}</b><span>${esc(g.speed || '')}</span>${g.rate ? `<span>${g.rate} ${esc(r.unit || '')}/h</span>` : ''}`);
      b.title = g.note || '';
      b.onclick = () => runtimeGo(id);
      grid.appendChild(b);
    }
    out.push(grid);
  }
  const info = el('div', 'rt');
  const stateTxt = { ready: 'Ready', starting: 'Starting', warming: 'Warming up', stopping: 'Stopping', failed: 'Failed', external: 'Running (started outside the harness)', off: 'Off',
    checking: 'Checking', orphaned: 'Still running' }[r.state] || r.state;
  info.innerHTML = `<div class="line"><span class="dot ${epState(curEp())[0]}"></span><b>${esc(stateTxt)}</b><span>${esc(r.detail && r.detail !== 'ready' ? r.detail : '')}</span></div>` +
    (['ready', 'orphaned'].includes(r.state) && r.up_min ? `<div class="line muted">${esc(r.choice_label || r.label)} up ${Math.round(r.up_min || 0)} min${r.rate ? `, about ${r.cost} ${esc(r.unit || '')} so far` : ''}</div>` : '') +
    (r.orphans?.length && RUNNING.includes(r.state) ? `<div class="line">Also still running on ${esc(r.remote || 'the remote side')} without a launcher: ${r.orphans.length} GPU${r.orphans.length > 1 ? 's' : ''}. Stop ends ${r.orphans.length > 1 ? 'them' : 'it'} too.</div>` : '') +
    (r.tail?.length ? `<div class="tail">${esc(r.tail.slice(-6).join('\n'))}</div>` : '');
  const btns = el('div', 'btns');
  if (STOPPABLE.includes(r.state) || (r.state === 'external' && r.remote)) {
    const s = el('button', 'danger', 'Stop');
    s.onclick = async () => {
      s.disabled = true; s.textContent = 'Stopping…';
      try { runtimeEvent(await api('/api/runtime/stop', {})); } catch (e) { toast(e.message); s.disabled = false; s.textContent = 'Stop'; }
    };
    btns.appendChild(s);
  } else if (r.state === 'stopping') {
    const s = el('button', 'danger', 'Stopping…');
    s.disabled = true;
    btns.appendChild(s);
  } else if (!choices.length && r.state !== 'external') {
    const s = el('button', 'primary', 'Start');
    s.onclick = () => runtimeGo(null);
    btns.appendChild(s);
  }
  info.appendChild(btns);
  out.push(info);
  out.forEach((n) => box.appendChild(n));
  return [box];
}
async function runtimeGo(choice) {
  const r = S.runtime || {};
  const name = choice ? (r.choices?.[choice]?.label || choice) : (r.label || 'the server');
  try {
    if (RUNNING.includes(r.state) && choice && r.choice !== choice) {
      // never stop a running server as a side effect of a click: only the Stop button stops it
      toast(`${r.choice_label || 'The server'} keeps running. To use ${name} instead, press Stop first, then pick ${name}.`, 6000);
      return;
    } else if (!RUNNING.includes(r.state)) {
      S.runtime = await api('/api/runtime/start', { choice });
      toast(`Starting ${name}${r.start_note ? ': ' + r.start_note : ''}`);
    }
  } catch (e) { toast(e.message); }
  paintModel();
  closePop();
}

async function checkHealth(ep) {
  if (!ep || ep.runtime || ep.kind !== 'openai') return;
  try { S.health[ep.id] = await api('/api/endpoints/health?id=' + encodeURIComponent(ep.id)); } catch { S.health[ep.id] = { ok: false }; }
  paintModel();
}

/* ---------------------------------------------------------------- projects */
$('projectBtn').onclick = () => {
  const nodes = [el('div', 'lbl', 'Projects')];
  for (const p of S.projects) {
    const b = el('button', 'item' + (p.path === S.project ? ' on' : ''), `<span>${esc(p.name)}<span class="s2">${esc(p.path)}</span></span>`);
    b.onclick = () => { closePop(); setProject(p.path); };
    nodes.push(b);
  }
  nodes.push(el('div', 'sep'));
  const open = el('button', 'item', 'Open a folder…');
  open.onclick = async () => {
    closePop();
    const p = window.prompt('Folder path (it is created if it does not exist)', S.project);
    if (!p) return;
    const r = await api('/api/projects', { path: p.trim().replace(/^"|"$/g, '') });
    S.projects = r.projects;
    setProject(r.path);
  };
  const neu = el('button', 'item', `New project in ${esc(base(S.cfg.workspace))}…`);
  neu.onclick = async () => {
    closePop();
    const n = window.prompt('Project name');
    if (!n) return;
    const r = await api('/api/projects', { name: n.trim() });
    S.projects = r.projects;
    setProject(r.path);
  };
  nodes.push(open, neu);
  popAt($('projectBtn'), nodes);
};
async function setProject(path, opts = {}) {
  const seq0 = openSeq;
  S.project = path;
  LS.set('project', path);
  $('projectName').textContent = base(path);
  await loadChats();
  window.KCF?.setRoot(path);
  if (opts.boot && openSeq !== seq0) return;     // another chat was opened explicitly while boot loaded
  const last = LS.get('chat:' + path, null);
  const firstActive = S.chats.find((c) => !c.archived);
  if (last && S.chats.find((c) => c.id === last && !c.archived)) await openChat(last);
  else if (firstActive) await openChat(firstActive.id);
  else if (S.chats[0]) await openChat(S.chats[0].id);
  else await newChat();
}

/* ---------------------------------------------------------------- layout: resizable lines, collapsible panels */
// Wide windows: the chats list and the files panel are columns, each shown or hidden (noside / noright, remembered).
// Narrow windows (NARROW, the same width as the CSS block): they open over the chat, one at a time, from their own
// remembered flags (mside / mright, closed by default) so a wide window's choice never covers a narrow window's chat.
const NARROW = window.matchMedia('(max-width: 820px)');
const layout = { side: 248, right: 42, tree: 38, noside: false, noright: false, mside: false, mright: false, ...LS.get('layout', {}) };
const sideShown = () => (NARROW.matches ? layout.mside : !layout.noside);
const rightShown = () => (NARROW.matches ? layout.mright : !layout.noright);
function applyLayout() {
  const root = document.documentElement.style;
  root.setProperty('--side', layout.side + 'px');
  root.setProperty('--rightw', layout.right + '%');
  root.setProperty('--treeh', layout.tree + '%');
  $('shell').classList.toggle('noside', !sideShown());
  $('shell').classList.toggle('noright', !rightShown());
  $('sideBtn').classList.toggle('on', sideShown());
  $('rightBtn').classList.toggle('on', rightShown());
  KCH.onLayout?.();
}
function saveLayout() { LS.set('layout', layout); applyLayout(); }
function setSide(on) {
  if (NARROW.matches) { layout.mside = on; if (on) layout.mright = false; } else layout.noside = !on;
  saveLayout();
}
function setRight(on) {
  if (NARROW.matches) { layout.mright = on; if (on) layout.mside = false; } else layout.noright = !on;
  saveLayout();
}
function toggleSide() { setSide(!sideShown()); }
KCH.showRight = () => { if (!rightShown()) setRight(true); };
$('sideBtn').onclick = toggleSide;
$('rightBtn').onclick = () => setRight(!rightShown());
$('rightClose').onclick = () => setRight(false);       // the Files button in the top bar brings it back
document.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'e') { e.preventDefault(); setRight(!rightShown()); } });
NARROW.addEventListener('change', applyLayout);
// narrow: a tap on the chat closes the panel over it, and picking a chat closes the chats list
$('center').addEventListener('pointerdown', () => { if (NARROW.matches && (layout.mside || layout.mright)) { layout.mside = layout.mright = false; saveLayout(); } });
$('chatList').addEventListener('click', (e) => { if (NARROW.matches && !KCH.phoneSelect?.active && e.target.closest('.chat-item') && !e.target.closest('.kids')) setSide(false); });
document.querySelectorAll('.gutter').forEach((g) => {
  g.addEventListener('dblclick', () => {
    const k = g.dataset.drag;
    if (k === 'side') layout.side = 248; if (k === 'right') layout.right = 42; if (k === 'tree') layout.tree = 38;
    saveLayout();
  });
  g.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const k = g.dataset.drag;
    g.classList.add('drag');
    document.body.classList.add(k === 'tree' ? 'dragging-h' : 'dragging');
    const shell = $('shell').getBoundingClientRect(), pane = $('pane-files').getBoundingClientRect();
    const move = (ev) => {
      if (k === 'side') layout.side = Math.max(180, Math.min(440, ev.clientX - shell.left));
      if (k === 'right') layout.right = Math.max(22, Math.min(66, (shell.right - ev.clientX) / shell.width * 100));
      if (k === 'tree') layout.tree = Math.max(12, Math.min(85, (ev.clientY - pane.top) / pane.height * 100));
      applyLayout();
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      g.classList.remove('drag');
      document.body.classList.remove('dragging', 'dragging-h');
      saveLayout();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
});

/* ---------------------------------------------------------------- meeting mode */
// The meetings app's address from the profile, under the same loopback name as this window (127.0.0.1 or localhost), so it
// stays same-site and loads inside the frame
function meetUrl() {
  const u = new URL(S.cfg?.features?.meetings_url || 'http://127.0.0.1:8770');
  if (['127.0.0.1', 'localhost'].includes(u.hostname)) u.hostname = location.hostname === 'localhost' ? 'localhost' : '127.0.0.1';
  return u.origin;
}
// What the frame shows: its address is set once, and a (re)started app reloads it so a dead page never stays on screen.
// A project switched in the harness goes over by message, so a live meeting's page is never reloaded under the user.
const meet = { project: null, model: null, watch: null, down: false, busy: false };
function meetLabel() { return S.cfg?.features?.meetings_label || 'Meetings'; }
function meetWanted(st) {
  const name = String(base(S.project) || '').toLowerCase();
  return (st.projects || []).find((p) => String(p).toLowerCase() === name) || null;
}
function meetLoad(st) {
  const f = $('meetFrame');
  const MEET = meetUrl();
  const theme = document.documentElement.dataset.theme || 'dark';
  const want = meetWanted(st);
  const ep = curEp();
  f.src = `${MEET}/?embed=1&theme=${theme}` + (want ? `&project=${encodeURIComponent(want)}` : '')
    + (ep ? `&model=${encodeURIComponent(ep.id)}&model_name=${encodeURIComponent(ep.name)}` : '');
  meet.project = want;
  meet.model = ep?.id || null;
}
// The meetings app's assistant uses the model picked here: tell an open frame when the pick changes (its next answer uses it).
function meetTellModel(ep) {
  const f = $('meetFrame');
  if (!ep || !f?.src || meet.model === ep.id) return;
  meet.model = ep.id;
  try { f.contentWindow.postMessage({ kcModel: { id: ep.id, name: ep.name } }, meetUrl()); } catch { /* not loaded yet */ }
}
function meetDown(text) {
  meet.down = !!text;
  $('meetDown').hidden = !text;
  if (text) $('meetDownText').textContent = text;
}
async function meetStart() {
  // Starting can take a while (first start, a slow disk); the toast stays up until it answers or fails.
  meet.busy = true;
  const t0 = Date.now();
  const tick = setInterval(() => toast(`Starting ${meetLabel()}… ${Math.round((Date.now() - t0) / 1000)} s`, 5000), 1000);
  toast(`Starting ${meetLabel()}…`, 5000);
  try { return await api('/api/meetings/start', {}); } finally { clearInterval(tick); meet.busy = false; }
}
async function meetCheck() {
  // While the meeting view is open: notice when the app stops, offer to start it again, and reload once it is back.
  if (meet.busy || !$('shell').classList.contains('meeting')) return;
  let st;
  try { st = await api('/api/meetings?fresh=1'); } catch { return; }      // the harness itself is restarting
  if (st.running) {
    if (meet.down) { meetDown(''); meetLoad(st); }
    return;
  }
  meetDown(st.conflict ? `Another program is using ${meetLabel()}'s port, so it cannot run.`
    : st.not_answering ? `${meetLabel()} is not answering.` : `${meetLabel()} has stopped. Meetings in progress are saved.`);
}
$('meetRestart').onclick = async () => {
  try { const st = await meetStart(); meetDown(''); meetLoad(st); toast(`${meetLabel()} is ready`, 1500); }
  catch (e) { meetDown(e.message); }
};
$('meetBtn').onclick = async () => {
  const shell = $('shell');
  if (shell.classList.contains('meeting')) {
    shell.classList.remove('meeting');
    $('meeting').hidden = true;
    $('meetBtn').classList.remove('on');
    $('meetBtn').textContent = meetLabel();
    clearInterval(meet.watch);
    return;
  }
  if (meet.busy) return;                   // a start is already under way
  try {
    let st = await api('/api/meetings?fresh=1');
    if (st.conflict) throw new Error(`port ${new URL(st.url).port} is used by another program. Close it, or give the meetings app another port in profile.json.`);
    let started = false;
    if (!st.running) { st = await meetStart(); started = true; toast(`${meetLabel()} is ready`, 1500); }
    const f = $('meetFrame');
    const want = meetWanted(st);
    if (started || meet.down || !f.src || !f.src.startsWith(meetUrl())) meetLoad(st);
    else if (want && want !== meet.project) {
      f.contentWindow.postMessage({ kcProject: want }, meetUrl());
      meet.project = want;
    }
    meetDown('');
    $('meeting').hidden = false;
    shell.classList.add('meeting');
    $('meetBtn').classList.add('on');
    $('meetBtn').textContent = 'Back to chats';
    clearInterval(meet.watch);
    meet.watch = setInterval(meetCheck, 5000);
  } catch (e) { toast(meetLabel() + ': ' + e.message, 9000); }
};

/* ---------------------------------------------------------------- theme + start */
$('themeBtn').onclick = () => {
  window.kcToggleTheme?.();
  const f = $('meetFrame');
  if (f.src) { try { f.contentWindow.postMessage({ kcTheme: document.documentElement.dataset.theme }, meetUrl()); } catch { /* not loaded */ } }
};

// buttons for things this machine has set up (profile.json, install.py) and nothing else
function applyFeatures() {
  const f = S.cfg?.features || {};
  $('meetBtn').hidden = !f.meetings;
  if (f.meetings) { $('meetBtn').textContent = $('meetBtn').classList.contains('on') ? 'Back to chats' : (f.meetings_label || 'Meetings'); $('meetBtn').title = f.meetings_label || 'Meetings'; $('meetFrame').title = f.meetings_label || 'Meetings'; }
  $('micBtn').hidden = !window.KCD?.engine();
  if (window.KCD) $('micBtn').title = KCD.title();
}
KCH.applyFeatures = applyFeatures;
KCH.autoSize = autoSize;
KCH.send = send;
KCH.setProject = setProject;
KCH.openChat = openChat;
Object.assign(KCH, {
  openChat, newChat, loadChats, deleteChat, forkChat, dayLabel, paintSub, paintList,
  pickReason, paintModel, paintReason, paintCtx, setSide, setRight, sideShown, rightShown,
  layout, saveLayout, applyLayout, NARROW, stop, paintQueue, setBusy, mention, closeMention,
  zoom, rawUrl, IMG_RE, epState, checkHealth,
  spawnSubagent, paintSubChrome, openSubagentsDialog, openReportMainDialog, latestSubagentReply,
});
KCH.repaint = async () => { paintModel(); paintReason(); paintCtx(); if (S.chat) await openChat(S.chat); };

function paintBurnBanner() {
  const b = $('burnBanner');
  if (!b) return;
  const list = Array.isArray(S.burnAlerts) ? S.burnAlerts : [];
  if (!list.length) { b.hidden = true; b.innerHTML = ''; return; }
  const top = list[0];
  const extra = list.length > 1 ? ` (+${list.length - 1} more)` : '';
  const cid = top.chat || '';
  b.hidden = false;
  b.innerHTML = `<span class="burn-dot" aria-hidden="true">!</span>`
    + `<span class="msg"><strong>Claude burn alert</strong>${esc(extra)}: ${esc(top.summary || top.title || 'High usage detected')}</span>`
    + `<span class="burn-acts">`
    + (cid ? `<button type="button" class="burn-open">Open</button><button type="button" class="burn-stop">Stop chat</button>` : '')
    + `<button type="button" class="burn-dismiss" title="Dismiss alert">Dismiss</button>`
    + `</span>`;
  const openBtn = b.querySelector('.burn-open');
  if (openBtn && cid) {
    openBtn.onclick = () => openChat(cid).catch(() => {});
  }
  const stopBtn = b.querySelector('.burn-stop');
  if (stopBtn && cid) {
    stopBtn.onclick = async () => {
      stopBtn.disabled = true;
      try {
        await api(`/api/chats/${encodeURIComponent(cid)}/stop`, {});
        toast(`Stopped "${top.title || cid}"`);
        const r = await api('/api/burn/dismiss', { id: top.id });
        S.burnAlerts = r.alerts || [];
        paintBurnBanner();
      } catch (e) {
        stopBtn.disabled = false;
        toast('Could not stop chat: ' + (e.message || e), 6000);
      }
    };
  }
  const disBtn = b.querySelector('.burn-dismiss');
  if (disBtn) {
    disBtn.onclick = async () => {
      try {
        const r = await api('/api/burn/dismiss', { id: top.id });
        S.burnAlerts = r.alerts || [];
      } catch {
        S.burnAlerts = list.slice(1);
      }
      paintBurnBanner();
    };
  }
}

async function boot() {
  applyLayout();
  const st = await api('/api/state');
  S.cfg = st.config;
  S.projects = st.projects;
  S.runtime = st.runtime;
  S.build = st.build || st.config?.build || '';
  S.sourcesSynced = st.sources_synced || {};
  S.burnAlerts = st.burn_alerts || [];
  paintBurnBanner();
  applyFeatures();
  if (!S.projects.length) {
    const r = await api('/api/projects', { name: 'scratch' });
    S.projects = r.projects;
  }
  const saved = LS.get('project', null);
  const proj = S.projects.find((p) => p.path === saved) || S.projects[0];
  if (!boot.connected) { boot.connected = true; connect(); }
  paintModel();
  paintReason();
  await setProject(proj.path, { boot: true });
  checkHealth(curEp());
  if (!boot.health) boot.health = setInterval(() => checkHealth(curEp()), 30000);
}
// The page never stays dead. A start that fails says why and tries again by itself: the app may be restarting, or a
// call may be failing for a reason that is fixed while the page is open (21 Nov 2026: one bad record in the chat list).
function bootAgain(e) {
  console.error(e);
  boot.tries = (boot.tries || 0) + 1;
  const wait = Math.min(2000 * boot.tries, 15000);
  toast('Chats could not start (' + (e.message || e) + '). Trying again.', wait + 1500);
  setTimeout(() => boot().catch(bootAgain), wait);
}
KCH.md = md;
KCH.showWorking = showWorking;
KCH.thinkBlock = thinkBlock;
KCH.setThought = setThought;
KCH.flowNode = flowNode;
KCH.unflow = unflow;
KCH.paintGroup = paintGroup;
KCH.toolRow = toolRow;
KCH.finishTool = finishTool;
KCH.findRow = findRow;
KCH.scrollDown = scrollDown;
boot().catch(bootAgain);
