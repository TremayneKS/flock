'use strict';
/* File viewers for the editor pane: every common file type opens in a proper view. Images, PDF, video and audio, markdown,
   tables (CSV, genomics), JSON and XML trees, notebooks, Word / Excel / PowerPoint, archives, 3D models, molecules, fonts,
   SQLite, sequences, and a hex view for anything else. Text files keep the editor, with a View / Source switch.

   Security: nothing a file holds ever runs as the app. Rendered HTML (markdown, notebooks, Word) goes through DOMPurify and
   the app's CSP forbids inline script anyway; links never navigate the app; SVG is only ever an <img> (a data: URL, never a
   same-origin blob: URL that could be opened as a page); PDFs and HTML pages open from the preview port, a separate origin
   with no API; /api/files/raw answers with a sandbox CSP. Heavy libraries load the first time a file needs them. */
(() => {
  const { esc, el, base, api } = window.KCH;
  const RAW = (p) => '/api/files/raw?path=' + encodeURIComponent(p);
  const MB = 1048576;
  const TEXT_CAP = 4 * MB;          // text read for a view when the file is too big to open whole
  const ROWS_CAP = 200000;          // rows parsed for a table
  const sizeTxt = (n) => n == null ? '' : n < 1024 ? n + ' B' : n < MB ? (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB' : n < 1024 * MB ? (n / MB).toFixed(1) + ' MB' : (n / 1024 / MB).toFixed(2) + ' GB';
  const num = (n) => Number(n).toLocaleString('en-GB');
  const extOf = (name) => { const n = String(name).toLowerCase(); const i = n.lastIndexOf('.'); return i > 0 ? n.slice(i + 1) : ''; };
  const sepOf = (p) => (String(p).includes('\\') ? '\\' : '/');
  const dirOf = (p) => String(p).replace(/[\\/][^\\/]*$/, '');
  const utf8 = (u8) => new TextDecoder('utf-8').decode(u8);
  const ab = (u8) => (u8.byteOffset === 0 && u8.byteLength === u8.buffer.byteLength ? u8.buffer : u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength));
  const h = (tag, cls, html) => el(tag, cls, html);

  /* ---------------------------------------------------------------- which viewer */
  const EXT = {};
  const reg = (viewer, list) => list.split(' ').forEach((e) => { EXT[e] = viewer; });
  reg('image', 'png jpg jpeg jfif pjpeg pjp gif webp avif bmp ico cur apng');
  reg('tiff', 'tif tiff');
  reg('svg', 'svg');
  reg('pdf', 'pdf');
  reg('video', 'mp4 m4v webm mov mkv ogv');
  reg('audio', 'mp3 wav ogg oga flac m4a aac opus weba');
  reg('markdown', 'md markdown mdown mkd');
  reg('html', 'html htm xhtml');
  reg('csv', 'csv tsv tab psv');
  reg('json', 'json geojson webmanifest har');
  reg('jsonl', 'jsonl ndjson');
  reg('xml', 'xml xsd xsl xslt rss atom kml gpx plist');
  reg('notebook', 'ipynb');
  reg('docx', 'docx');
  reg('sheet', 'xlsx xlsm xlsb xls ods');
  reg('pptx', 'pptx');
  reg('zip', 'zip jar war apk aar whl nupkg vsix xpi epub odt odp odg npz');
  reg('tar', 'tar');
  reg('targz', 'tgz');
  reg('gz', 'gz gzip bgz');
  reg('model', 'glb gltf stl obj ply');
  reg('molecule', 'pdb ent pqr cif mmcif mcif mol2 sdf mol xyz gro');
  reg('fastx', 'fa fasta fna ffn faa frn fas fsa fastq fq');
  reg('genome', 'vcf bed bedgraph gff gff3 gtf sam');
  reg('alignment', 'bam cram bai crai csi tbi');
  reg('font', 'ttf otf woff woff2');
  reg('sqlite', 'db sqlite sqlite3 db3');
  const TEXT = new Set(['svg', 'markdown', 'html', 'csv', 'json', 'jsonl', 'xml', 'notebook', 'molecule', 'fastx', 'genome']);

  function kind(path) {
    const n = base(path).toLowerCase();
    if (/\.tar\.(gz|bgz)$/.test(n)) return { viewer: 'targz', text: false };
    const v = EXT[extOf(n)];
    return v ? { viewer: v, text: TEXT.has(v) } : null;
  }
  const defaultMode = (viewer) => (viewer === 'html' ? 'source' : 'view');

  /* ---------------------------------------------------------------- byte sources: a file on disk (HTTP Range) or bytes in memory */
  function decodeText(u8, cut) {
    let s = utf8(u8);
    if (cut) { const i = s.lastIndexOf('\n'); if (i > 0) s = s.slice(0, i); }
    return s;
  }
  function fileSrc(path, tab) {
    let total = null;
    const s = {
      name: base(path), path, url: RAW(path),
      async range(a, b) {                          // bytes [a, b)
        if (total != null) b = Math.min(b, total);
        if (b <= a) return new Uint8Array(0);
        const r = await fetch(s.url, { headers: { Range: `bytes=${a}-${b - 1}` } });
        const m = /\/(\d+)$/.exec(r.headers.get('Content-Range') || '');
        if (m) total = +m[1];
        if (r.status === 416) return new Uint8Array(0);
        if (!r.ok) { let msg = `HTTP ${r.status}`; try { msg = (await r.json()).error || msg; } catch { /* not json */ } throw new Error(msg); }
        if (r.status === 200) total = +(r.headers.get('Content-Length') || 0);
        return new Uint8Array(await r.arrayBuffer());
      },
      async size() { if (total == null) await s.range(0, 1); return total; },
      async bytes(cap) { const n = await s.size(); if (cap && n > cap) throw new Error(`Too big to open here (${sizeTxt(n)}; the limit is ${sizeTxt(cap)})`); return s.range(0, n); },
      async text(cap = TEXT_CAP) {
        if (tab && typeof tab.text === 'string' && !tab.big) return { text: tab.text, partial: false };
        const n = await s.size();
        const u = await s.range(0, Math.min(n, cap));
        return { text: decodeText(u, n > cap), partial: n > cap };
      },
    };
    return s;
  }
  function memSrc(name, u8, partial) {
    return {
      name, path: null, mem: true, partial: !!partial,
      async range(a, b) { return u8.subarray(a, Math.min(b, u8.length)); },
      async size() { return u8.length; },
      async bytes() { return u8; },
      async text(cap = TEXT_CAP) { const cut = u8.length > cap; return { text: decodeText(cut ? u8.subarray(0, cap) : u8, cut || partial), partial: cut || !!partial }; },
    };
  }

  /* ---------------------------------------------------------------- decompression (the browser's own DecompressionStream) */
  function concat(parts, n) {
    if (parts.length === 1) return parts[0];
    const out = new Uint8Array(n ?? parts.reduce((a, p) => a + p.length, 0));
    let o = 0;
    for (const p of parts) { out.set(p.subarray(0, Math.min(p.length, out.length - o)), o); o += p.length; if (o >= out.length) break; }
    return out;
  }
  async function collect(stream, cap = Infinity) {
    const rd = stream.getReader();
    const parts = [];
    let n = 0, partial = false;
    try {
      for (;;) {
        const { done, value } = await rd.read();
        if (done) break;
        parts.push(value); n += value.length;
        if (n >= cap) { partial = true; rd.cancel().catch(() => {}); break; }
      }
    } catch (e) { if (!n) throw e; partial = true; }     // cut-off input: keep what came out
    return { bytes: concat(parts, Math.min(n, cap)), partial };
  }
  const inflate = (u8, format, cap) => collect(new Blob([u8]).stream().pipeThrough(new DecompressionStream(format)), cap);
  /* gzip, including BGZF (bgzip: .vcf.gz, .bam), which is many small gzip members the stream API would stop after the first */
  async function gunzip(u8, cap = 16 * MB) {
    const bgzf = u8.length > 18 && u8[0] === 0x1f && u8[1] === 0x8b && (u8[3] & 4) && u8[12] === 66 && u8[13] === 67;
    if (!bgzf) return inflate(u8, 'gzip', cap);
    const blocks = [];
    let p = 0;
    while (p + 18 <= u8.length && u8[p] === 0x1f && u8[p + 1] === 0x8b) {
      const bsize = (u8[p + 16] | (u8[p + 17] << 8)) + 1;
      if (p + bsize > u8.length) break;
      blocks.push(u8.subarray(p + 18, p + bsize - 8));
      p += bsize;
    }
    const parts = [];
    let n = 0;
    for (let i = 0; i < blocks.length && n < cap; i += 16) {
      const outs = await Promise.all(blocks.slice(i, i + 16).map((b) => (b.length > 2 ? inflate(b, 'deflate-raw') : { bytes: new Uint8Array(0) })));
      for (const o of outs) { parts.push(o.bytes); n += o.bytes.length; }
    }
    return { bytes: concat(parts, Math.min(n, cap)), partial: n > cap || p < u8.length };
  }

  /* ---------------------------------------------------------------- libraries, loaded on first use */
  const loading = {};
  function script(src) {
    return (loading[src] ||= new Promise((ok, fail) => {
      const s = document.createElement('script');
      s.src = src;
      s.onload = ok;
      s.onerror = () => { delete loading[src]; fail(new Error('Could not load ' + src)); };
      document.head.appendChild(s);
    }));
  }
  const LIB = { mol: '/static/vendor/3dmol/3Dmol-min.js', mammoth: '/static/vendor/mammoth/mammoth.browser.min.js',
    xlsx: '/static/vendor/sheetjs/xlsx.full.min.js', utif: '/static/vendor/utif/UTIF.js', three: '/static/vendor/three/three-viewer.js',
    pdfjs: '/static/vendor/pdfjs/pdf.min.js', pdfjsWorker: '/static/vendor/pdfjs/pdf.worker.min.js',
    pdfLib: '/static/vendor/pdf-lib/pdf-lib.min.js', edit: '/static/viewers-edit.js' };
  const editHelpers = () => ({ LIB, script, bar, btn, spacer, body, note, ab, extOf, sizeTxt, sniff, parseDelimited, sniffImgMime, decodeTiffCanvas, zipOpen, memSrc, sanitize, wireDoc, mdHtml });

  /* ---------------------------------------------------------------- small UI pieces */
  function bar(cx) { const b = h('div', 'kv-bar'); cx.root.appendChild(b); return b; }
  function btn(label, title, fn) { const b = h('button', 'kv-btn', label); if (title) b.title = title; b.onclick = fn; return b; }
  function spacer() { return h('div', 'kv-sp'); }
  function body(cx, cls) { const b = h('div', 'kv-body ' + (cls || '')); cx.root.appendChild(b); return b; }
  function note(parent, title, detail) { const n = h('div', 'kv-note', `<b>${esc(title)}</b>${detail ? `<span>${esc(detail)}</span>` : ''}`); parent.appendChild(n); return n; }
  function select(options, value, onChange) {
    const s = h('select', 'kv-sel');
    s.innerHTML = options.map(([v, label]) => `<option value="${esc(v)}"${v === value ? ' selected' : ''}>${esc(label)}</option>`).join('');
    s.onchange = () => onChange(s.value);
    return s;
  }
  const TWI = '<svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg>';

  /* a sortable table: rows are arrays of strings; long tables grow with "Show more" */
  const NUMRE = /^\s*[-+]?(\d[\d,]*\.?\d*|\.\d+)([eE][-+]?\d+)?%?\s*$/;
  function table(rows, { cols, sortable = true, index = true, step = 500, start = 1, onRow, maxCols = 300 } = {}) {
    const wrap = h('div', 'kv-tablewrap');
    let ncol = cols ? cols.length : 0;
    for (let i = 0; i < rows.length && i < 5000; i++) ncol = Math.max(ncol, rows[i].length);
    ncol = Math.min(ncol, maxCols);
    const numeric = [];
    for (let c = 0; c < ncol; c++) {
      let seen = 0, nums = 0;
      for (let i = 0; i < rows.length && seen < 400; i++) { const v = rows[i][c]; if (v === undefined || v === '') continue; seen++; if (NUMRE.test(v)) nums++; }
      numeric[c] = seen > 0 && nums / seen >= 0.8;
    }
    rows.forEach((r, i) => { if (r.ref === undefined) r.ref = i; });
    let view = rows, shown = 0, sortCol = -1, dir = 1;
    const t = h('table', 'kv-table');
    const head = h('thead');
    const hr = h('tr');
    if (index) hr.appendChild(h('th', 'ix', ''));
    for (let c = 0; c < ncol; c++) {
      const th = h('th', numeric[c] ? 'num' : '', `<span>${esc(cols?.[c] ?? '')}</span>`);
      if (sortable) {
        th.classList.add('sort');
        th.title = 'Sort';
        th.onclick = () => {
          if (sortCol === c) dir = dir === 1 ? -1 : dir === -1 ? 0 : 1; else { sortCol = c; dir = 1; }
          hr.querySelectorAll('th').forEach((x) => x.classList.remove('asc', 'desc'));
          if (dir) th.classList.add(dir === 1 ? 'asc' : 'desc');
          if (!dir) { view = rows; sortCol = -1; } else {
            const key = numeric[c] ? (r) => { const v = r[c]; return v === undefined || v === '' ? null : parseFloat(String(v).replace(/,/g, '')); } : (r) => r[c] ?? '';
            const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
            view = rows.slice().sort((a, b) => {
              const x = key(a), y = key(b);
              if (x === null || x === '') return y === null || y === '' ? 0 : 1;
              if (y === null || y === '') return -1;
              return dir * (numeric[c] ? x - y : coll.compare(x, y));
            });
          }
          shown = 0; tb.innerHTML = ''; more();
        };
      }
      hr.appendChild(th);
    }
    head.appendChild(hr);
    if (cols || index) t.appendChild(head);
    const tb = h('tbody');
    t.appendChild(tb);
    wrap.appendChild(t);
    const moreBtn = h('button', 'kv-more');
    wrap.appendChild(moreBtn);
    function more() {
      const end = Math.min(view.length, shown + step);
      let html = '';
      for (let i = shown; i < end; i++) {
        const r = view[i];
        html += `<tr data-i="${i}">` + (index ? `<td class="ix">${start + r.ref}</td>` : '');
        for (let c = 0; c < ncol; c++) html += `<td${numeric[c] ? ' class="num"' : ''}>${esc(r[c] ?? '')}</td>`;
        html += '</tr>';
      }
      tb.insertAdjacentHTML('beforeend', html);
      shown = end;
      moreBtn.hidden = shown >= view.length;
      moreBtn.textContent = `Show ${num(Math.min(step, view.length - shown))} more (${num(shown)} of ${num(view.length)} shown)`;
    }
    moreBtn.onclick = more;
    tb.onclick = (e) => {
      const td = e.target.closest('td');
      if (!td) return;
      if (onRow) { tb.querySelectorAll('tr.on').forEach((x) => x.classList.remove('on')); const tr = td.parentElement; tr.classList.add('on'); onRow(view[+tr.dataset.i]); }
      else if (!td.classList.contains('ix')) td.classList.toggle('open');
    };
    more();
    return wrap;
  }

  /* a lazily built collapsible tree row */
  function branch(rowHtml, build, open) {
    const box = h('div', 'kv-node');
    const row = h('div', 'kv-row br', `<span class="tw">${TWI}</span>${rowHtml}`);
    const kids = h('div', 'kv-kids');
    kids.hidden = true;
    let built = false;
    row.onclick = (e) => { if (e.target.closest('a,button')) return; toggle(); };
    function toggle(force) {
      const show = force ?? kids.hidden;
      if (show && !built) { built = true; build(kids); }
      kids.hidden = !show;
      row.classList.toggle('open', show);
    }
    box.append(row, kids);
    box.toggle = toggle;
    if (open) toggle(true);
    return box;
  }
  function leaf(html) { return h('div', 'kv-row', `<span class="tw"></span>${html}`); }
  function inChunks(items, kids, render, step = 300) {
    let i = 0;
    const more = () => {
      const end = Math.min(items.length, i + step);
      for (; i < end; i++) kids.appendChild(render(items[i], i));
      if (i < items.length) {
        const b = h('button', 'kv-more inline', `Show ${num(Math.min(step, items.length - i))} more of ${num(items.length)}`);
        b.onclick = () => { b.remove(); more(); };
        kids.appendChild(b);
      }
    };
    more();
  }
  function expandAll(root, limit = 4000) {
    let n = 0;
    for (let pass = 0; pass < 12 && n < limit; pass++) {
      const closed = [...root.querySelectorAll('.kv-node')].filter((x) => !x.firstChild.classList.contains('open'));
      if (!closed.length) break;
      for (const c of closed) { if (n++ >= limit) break; c.toggle(true); }
    }
  }
  function collapseAll(root) { root.querySelectorAll('.kv-node').forEach((x) => { if (x.firstChild.classList.contains('open')) x.toggle(false); }); }

  /* ---------------------------------------------------------------- safe HTML */
  const PURIFY = {
    FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'option', 'iframe', 'frame', 'frameset', 'object', 'embed',
      'base', 'meta', 'link', 'dialog', 'template', 'portal', 'audio', 'video', 'source', 'track'],
    FORBID_ATTR: ['style', 'srcset', 'formaction', 'action', 'ping', 'autofocus', 'tabindex'],
    ALLOW_DATA_ATTR: false, SANITIZE_NAMED_PROPS: true,
  };
  const PURIFY_RICH = { ...PURIFY, FORBID_ATTR: PURIFY.FORBID_ATTR.filter((a) => a !== 'style') };
  const sanitize = (html, rich) => DOMPurify.sanitize(html, rich ? PURIFY_RICH : PURIFY);
  const mdHtml = (text) => {                       // task-list boxes become marks: form controls are not allowed in rendered files
    try { return marked.parse(text || '').replace(/<input\b[^>]*type="checkbox"[^>]*>/gi, (m) => (/\bchecked\b/i.test(m) ? '<span class="kv-check on">\u2713</span>' : '<span class="kv-check"></span>')); } catch { return esc(text); }
  };

  function resolvePath(dir, rel) {
    const sep = sepOf(dir);
    const parts = dir.split(/[\\/]/);
    for (const p of rel.replace(/^[\\/]+/, '').split(/[\\/]/)) {
      if (p === '..') parts.length > 1 && parts.pop();
      else if (p && p !== '.') parts.push(p);
    }
    return parts.join(sep);
  }
  /* images and links inside rendered documents: relative images come from the file's folder; links never navigate the app */
  function wireDoc(root, cx) {
    const dir = cx.src.path ? dirOf(cx.src.path) : null;
    root.querySelectorAll('img').forEach((img) => {
      const s = img.getAttribute('src') || '';
      if (/^(data:image\/|blob:)/i.test(s)) {
        img.onerror = () => img.replaceWith(h('span', 'kv-noimg', esc(img.getAttribute('alt') || 'Unsupported embedded image')));
        return;
      }
      if (/^[a-z][\w+.-]*:/i.test(s) || !dir) { img.replaceWith(h('span', 'kv-noimg', esc(img.getAttribute('alt') || 'image')));  return; }
      try { img.src = RAW(resolvePath(dir, decodeURIComponent(s.split(/[?#]/)[0]))); } catch { img.remove(); }
    });
    root.addEventListener('click', (e) => {
      const a = e.target.closest('a[href]');
      if (!a || !root.contains(a)) return;
      e.preventDefault();
      const href = a.getAttribute('href');
      if (href.startsWith('#')) {
        const id = decodeURIComponent(href.slice(1));
        const t = root.querySelector(`[id="${CSS.escape(id)}"], [id="user-content-${CSS.escape(id)}"], [name="${CSS.escape(id)}"]`);
        t?.scrollIntoView({ block: 'start' });
      } else if (/^(https?|mailto):/i.test(href)) window.open(href, '_blank', 'noopener,noreferrer');
      else if (dir && !/^[a-z][\w+.-]*:/i.test(href)) window.KCF?.openFile(resolvePath(dir, decodeURIComponent(href.split('#')[0])));
    });
  }

  /* ---------------------------------------------------------------- images */
  async function imageView(cx, url, extra) {
    const tools = bar(cx);
    const stage = body(cx, 'kv-stage');
    const img = new Image();
    img.alt = ''; img.draggable = false;
    stage.appendChild(img);
    let s = 1, x = 0, y = 0, fit = true, w = 0, hh = 0;
    const zl = h('span', 'kv-dim');
    const apply = () => { img.style.transform = `translate(${x}px, ${y}px) scale(${s})`; zl.textContent = Math.round(s * 100) + '%'; img.classList.toggle('px', s >= 3); };
    const doFit = () => {
      const W = stage.clientWidth, H = stage.clientHeight;
      if (!W || !H) return;
      s = Math.min((W - 32) / w, (H - 32) / hh);
      s = Math.max(0.01, Math.max(w, hh) <= 64 ? Math.min(s, 8) : Math.min(s, 1));
      x = (W - w * s) / 2; y = (H - hh * s) / 2; fit = true; apply();
    };
    const zoomAt = (f, px, py) => { const ns = Math.min(64, Math.max(0.01, s * f)); x = px - (px - x) * ns / s; y = py - (py - y) * ns / s; s = ns; fit = false; apply(); };
    const mid = (f) => zoomAt(f, stage.clientWidth / 2, stage.clientHeight / 2);
    tools.append(btn('Fit', 'Fit to the pane (double-click the image)', doFit),
      btn('1:1', 'Actual size', () => { const W = stage.clientWidth, H = stage.clientHeight; s = 1; x = (W - w) / 2; y = (H - hh) / 2; fit = false; apply(); }),
      btn('−', 'Zoom out', () => mid(1 / 1.25)), btn('+', 'Zoom in (or scroll on the image)', () => mid(1.25)), zl, spacer());
    stage.addEventListener('wheel', (e) => { e.preventDefault(); const r = stage.getBoundingClientRect(); zoomAt(Math.exp(-e.deltaY * 0.0016), e.clientX - r.left, e.clientY - r.top); }, { passive: false });
    stage.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      stage.setPointerCapture(e.pointerId);
      stage.classList.add('drag');
      let lx = e.clientX, ly = e.clientY;
      const move = (m) => { x += m.clientX - lx; y += m.clientY - ly; lx = m.clientX; ly = m.clientY; fit = false; apply(); };
      const up = () => { stage.classList.remove('drag'); stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerup', up); };
      stage.addEventListener('pointermove', move);
      stage.addEventListener('pointerup', up);
    });
    stage.addEventListener('dblclick', (e) => { if (fit) { const r = stage.getBoundingClientRect(); zoomAt(1 / s, e.clientX - r.left, e.clientY - r.top); } else doFit(); });
    const ro = new ResizeObserver(() => { if (fit && w) doFit(); });
    ro.observe(stage);
    cx.later(() => ro.disconnect());
    await new Promise((ok, fail) => { img.onload = ok; img.onerror = () => fail(new Error('The browser cannot show this image')); img.src = url; });
    w = img.naturalWidth; hh = img.naturalHeight;
    if (!w || !hh) { w = 300; hh = 150; }
    if (extra?.size) { w = extra.size[0]; hh = extra.size[1]; }
    img.style.width = w + 'px'; img.style.height = hh + 'px';
    doFit();
    const n = await cx.src.size().catch(() => null);
    const dims = extra?.size || [img.naturalWidth, img.naturalHeight];
    const info = [dims[0] && dims[1] ? `${num(dims[0])} × ${num(dims[1])} px` : '', extra?.note || '', n != null ? sizeTxt(n) : ''].filter(Boolean).join(' · ');
    tools.appendChild(h('span', 'kv-dim', esc(info)));
    cx.info(info);
  }
  const IMGTYPE = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', jfif: 'image/jpeg', pjpeg: 'image/jpeg', pjp: 'image/jpeg', gif: 'image/gif',
    webp: 'image/webp', avif: 'image/avif', bmp: 'image/bmp', ico: 'image/x-icon', cur: 'image/x-icon', apng: 'image/apng' };
  const MEDIATYPE = { mp4: 'video/mp4', m4v: 'video/mp4', webm: 'video/webm', mov: 'video/quicktime', mkv: 'video/x-matroska', ogv: 'video/ogg',
    mp3: 'audio/mpeg', wav: 'audio/wav', ogg: 'audio/ogg', oga: 'audio/ogg', flac: 'audio/flac', m4a: 'audio/mp4', aac: 'audio/aac', opus: 'audio/ogg', weba: 'audio/webm' };
  function b64(u8) { let s = ''; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); }
  const svgURL = (text) => 'data:image/svg+xml;base64,' + b64(new TextEncoder().encode(text));
  /* an object URL for bytes in memory: only image, audio and video types (never one a browser would run as a page) */
  function objURL(cx, u8, type) {
    if (!/^(image\/(?!svg)|audio\/|video\/)/.test(type)) type = 'application/octet-stream';
    const u = URL.createObjectURL(new Blob([u8], { type }));
    cx.later(() => URL.revokeObjectURL(u));
    return u;
  }
  function svgSize(text) {
    const m = /<svg\b[^>]*>/i.exec(text);
    if (!m) return null;
    const vb = /viewBox\s*=\s*["']\s*([-\d.e]+)[\s,]+([-\d.e]+)[\s,]+([-\d.e]+)[\s,]+([-\d.e]+)/i.exec(m[0]);
    const wa = /\swidth\s*=\s*["']\s*([\d.]+)(px)?\s*["']/i.exec(m[0]), ha = /\sheight\s*=\s*["']\s*([\d.]+)(px)?\s*["']/i.exec(m[0]);
    if (wa && ha) return [Math.round(+wa[1]), Math.round(+ha[1])];
    if (vb) return [Math.round(+vb[3]), Math.round(+vb[4])];
    return null;
  }

  const VIEW = {};
  VIEW.image = async (cx) => imageView(cx, cx.src.path ? cx.src.url + '&t=' + Date.now() : objURL(cx, await cx.src.bytes(), IMGTYPE[extOf(cx.src.name)] || 'image/png'));
  VIEW.svg = async (cx) => {
    const { text, partial } = await cx.src.text(16 * MB);
    if (partial && cx.src.path) return imageView(cx, cx.src.url + '&t=' + Date.now());
    const sz = svgSize(text);
    return imageView(cx, svgURL(text), { size: sz && sz[0] > 0 && sz[1] > 0 ? sz : null, note: 'SVG' });
  };
  function sniffImgMime(u8, declared) {
    if (u8.length >= 4) {
      const a = u8[0], b = u8[1], c = u8[2], d = u8[3];
      if ((a === 0x49 && b === 0x49 && c === 0x2a && d === 0x00) || (a === 0x4d && b === 0x4d && c === 0x00 && d === 0x2a)) return 'image/tiff';
      if (a === 0x89 && b === 0x50 && c === 0x4e && d === 0x47) return 'image/png';
      if (a === 0xff && b === 0xd8 && c === 0xff) return 'image/jpeg';
      if (a === 0x47 && b === 0x49 && c === 0x46 && d === 0x38) return 'image/gif';
      if (a === 0x42 && b === 0x4d) return 'image/bmp';
      if (u8.length >= 12 && a === 0x52 && b === 0x49 && c === 0x46 && d === 0x46 && u8[8] === 0x57 && u8[9] === 0x45 && u8[10] === 0x42 && u8[11] === 0x50) return 'image/webp';
      if (a === 0x3c) {
        const head = utf8(u8.subarray(0, Math.min(256, u8.length))).trimStart();
        if (/^(<\?xml[\s\S]*?)?<svg\b/i.test(head)) return 'image/svg+xml';
      }
    }
    const ct = String(declared || '').split(';')[0].trim().toLowerCase();
    if (ct === 'image/jpg') return 'image/jpeg';
    if (ct === 'image/x-png') return 'image/png';
    if (ct === 'image/tif' || ct === 'image/x-tiff') return 'image/tiff';
    return ct;
  }
  async function decodeTiffCanvas(buf) {
    await script(LIB.utif);
    const ifds = window.UTIF.decode(buf);
    const page = ifds.find((d) => d.t256 && d.t257) || ifds[0];
    if (!page) throw new Error('Empty TIFF image');
    window.UTIF.decodeImage(buf, page, ifds);
    const rgba = window.UTIF.toRGBA8(page);
    const c = document.createElement('canvas');
    c.width = page.width; c.height = page.height;
    c.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(rgba.buffer, rgba.byteOffset, c.width * c.height * 4), c.width, c.height), 0, 0);
    return { canvas: c, pages: ifds.length };
  }
  VIEW.tiff = async (cx) => {
    if (cx.src.path) {
      const ok = await new Promise((r) => { const i = new Image(); i.onload = () => r(true); i.onerror = () => r(false); i.src = cx.src.url; });
      if (ok) return imageView(cx, cx.src.url);
    }
    const u8 = await cx.src.bytes(300 * MB);
    const { canvas: c, pages } = await decodeTiffCanvas(ab(u8));
    const blob = await new Promise((r) => c.toBlob(r, 'image/png'));
    const url = URL.createObjectURL(blob);
    cx.later(() => URL.revokeObjectURL(url));
    return imageView(cx, url, { note: 'TIFF' + (pages > 1 ? `, page 1 of ${pages}` : '') });
  };

  /* ---------------------------------------------------------------- video and audio: native controls, seeking through HTTP Range */
  async function media(cx, video) {
    if (video) {
      const tb = bar(cx);
      const fs = btn('Fullscreen', () => {
        if (window.KCH?.zoomVideo && m.src) window.KCH.zoomVideo(m.src, { from: m, title: cx.src.name });
        else if (m.requestFullscreen) m.requestFullscreen().catch(() => {});
        else if (m.webkitEnterFullscreen) m.webkitEnterFullscreen();
      }, 'Open fullscreen video');
      tb.append(spacer(), fs);
    }
    const b = body(cx, 'kv-media' + (video ? '' : ' audio'));
    const m = document.createElement(video ? 'video' : 'audio');
    m.controls = true; m.preload = 'metadata';
    if (video) { m.setAttribute('playsinline', ''); m.setAttribute('webkit-playsinline', ''); }
    if (!video) b.appendChild(h('div', 'kv-card', `<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><b>${esc(cx.src.name)}</b>`));
    b.appendChild(m);
    cx.later(() => { m.pause(); m.removeAttribute('src'); m.load(); });
    const n = await cx.src.size().catch(() => null);
    m.onloadedmetadata = () => {
      const d = m.duration, t = isFinite(d) ? `${Math.floor(d / 60)}:${String(Math.floor(d % 60)).padStart(2, '0')}` : '';
      cx.info([video && m.videoWidth ? `${m.videoWidth} × ${m.videoHeight}` : '', t, sizeTxt(n)].filter(Boolean).join(' · '));
    };
    m.onerror = () => { m.hidden = true; note(b, `This ${video ? 'video' : 'audio file'} cannot be played here`, `The browser does not support its codec or container (${extOf(cx.src.name).toUpperCase()}, ${sizeTxt(n)}).`); };
    m.src = cx.src.path ? cx.src.url : objURL(cx, await cx.src.bytes(512 * MB), MEDIATYPE[extOf(cx.src.name)] || '');
    cx.info(sizeTxt(n));
  }
  VIEW.video = (cx) => media(cx, true);
  VIEW.audio = (cx) => media(cx, false);

  /* ---------------------------------------------------------------- PDF and HTML: from the preview port (its own origin, no API) */
  async function framed(cx, title, sandbox) {
    if (!cx.src.path) { note(body(cx), `${title} inside an archive`, 'Extract it to open it here.'); return; }
    const { url } = await api('/api/preview-url?path=' + encodeURIComponent(cx.src.path));
    const f = document.createElement('iframe');
    f.className = 'kv-frame'; f.title = title;
    if (sandbox) f.setAttribute('sandbox', sandbox);
    f.src = url;
    cx.root.appendChild(f);
    const n = await cx.src.size().catch(() => null);
    cx.info(sizeTxt(n));
  }
  VIEW.pdf = async (cx) => {
    if (!cx.src.path) return framed(cx, 'PDF');
    await script(LIB.edit);
    return window.KCV_EDIT.pdf(cx, editHelpers());
  };
  VIEW.html = async (cx) => { await framed(cx, 'Page', 'allow-scripts allow-forms allow-same-origin allow-popups allow-modals'); cx.info('the saved file, as served from the preview port'); };

  /* ---------------------------------------------------------------- markdown */
  VIEW.markdown = async (cx) => {
    await script(LIB.edit);
    return window.KCV_EDIT.markdown(cx, editHelpers());
  };

  /* ---------------------------------------------------------------- delimited text */
  function parseDelimited(text, d, maxRows = ROWS_CAP) {
    const rows = [];
    const D = d.charCodeAt(0), n = text.length;
    let row = [], i = text.charCodeAt(0) === 0xfeff ? 1 : 0;
    while (i < n) {
      let f;
      if (text.charCodeAt(i) === 34) {                     // "quoted", with "" for a quote
        let j = i + 1;
        f = '';
        for (;;) {
          const k = text.indexOf('"', j);
          if (k < 0) { f += text.slice(j); i = n; break; }
          f += text.slice(j, k);
          if (text.charCodeAt(k + 1) === 34) { f += '"'; j = k + 2; } else { i = k + 1; break; }
        }
        while (i < n) { const c = text.charCodeAt(i); if (c === D || c === 10 || c === 13) break; f += text[i++]; }
      } else {
        let j = i;
        while (j < n) { const c = text.charCodeAt(j); if (c === D || c === 10 || c === 13) break; j++; }
        f = text.slice(i, j);
        i = j;
      }
      row.push(f);
      if (i >= n) break;
      const c = text.charCodeAt(i);
      if (c === D) { i++; if (i >= n) row.push(''); continue; }
      if (!(row.length === 1 && row[0] === '')) rows.push(row);
      row = [];
      if (c === 13 && text.charCodeAt(i + 1) === 10) i++;
      i++;
      if (rows.length >= maxRows) return { rows, more: i < n };
    }
    if (row.length && !(row.length === 1 && row[0] === '')) rows.push(row);
    return { rows, more: false };
  }
  function sniff(text) {
    const lines = text.slice(0, 20000).split(/\r?\n/).filter(Boolean).slice(0, 10);
    let best = ',', score = 0;
    for (const d of [',', '\t', ';', '|']) {
      const counts = lines.map((l) => l.split(d).length - 1);
      const s = counts.length && counts.every((c) => c === counts[0]) ? counts[0] * 2 : Math.min(...counts);
      if (s > score) { score = s; best = d; }
    }
    return best;
  }
  VIEW.csv = async (cx) => {
    await script(LIB.edit);
    return window.KCV_EDIT.csv(cx, editHelpers());
  };

  /* ---------------------------------------------------------------- JSON and XML trees */
  function jsonValue(v) {
    if (v === null) return '<span class="j-null">null</span>';
    if (typeof v === 'string') return `<span class="j-str">"${esc(v.length > 4000 ? v.slice(0, 4000) + '…' : v)}"</span>`;
    if (typeof v === 'number') return `<span class="j-num">${v}</span>`;
    if (typeof v === 'boolean') return `<span class="j-bool">${v}</span>`;
    return esc(String(v));
  }
  function jsonNode(key, v, depth) {
    const k = key === null ? '' : `<span class="j-key">${esc(key)}</span><span class="j-p">: </span>`;
    if (!v || typeof v !== 'object') return leaf(k + jsonValue(v));
    const arr = Array.isArray(v);
    const keys = arr ? null : Object.keys(v);
    const n = arr ? v.length : keys.length;
    if (!n) return leaf(k + `<span class="j-p">${arr ? '[]' : '{}'}</span>`);
    return branch(k + `<span class="j-p">${arr ? '[' : '{'}</span><span class="j-sum">${num(n)} ${arr ? (n === 1 ? 'item' : 'items') : (n === 1 ? 'key' : 'keys')}</span>`,
      (kids) => inChunks(arr ? v : keys, kids, (x, i) => (arr ? jsonNode(String(i), x, depth + 1) : jsonNode(x, v[x], depth + 1))), depth < 1 || (depth < 2 && n <= 50));
  }
  function treeTools(cx, tree, sourceNote) {
    const t = bar(cx);
    t.append(btn('Expand all', 'Open every branch (up to a few thousand)', () => expandAll(tree)), btn('Collapse all', '', () => collapseAll(tree)), spacer());
    if (sourceNote) t.appendChild(h('span', 'kv-dim', esc(sourceNote)));
  }
  VIEW.json = async (cx) => {
    const { text, partial } = await cx.src.text(64 * MB);
    let v;
    try { v = JSON.parse(text); } catch (e) {
      if (partial) throw new Error('This JSON file is too big to show as a tree (only the first part was read).');
      throw new Error('Not valid JSON: ' + e.message + '. Switch to Source to fix it.');
    }
    const tree = h('div', 'kv-tree');
    treeTools(cx, tree);
    tree.appendChild(jsonNode(null, v, 0));
    body(cx, 'kv-scroll').appendChild(tree);
    cx.info(Array.isArray(v) ? `array of ${num(v.length)}` : v && typeof v === 'object' ? `object with ${num(Object.keys(v).length)} keys` : typeof v);
  };
  VIEW.jsonl = async (cx) => {
    const { text, partial } = await cx.src.text();
    const lines = text.split(/\r?\n/).filter((l) => l.trim());
    const tree = h('div', 'kv-tree');
    treeTools(cx, tree);
    let bad = 0;
    inChunks(lines, tree, (l, i) => {
      try { const v = JSON.parse(l); return jsonNode(`line ${i + 1}`, v, 1); } catch (e) { bad++; return leaf(`<span class="j-key">line ${i + 1}</span><span class="j-p">: </span><span class="j-err">${esc(e.message)}</span>`); }
    }, 200);
    body(cx, 'kv-scroll').appendChild(tree);
    cx.info(`${num(lines.length)}${partial ? '+' : ''} records${partial ? ' · first 4 MB' : ''}`);
  };
  function xmlAttrs(e) { return [...e.attributes].map((a) => ` <span class="x-attr">${esc(a.name)}</span><span class="j-p">=</span><span class="j-str">"${esc(a.value)}"</span>`).join(''); }
  function xmlNode(node, depth) {
    if (node.nodeType === 3 || node.nodeType === 4) { const t = node.nodeValue.trim(); return t ? leaf(`<span class="x-text">${esc(t.length > 4000 ? t.slice(0, 4000) + '…' : t)}</span>`) : null; }
    if (node.nodeType === 8) return leaf(`<span class="x-com">&lt;!--${esc(node.nodeValue)}--&gt;</span>`);
    if (node.nodeType === 7) return leaf(`<span class="x-com">&lt;?${esc(node.target)} ${esc(node.data)}?&gt;</span>`);
    if (node.nodeType !== 1) return null;
    const tag = `<span class="x-tag">&lt;${esc(node.nodeName)}</span>${xmlAttrs(node)}<span class="x-tag">&gt;</span>`;
    const kids = [...node.childNodes].filter((c) => c.nodeType === 1 || c.nodeType === 8 || c.nodeType === 7 || ((c.nodeType === 3 || c.nodeType === 4) && c.nodeValue.trim()));
    const close = `<span class="x-tag">&lt;/${esc(node.nodeName)}&gt;</span>`;
    if (!kids.length) return leaf(tag.replace(/&gt;<\/span>$/, ' /&gt;</span>'));
    if (kids.length === 1 && kids[0].nodeType !== 1 && kids[0].nodeType !== 8 && kids[0].nodeValue.trim().length < 120) return leaf(tag + `<span class="x-text">${esc(kids[0].nodeValue.trim())}</span>` + close);
    return branch(tag + `<span class="j-sum">${num(kids.length)}</span>`, (box) => {
      inChunks(kids, box, (c) => xmlNode(c, depth + 1) || document.createComment(''));
      box.appendChild(leaf(close));
    }, depth < 2);
  }
  VIEW.xml = async (cx) => {
    const { text, partial } = await cx.src.text(32 * MB);
    const doc = new DOMParser().parseFromString(text, 'application/xml');
    const err = doc.getElementsByTagName('parsererror')[0];
    if (err) throw new Error((partial ? 'Only the first part of this file was read, so it is not complete XML. ' : 'Not valid XML: ') + (err.textContent || '').split('\n').slice(0, 2).join(' ').slice(0, 300));
    const tree = h('div', 'kv-tree');
    treeTools(cx, tree);
    for (const c of doc.childNodes) { const n = xmlNode(c, 0); if (n) tree.appendChild(n); }
    body(cx, 'kv-scroll').appendChild(tree);
    cx.info(`<${doc.documentElement.nodeName}> · ${num(doc.getElementsByTagName('*').length)} elements`);
  };

  /* ---------------------------------------------------------------- notebooks */
  const KW = new Set(('False None True and as assert async await break class continue def del elif else except finally for from global if import in is ' +
    'lambda nonlocal not or pass raise return try while with yield match case function var let const new this typeof instanceof void delete switch ' +
    'default throw catch export extends static super null undefined true false library require repeat next TRUE FALSE NULL NA Inf NaN end begin ' +
    'module using struct mutable where local do then elseif fn mut pub impl use crate trait enum').split(' '));
  const HL_TAIL = String.raw`|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|${'`'}(?:\\.|[^${'`'}\\])*${'`'})|\b(0x[0-9a-f]+|\d+(?:\.\d+)?(?:e[-+]?\d+)?[jJL]?)\b|(@[\w.]+)|([A-Za-z_]\w*)(?=\s*\()|([A-Za-z_]\w*)`;
  const HL_HASH = new RegExp(String.raw`(#[^\n]*)` + HL_TAIL, 'gi');
  const HL_SLASH = new RegExp(String.raw`(\/\/[^\n]*|\/\*[\s\S]*?\*\/)` + HL_TAIL, 'gi');
  function highlight(code, lang) {
    const re = /^(javascript|typescript|js|ts|c|cpp|c\+\+|java|rust|go|kotlin|scala|swift|csharp|c#)$/i.test(lang || '') ? HL_SLASH : HL_HASH;
    re.lastIndex = 0;
    let out = '', last = 0, m;
    while ((m = re.exec(code))) {
      out += esc(code.slice(last, m.index));
      const t = esc(m[0]);
      out += m[1] ? `<span class="hl-com">${t}</span>` : m[2] ? `<span class="hl-str">${t}</span>` : m[3] ? `<span class="hl-num">${t}</span>`
        : m[4] ? `<span class="hl-fn">${t}</span>` : m[5] ? (KW.has(m[5]) ? `<span class="hl-kw">${t}</span>` : `<span class="hl-fn">${t}</span>`)
          : KW.has(m[6]) ? `<span class="hl-kw">${t}</span>` : t;
      last = m.index + m[0].length;
    }
    return out + esc(code.slice(last));
  }
  function ansi(text) {
    let out = '', open = false, last = 0, m;
    const re = /\x1b\[([\d;]*)m/g;
    const close = () => { if (open) { out += '</span>'; open = false; } };
    while ((m = re.exec(text))) {
      out += esc(text.slice(last, m.index));
      last = m.index + m[0].length;
      close();
      const codes = m[1].split(';').map(Number);
      const fg = codes.find((c) => (c >= 30 && c <= 37) || (c >= 90 && c <= 97));
      const bold = codes.includes(1);
      if (fg || bold) { out += `<span class="${fg ? 'a' + (fg % 10) : ''}${bold ? ' ab' : ''}">`; open = true; }
    }
    out += esc(text.slice(last));
    close();
    return out.replace(/\x1b\[[\d;]*[A-Za-z]/g, '');
  }
  const joinSrc = (s) => (Array.isArray(s) ? s.join('') : String(s ?? ''));
  const V3 = { png: 'image/png', jpeg: 'image/jpeg', svg: 'image/svg+xml', html: 'text/html', text: 'text/plain', latex: 'text/latex', markdown: 'text/markdown', json: 'application/json' };
  function nbOutput(o, cx) {
    const d = h('div', 'nb-out');
    const t = o.output_type;
    const cap = (s) => (s.length > 200000 ? s.slice(0, 200000) + '\n… (output cut here)' : s);
    if (t === 'stream') { d.appendChild(h('pre', 'nb-text' + (o.name === 'stderr' ? ' err' : ''), ansi(cap(joinSrc(o.text))))); return d; }
    if (t === 'error' || t === 'pyerr') { d.appendChild(h('pre', 'nb-text err', ansi(cap((o.traceback || []).join('\n') || `${o.ename}: ${o.evalue}`)))); return d; }
    let data = o.data;
    if (!data) { data = {}; for (const [k, m] of Object.entries(V3)) if (o[k] !== undefined) data[m] = o[k]; }
    const get = (m) => (data[m] === undefined ? undefined : joinSrc(data[m]));
    let v;
    if ((v = get('image/png')) !== undefined || (v = get('image/jpeg')) !== undefined || (v = get('image/gif')) !== undefined) {
      const mime = data['image/png'] !== undefined ? 'image/png' : data['image/jpeg'] !== undefined ? 'image/jpeg' : 'image/gif';
      const img = h('img', 'nb-img'); img.alt = ''; img.src = `data:${mime};base64,${v.replace(/\s/g, '')}`;
      d.appendChild(img);
    } else if ((v = get('image/svg+xml')) !== undefined) {
      const img = h('img', 'nb-img'); img.alt = ''; img.src = svgURL(v); d.appendChild(img);
    } else if ((v = get('text/html')) !== undefined) {
      const x = h('div', 'nb-html prose');
      x.innerHTML = sanitize(v, true);
      if (/<script/i.test(v) && !x.textContent.trim()) x.appendChild(h('div', 'nb-text muted', 'Interactive output (it needs script, which is not run here)'));
      wireDoc(x, cx);
      d.appendChild(x);
    } else if ((v = get('text/markdown')) !== undefined) {
      const x = h('div', 'nb-html prose'); x.innerHTML = sanitize(mdHtml(v)); wireDoc(x, cx); d.appendChild(x);
    } else if ((v = get('text/plain')) !== undefined || (v = get('text/latex')) !== undefined) {
      d.appendChild(h('pre', 'nb-text', ansi(cap(v))));
    } else if (data['application/json'] !== undefined) {
      d.appendChild(h('pre', 'nb-text', esc(cap(JSON.stringify(data['application/json'], null, 2)))));
    } else d.appendChild(h('div', 'nb-text muted', esc(Object.keys(data).join(', ') || 'empty output')));
    return d;
  }
  VIEW.notebook = async (cx) => {
    const { text } = await cx.src.text(64 * MB);
    let nb;
    try { nb = JSON.parse(text); } catch (e) { throw new Error('Not valid notebook JSON: ' + e.message); }
    const cells = nb.cells || nb.worksheets?.[0]?.cells || [];
    const lang = nb.metadata?.language_info?.name || nb.metadata?.kernelspec?.language || nb.metadata?.language || 'python';
    const b = body(cx, 'kv-scroll');
    const box = h('div', 'kv-nb');
    for (const c of cells) {
      const src = joinSrc(c.source ?? c.input);
      if (c.cell_type === 'markdown' || c.cell_type === 'heading') {
        const md = h('div', 'nb-md prose');
        let s = src;
        if (c.cell_type === 'heading') s = '#'.repeat(c.level || 1) + ' ' + s;
        md.innerHTML = sanitize(mdHtml(s));
        if (c.attachments) md.querySelectorAll('img').forEach((img) => {
          const a = /^attachment:(.+)$/.exec(img.getAttribute('src') || '');
          const att = a && c.attachments[decodeURIComponent(a[1])];
          if (!att) return;
          const [mime, dat] = Object.entries(att)[0];
          img.src = mime === 'image/svg+xml' ? svgURL(joinSrc(dat)) : /^image\//.test(mime) ? `data:${mime};base64,${joinSrc(dat).replace(/\s/g, '')}` : '';
        });
        wireDoc(md, cx);
        box.appendChild(md);
      } else if (c.cell_type === 'code') {
        const cell = h('div', 'nb-cell');
        const n = c.execution_count ?? c.prompt_number;
        cell.appendChild(h('div', 'nb-in', `<span class="nb-n">[${n ?? ' '}]</span><pre class="nb-code">${highlight(src, lang)}</pre>`));
        for (const o of c.outputs || []) cell.appendChild(nbOutput(o, cx));
        box.appendChild(cell);
      } else box.appendChild(h('pre', 'nb-raw', esc(src)));
    }
    if (!cells.length) note(box, 'This notebook has no cells');
    b.appendChild(box);
    cx.info(`${num(cells.length)} cells · ${nb.metadata?.kernelspec?.display_name || lang}`);
  };

  /* ---------------------------------------------------------------- sequences (FASTA / FASTQ) */
  VIEW.fastx = async (cx) => {
    const { text, partial } = await cx.src.text(16 * MB);
    const fq = /^\s*@/.test(text) && !/^\s*>/.test(text);
    const recs = [];
    if (fq) {
      const L = text.split(/\r?\n/);
      for (let i = 0; i + 1 < L.length && recs.length < ROWS_CAP; i += 4) {
        if (!L[i]) { i -= 3; continue; }
        const head = L[i].slice(1), seq = (L[i + 1] || '').trim(), qual = (L[i + 3] || '').trim();
        if (partial && i + 3 >= L.length) break;
        recs.push({ head, seq, qual });
      }
    } else {
      let cur = null, parts = [];
      for (const line of text.split(/\r?\n/)) {
        if (line.startsWith('>')) { if (cur) { cur.seq = parts.join(''); recs.push(cur); if (recs.length >= ROWS_CAP) { cur = null; break; } } cur = { head: line.slice(1) }; parts = []; }
        else if (cur && !line.startsWith(';')) parts.push(line.trim());
      }
      if (cur) { cur.seq = parts.join(''); recs.push(cur); }
      if (partial && recs.length > 1) recs.pop();
    }
    if (!recs.length) throw new Error(`No ${fq ? 'FASTQ' : 'FASTA'} records found`);
    const sample = recs.slice(0, 20).map((r) => r.seq).join('').slice(0, 20000).toUpperCase();
    const protein = /[EFILPQ]/.test(sample);
    let total = 0, gcAll = 0, atgcAll = 0, qSum = 0, qN = 0, q30 = 0;
    const lens = [];
    const rows = recs.map((r) => {
      const s = r.seq;
      let gc = 0, at = 0, nn = 0;
      if (!protein) for (let i = 0; i < s.length; i++) { const c = s.charCodeAt(i) | 32; if (c === 103 || c === 99 || c === 115) gc++; else if (c === 97 || c === 116 || c === 117 || c === 119) at++; else if (c === 110) nn++; }
      total += s.length; gcAll += gc; atgcAll += gc + at; lens.push(s.length);
      const sp = r.head.search(/\s/);
      const row = [sp < 0 ? r.head : r.head.slice(0, sp), String(s.length)];
      if (!protein) row.push(gc + at ? (100 * gc / (gc + at)).toFixed(1) : '', String(nn));
      if (fq) {
        let qs = 0;
        for (let i = 0; i < r.qual.length; i++) { const q = r.qual.charCodeAt(i) - 33; qs += q; if (q >= 30) q30++; }
        qSum += qs; qN += r.qual.length;
        row.push(r.qual.length ? (qs / r.qual.length).toFixed(1) : '');
      }
      row.push(sp < 0 ? '' : r.head.slice(sp + 1));
      row.rec = r;
      return row;
    });
    lens.sort((a, b) => b - a);
    let acc = 0, n50 = 0;
    for (const l of lens) { acc += l; if (acc >= total / 2) { n50 = l; break; } }
    const stats = [['Sequences', num(recs.length) + (partial ? '+' : '')], ['Total length', num(total)], ['Shortest', num(lens[lens.length - 1])], ['Longest', num(lens[0])],
      ['Mean', num(Math.round(total / recs.length))], ['N50', num(n50)]];
    if (!protein) stats.push(['GC', atgcAll ? (100 * gcAll / atgcAll).toFixed(1) + '%' : 'n/a']);
    if (fq && qN) stats.push(['Mean quality', (qSum / qN).toFixed(1)], ['Q30 bases', (100 * q30 / qN).toFixed(1) + '%']);
    const b = body(cx, 'kv-split');
    b.appendChild(h('div', 'kv-stats', stats.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')));
    const cols = ['ID', 'Length'];
    if (!protein) cols.push('GC %', 'N');
    if (fq) cols.push('Mean Q');
    cols.push('Description');
    const detail = h('div', 'kv-detail');
    detail.hidden = true;
    const list = h('div', 'kv-scroll kv-grow');
    list.appendChild(table(rows, { cols, onRow: (row) => showSeq(row.rec) }));
    b.append(list, detail);
    function showSeq(r) {
      detail.hidden = false;
      const s = r.seq.length > 100000 ? r.seq.slice(0, 100000) : r.seq;
      const pad = String(s.length).length;
      let out = '';
      for (let i = 0; i < s.length; i += 60) {
        const chunk = s.slice(i, i + 60);
        const body2 = !protein && i < 30000 ? esc(chunk).replace(/([ACGTUacgtu])\1*/g, (m, c) => `<i class="b-${c.toLowerCase() === 'u' ? 't' : c.toLowerCase()}">${m}</i>`) : esc(chunk);
        out += `<span class="pos">${String(i + 1).padStart(pad)}</span> ${body2.replace(/(.{10})/g, '$1')}\n`;
      }
      detail.innerHTML = `<div class="kv-dhead"><b>${esc(r.head)}</b><span>${num(r.seq.length)} ${protein ? 'aa' : 'bp'}${r.seq.length > s.length ? ' · first 100,000 shown' : ''}</span><button class="kv-btn" title="Close">×</button></div><pre class="kv-seq">${out}</pre>`;
      detail.querySelector('button').onclick = () => { detail.hidden = true; };
    }
    cx.info(`${fq ? 'FASTQ' : 'FASTA'} · ${protein ? 'protein' : 'nucleotide'} · ${num(recs.length)}${partial ? '+' : ''} sequences${partial ? ' · first part of the file' : ''}`);
  };

  /* ---------------------------------------------------------------- genomics tables (VCF, BED, GFF/GTF, SAM) */
  const GCOLS = {
    bed: ['chrom', 'chromStart', 'chromEnd', 'name', 'score', 'strand', 'thickStart', 'thickEnd', 'itemRgb', 'blockCount', 'blockSizes', 'blockStarts'],
    bedgraph: ['chrom', 'start', 'end', 'value'],
    gff: ['seqid', 'source', 'type', 'start', 'end', 'score', 'strand', 'phase', 'attributes'],
    sam: ['QNAME', 'FLAG', 'RNAME', 'POS', 'MAPQ', 'CIGAR', 'RNEXT', 'PNEXT', 'TLEN', 'SEQ', 'QUAL', 'TAGS'],
  };
  VIEW.genome = async (cx) => {
    const { text, partial } = await cx.src.text(16 * MB);
    let ext = extOf(cx.src.name.replace(/\.(gz|bgz)$/i, ''));
    if (ext === 'gff3' || ext === 'gtf') ext = 'gff';
    const head = [], rows = [];
    let cols = GCOLS[ext] || null;
    const lines = text.split(/\r?\n/);
    if (partial) lines.pop();
    for (const line of lines) {
      if (!line) continue;
      if (ext === 'vcf' && line.startsWith('#CHROM')) { cols = line.slice(1).split('\t'); head.push(line); continue; }
      if (line.startsWith('#') || (ext === 'sam' && line.startsWith('@')) || ((ext === 'bed' || ext === 'bedgraph') && /^(track|browser)\b/.test(line))) {
        if (head.length < 20000) head.push(line);
        continue;
      }
      if (rows.length >= ROWS_CAP) break;
      let f = line.includes('\t') ? line.split('\t') : line.trim().split(/\s+/);
      if (ext === 'sam' && f.length > 11) f = [...f.slice(0, 11), f.slice(11).join(' ')];
      rows.push(f);
    }
    const b = body(cx, 'kv-scroll');
    if (ext === 'vcf') {
      const count = (re) => head.filter((l) => re.test(l)).length;
      const ff = (head.find((l) => l.startsWith('##fileformat=')) || '').split('=')[1] || '';
      const ref = (head.find((l) => l.startsWith('##reference=')) || '').slice(12);
      const samples = cols ? Math.max(0, cols.length - 9) : 0;
      const st = [['Format', ff || 'VCF'], ['Variants', num(rows.length) + (partial ? '+' : '')], ['Samples', num(samples)], ['INFO fields', num(count(/^##INFO=/))],
        ['FORMAT fields', num(count(/^##FORMAT=/))], ['Contigs', num(count(/^##contig=/))]];
      if (ref) st.push(['Reference', ref.length > 40 ? '…' + ref.slice(-40) : ref]);
      b.appendChild(h('div', 'kv-stats', st.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')));
    }
    if (head.length) {
      const d = h('details', 'kv-head');
      d.innerHTML = `<summary>Header · ${num(head.length)} line${head.length === 1 ? '' : 's'}</summary><pre>${esc(head.slice(0, 2000).join('\n'))}</pre>`;
      b.appendChild(d);
    }
    b.appendChild(table(rows, { cols: cols || [] }));
    const what = { vcf: 'variants', bed: 'regions', bedgraph: 'intervals', gff: 'features', sam: 'reads' }[ext] || 'rows';
    cx.info(`${ext.toUpperCase()} · ${num(rows.length)}${partial ? '+' : ''} ${what}${partial ? ' · first part of the file' : ''}`);
  };

  /* ---------------------------------------------------------------- binary alignments */
  VIEW.alignment = async (cx) => {
    const ext = extOf(cx.src.name);
    const n = await cx.src.size();
    const b = body(cx, 'kv-scroll');
    const what = { bam: 'Binary alignment file (BAM)', cram: 'Compressed alignment file (CRAM)', bai: 'BAM index (BAI)', crai: 'CRAM index (CRAI)',
      csi: 'Coordinate-sorted index (CSI)', tbi: 'Tabix index (TBI)' }[ext];
    const card = note(b, what, `${sizeTxt(n)}. It is binary, so it is not shown as text. Read it with samtools, for example: samtools view -H ${cx.src.name}`);
    cx.info(`${what} · ${sizeTxt(n)}`);
    if (ext === 'cram') {
      const u = await cx.src.range(0, 26);
      if (utf8(u.subarray(0, 4)) === 'CRAM') card.appendChild(h('span', '', `CRAM version ${u[4]}.${u[5]}`));
      return;
    }
    if (ext !== 'bam') return;
    const { bytes } = await gunzip(await cx.src.range(0, Math.min(n, 8 * MB)), 32 * MB);
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (bytes.length < 12 || utf8(bytes.subarray(0, 4)) !== 'BAM\x01') { card.appendChild(h('span', '', 'The header could not be read.')); return; }
    const lt = dv.getInt32(4, true);
    const htext = utf8(bytes.subarray(8, 8 + Math.min(lt, bytes.length - 8))).replace(/\0+$/, '');
    let p = 8 + lt;
    const refs = [];
    if (p + 4 <= bytes.length) {
      const nref = dv.getInt32(p, true); p += 4;
      for (let i = 0; i < nref && p + 4 <= bytes.length; i++) {
        const ln = dv.getInt32(p, true); p += 4;
        if (p + ln + 4 > bytes.length) break;
        const name = utf8(bytes.subarray(p, p + ln - 1)); p += ln;
        refs.push([name, num(dv.getInt32(p, true))]); p += 4;
      }
    }
    const sorted = (/^@HD.*\tSO:(\S+)/m.exec(htext) || [])[1];
    b.appendChild(h('div', 'kv-stats', [['References', num(refs.length)], ['Sort order', sorted || 'unknown'], ['Read groups', num((htext.match(/^@RG/gm) || []).length)],
      ['Programs', num((htext.match(/^@PG/gm) || []).length)]].map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')));
    if (htext.trim()) { const d = h('details', 'kv-head'); d.innerHTML = `<summary>SAM header · ${num(htext.trim().split('\n').length)} lines</summary><pre>${esc(htext.slice(0, 200000))}</pre>`; b.appendChild(d); }
    if (refs.length) b.appendChild(table(refs, { cols: ['Reference', 'Length'] }));
  };

  /* ---------------------------------------------------------------- molecules (3Dmol.js) */
  const MOLFMT = { pdb: 'pdb', ent: 'pdb', pqr: 'pqr', cif: 'cif', mmcif: 'cif', mcif: 'cif', mol2: 'mol2', sdf: 'sdf', mol: 'sdf', xyz: 'xyz', gro: 'gro' };
  function loseContext(canvas) { try { (canvas.getContext('webgl2') || canvas.getContext('webgl'))?.getExtension('WEBGL_lose_context')?.loseContext(); } catch { /* gone */ } }
  VIEW.molecule = async (cx) => {
    const { text, partial } = await cx.src.text(64 * MB);
    if (partial) throw new Error('This structure file is too big to show in 3D here.');
    await script(LIB.mol);
    const $3D = window.$3Dmol;
    const fmt = MOLFMT[extOf(cx.src.name)] || 'pdb';
    const tools = bar(cx);
    const box = body(cx, 'kv-3d');
    const v = $3D.createViewer(box, { backgroundColor: 'white', backgroundAlpha: 0, antialias: true, id: 'kv3dmol' + Date.now() });
    cx.later(() => { try { const c = box.querySelector('canvas'); v.clear(); if (c) loseContext(c); } catch { /* gone */ } box.innerHTML = ''; });
    const frames = fmt === 'sdf' || fmt === 'xyz';
    const m = frames ? v.addModelsAsFrames(text, fmt) : v.addModel(text, fmt);
    const atoms = m.selectedAtoms({});
    if (!atoms.length) throw new Error(`No atoms found (read as ${fmt.toUpperCase()})`);
    const protein = atoms.filter((a) => a.atom === 'CA' && !a.hetflag).length >= 8 || atoms.filter((a) => a.atom === 'P' && !a.hetflag).length >= 8;
    const STY = {
      auto: () => { if (protein) { v.setStyle({}, { cartoon: { color: 'spectrum' } }); v.setStyle({ hetflag: true }, { stick: { radius: 0.18 } }); v.setStyle({ resn: ['HOH', 'WAT', 'DOD'] }, {}); } else STY.ball(); },
      cartoon: () => v.setStyle({}, { cartoon: { color: 'spectrum' } }),
      ball: () => v.setStyle({}, { stick: { radius: 0.14 }, sphere: { scale: 0.25 } }),
      stick: () => v.setStyle({}, { stick: { radius: 0.2 } }),
      sphere: () => v.setStyle({}, { sphere: {} }),
      line: () => v.setStyle({}, { line: {} }),
    };
    let style = 'auto', spin = false;
    const fit = () => { v.zoomTo(); const w = box.clientWidth, hh = box.clientHeight; if (w && hh > w) v.zoom(w / hh); v.render(); };   // 3Dmol fits the height only
    const draw = () => { STY[style](); v.render(); };
    tools.append(select([['auto', 'Auto style'], ['cartoon', 'Cartoon'], ['ball', 'Ball and stick'], ['stick', 'Sticks'], ['sphere', 'Spheres'], ['line', 'Lines']], style, (s) => { style = s; draw(); }),
      btn('Spin', 'Turn slowly', (e) => { spin = !spin; v.spin(spin ? 'y' : false); e.currentTarget.classList.toggle('on', spin); }),
      btn('Reset', 'Fit the whole structure', fit));
    const nf = frames ? v.getNumFrames() : 1;
    if (nf > 1) {
      let f = 0;
      const lbl = h('span', 'kv-dim', `1 / ${nf}`);
      const go = async (d) => { f = (f + d + nf) % nf; await v.setFrame(f); lbl.textContent = `${f + 1} / ${nf}`; draw(); };
      tools.append(btn('‹', 'Previous structure', () => go(-1)), lbl, btn('›', 'Next structure', () => go(1)));
    }
    tools.appendChild(spacer());
    draw();
    fit();
    let fitted = false;
    const ro = new ResizeObserver(() => { v.resize(); if (!fitted && box.clientWidth) { fitted = true; fit(); } else v.render(); });
    ro.observe(box);
    cx.later(() => { ro.disconnect(); v.spin(false); });
    const title = fmt === 'pdb' ? (text.match(/^TITLE {4}.(.*)$/gm) || []).map((l) => l.slice(10).trim()).join(' ').slice(0, 90) : fmt === 'sdf' ? text.split(/\r?\n/)[0].trim().slice(0, 60) : '';
    const info = [`${num(atoms.length)} atoms`, nf > 1 ? `${nf} structures` : '', protein ? 'macromolecule' : '', title].filter(Boolean).join(' · ');
    tools.appendChild(h('span', 'kv-dim', esc(`${num(atoms.length)} atoms`)));
    cx.info(info);
  };

  /* ---------------------------------------------------------------- 3D models (three.js) */
  VIEW.model = async (cx) => {
    const T = await import(LIB.three);
    const { THREE } = T;
    const ext = extOf(cx.src.name);
    const u8 = await cx.src.bytes(512 * MB);
    const tools = bar(cx);
    const box = body(cx, 'kv-3d');
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.toneMapping = THREE.NeutralToneMapping;
    box.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new T.RoomEnvironment(), 0.04).texture;
    scene.environmentIntensity = 0.55;
    const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 1000);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x777777, 0.5));
    const sun = new THREE.DirectionalLight(0xffffff, 1.1);
    sun.position.set(1, 2, 3);
    camera.add(sun);
    scene.add(camera);
    const controls = new T.OrbitControls(camera, renderer.domElement);
    let raf = 0;
    const render = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (controls.autoRotate) { controls.update(); render(); } renderer.render(scene, camera); }); };
    controls.addEventListener('change', render);
    cx.later(() => {
      cancelAnimationFrame(raf); controls.dispose();
      scene.traverse((o) => { o.geometry?.dispose?.(); [].concat(o.material || []).forEach((m) => { Object.values(m).forEach((x) => x?.isTexture && x.dispose()); m.dispose?.(); }); });
      pmrem.dispose(); renderer.dispose(); renderer.forceContextLoss();
    });
    const mat = () => new THREE.MeshStandardMaterial({ color: 0xb9b4aa, metalness: 0.05, roughness: 0.55, side: THREE.DoubleSide });
    let obj;
    if (ext === 'glb' || ext === 'gltf') {
      const dir = cx.src.path ? dirOf(cx.src.path) : null;
      const manager = new THREE.LoadingManager();
      manager.setURLModifier((u) => (u.startsWith('kvres/') ? (dir ? RAW(dir + sepOf(cx.src.path) + decodeURIComponent(u.slice(6))) : 'data:,') : u));
      const gltf = await new Promise((ok, fail) => new T.GLTFLoader(manager).parse(ab(u8), 'kvres/', ok, (e) => fail(new Error(e?.message || 'Could not read this glTF file'))));
      obj = gltf.scene;
    } else if (ext === 'stl') {
      const g = new T.STLLoader().parse(ab(u8));
      const m = mat();
      if (g.hasColors) { m.vertexColors = true; m.color.set(0xffffff); }
      obj = new THREE.Mesh(g, m);
    } else if (ext === 'obj') {
      obj = new T.OBJLoader().parse(utf8(u8));
      obj.traverse((o) => { if (o.isMesh) o.material = mat(); });
    } else if (ext === 'ply') {
      const g = new T.PLYLoader().parse(ab(u8));
      if (g.index) {
        if (!g.attributes.normal) g.computeVertexNormals();
        const m = mat();
        if (g.attributes.color) { m.vertexColors = true; m.color.set(0xffffff); }
        obj = new THREE.Mesh(g, m);
      } else obj = new THREE.Points(g, new THREE.PointsMaterial({ size: 0.01, vertexColors: !!g.attributes.color, sizeAttenuation: true }));
    }
    scene.add(obj);
    const bb = new THREE.Box3().setFromObject(obj);
    const size = bb.getSize(new THREE.Vector3()), center = bb.getCenter(new THREE.Vector3());
    const r = Math.max(size.x, size.y, size.z) || 1;
    if (obj.isPoints) obj.material.size = r / 400;
    const home = () => {
      camera.near = r / 1000; camera.far = r * 100; camera.updateProjectionMatrix();
      const vf = (camera.fov * Math.PI) / 180, hf = 2 * Math.atan(Math.tan(vf / 2) * camera.aspect);
      const d = (size.length() / 2) / Math.sin(Math.min(vf, hf) / 2) * 1.08;      // the whole bounding sphere fits the narrower side
      camera.position.copy(center).add(new THREE.Vector3(0.6, 0.45, 1).normalize().multiplyScalar(d));
      controls.target.copy(center); controls.update(); render();
    };
    let tris = 0, verts = 0;
    obj.traverse((o) => { const g = o.geometry; if (!g) return; const n = g.attributes.position?.count || 0; verts += n; tris += o.isMesh ? (g.index ? g.index.count : n) / 3 : 0; });
    let wire = false;
    tools.append(btn('Reset', 'Back to the starting view', home),
      btn('Wireframe', '', (e) => { wire = !wire; obj.traverse((o) => [].concat(o.material || []).forEach((m) => { if ('wireframe' in m) m.wireframe = wire; })); e.currentTarget.classList.toggle('on', wire); render(); }),
      btn('Spin', 'Turn slowly', (e) => { controls.autoRotate = !controls.autoRotate; e.currentTarget.classList.toggle('on', controls.autoRotate); render(); }), spacer());
    const info = [tris ? `${num(Math.round(tris))} triangles` : '', `${num(verts)} vertices`, `${+size.x.toPrecision(3)} × ${+size.y.toPrecision(3)} × ${+size.z.toPrecision(3)}`].filter(Boolean).join(' · ');
    tools.appendChild(h('span', 'kv-dim', esc(info)));
    const fitSize = () => { const w = box.clientWidth, hh = box.clientHeight; if (!w || !hh) return; renderer.setSize(w, hh, false); camera.aspect = w / hh; camera.updateProjectionMatrix(); render(); };
    const ro = new ResizeObserver(fitSize);
    ro.observe(box);
    cx.later(() => ro.disconnect());
    fitSize(); home();
    cx.info(info + ' · drag to turn, right-drag to move, scroll to zoom');
  };

  /* ---------------------------------------------------------------- ZIP (Range reads: only the directory and the opened entry) */
  async function zipOpen(src) {
    const size = await src.size();
    const tailLen = Math.min(size, 65557);
    const tail = await src.range(size - tailLen, size);
    const tv = new DataView(tail.buffer, tail.byteOffset, tail.byteLength);
    let e = -1;
    for (let i = tail.length - 22; i >= 0; i--) if (tv.getUint32(i, true) === 0x06054b50) { e = i; break; }
    if (e < 0) throw new Error('Not a ZIP archive (no central directory)');
    let count = tv.getUint16(e + 10, true), cdSize = tv.getUint32(e + 12, true), cdOff = tv.getUint32(e + 16, true);
    if ((cdOff === 0xffffffff || count === 0xffff || cdSize === 0xffffffff) && e >= 20 && tv.getUint32(e - 20, true) === 0x07064b50) {
      const z64 = Number(tv.getBigUint64(e - 12, true));
      const r = await src.range(z64, z64 + 56);
      const rv = new DataView(r.buffer, r.byteOffset, r.byteLength);
      count = Number(rv.getBigUint64(32, true)); cdSize = Number(rv.getBigUint64(40, true)); cdOff = Number(rv.getBigUint64(48, true));
    }
    const cd = await src.range(cdOff, cdOff + cdSize);
    const v = new DataView(cd.buffer, cd.byteOffset, cd.byteLength);
    const entries = [];
    const cp = new TextDecoder('utf-8');
    for (let p = 0; p + 46 <= cd.length && entries.length < count && v.getUint32(p, true) === 0x02014b50;) {
      const flags = v.getUint16(p + 8, true), method = v.getUint16(p + 10, true), time = v.getUint16(p + 12, true), date = v.getUint16(p + 14, true);
      let csize = v.getUint32(p + 20, true), usize = v.getUint32(p + 24, true), off = v.getUint32(p + 42, true);
      const nl = v.getUint16(p + 28, true), xl = v.getUint16(p + 30, true), cl = v.getUint16(p + 32, true);
      const name = cp.decode(cd.subarray(p + 46, p + 46 + nl));
      for (let x = p + 46 + nl; x + 4 <= p + 46 + nl + xl;) {
        const id = v.getUint16(x, true), len = v.getUint16(x + 2, true);
        if (id === 1) {
          let q = x + 4;
          if (usize === 0xffffffff) { usize = Number(v.getBigUint64(q, true)); q += 8; }
          if (csize === 0xffffffff) { csize = Number(v.getBigUint64(q, true)); q += 8; }
          if (off === 0xffffffff) { off = Number(v.getBigUint64(q, true)); }
        }
        x += 4 + len;
      }
      entries.push({ name, method, csize, size: usize, off, encrypted: !!(flags & 1), dir: name.endsWith('/'),
        date: new Date(1980 + (date >> 9), ((date >> 5) & 15) - 1, date & 31, time >> 11, (time >> 5) & 63) });
      p += 46 + nl + xl + cl;
    }
    async function read(en, cap = 64 * MB) {
      if (en.encrypted) throw new Error('This entry is encrypted');
      if (en.size > cap) throw new Error(`Too big to open inside the archive (${sizeTxt(en.size)})`);
      const lh = await src.range(en.off, en.off + 30);
      const lv = new DataView(lh.buffer, lh.byteOffset, lh.byteLength);
      if (lv.getUint32(0, true) !== 0x04034b50) throw new Error('Damaged archive entry');
      const start = en.off + 30 + lv.getUint16(26, true) + lv.getUint16(28, true);
      const data = await src.range(start, start + en.csize);
      if (en.method === 0) return data;
      if (en.method === 8) return (await inflate(data, 'deflate-raw', cap + 1)).bytes;
      throw new Error(`Compression method ${en.method} is not supported here`);
    }
    return { entries, read };
  }

  /* ---------------------------------------------------------------- TAR (also inside gzip), read as a stream */
  function queue(stream) {
    const rd = stream.getReader();
    let chunks = [], have = 0, done = false, pos = 0;
    async function fill(n) { while (have < n && !done) { const r = await rd.read(); if (r.done) { done = true; break; } chunks.push(r.value); have += r.value.length; } }
    return {
      get pos() { return pos; },
      async take(n) {
        await fill(n);
        n = Math.min(n, have);
        const out = new Uint8Array(n);
        let o = 0;
        while (o < n) { const c = chunks[0], k = Math.min(c.length, n - o); out.set(c.subarray(0, k), o); o += k; if (k === c.length) chunks.shift(); else chunks[0] = c.subarray(k); }
        have -= n; pos += n;
        return out;
      },
      async skip(n) {
        while (n > 0) {
          if (!have) { await fill(1); if (!have) return; }
          const c = chunks[0], k = Math.min(c.length, n);
          if (k === c.length) chunks.shift(); else chunks[0] = c.subarray(k);
          have -= k; n -= k; pos += k;
        }
      },
      cancel() { rd.cancel().catch(() => {}); },
    };
  }
  async function tarList(stream, keep) {
    const q = queue(stream);
    const entries = [];
    let longName = null, paxPath = null, kept = 0, stopped = '';
    const str = (u, a, n) => { const s = u.subarray(a, a + n); const z = s.indexOf(0); return new TextDecoder().decode(z < 0 ? s : s.subarray(0, z)); };
    const oct = (u, a, n) => { if (u[a] & 0x80) { let v = 0; for (let i = a + 1; i < a + n; i++) v = v * 256 + u[i]; return v; } return parseInt(str(u, a, n).trim() || '0', 8) || 0; };
    for (;;) {
      if (entries.length >= 20000) { stopped = 'the first 20,000 entries'; break; }
      if (q.pos > 4096 * MB) { stopped = 'the first 4 GB'; break; }
      const hd = await q.take(512);
      if (hd.length < 512 || hd.every((x) => x === 0)) break;
      const type = String.fromCharCode(hd[156] || 48);
      const size = oct(hd, 124, 12);
      let name = str(hd, 0, 100);
      if (str(hd, 257, 5) === 'ustar') { const pre = str(hd, 345, 155); if (pre) name = pre + '/' + name; }
      const pad = (512 - (size % 512)) % 512;
      if (type === 'L' || type === 'x' || type === 'g') {
        const d = await q.take(size); await q.skip(pad);
        const s = new TextDecoder().decode(d);
        if (type === 'L') longName = s.replace(/\0+$/, '');
        else if (type === 'x') { const m = /(?:^|\n)\d+ path=([^\n]*)\n/.exec(s); if (m) paxPath = m[1]; }
        continue;
      }
      name = paxPath || longName || name;
      paxPath = longName = null;
      const en = { name, size, off: q.pos, dir: type === '5' || name.endsWith('/'), link: type === '1' || type === '2', date: new Date(oct(hd, 136, 12) * 1000) };
      if (keep && !en.dir && size <= 16 * MB && kept + size <= 96 * MB) { en.data = await q.take(size); kept += size; await q.skip(pad); }
      else await q.skip(size + pad);
      entries.push(en);
    }
    q.cancel();
    return { entries, stopped };
  }

  /* ---------------------------------------------------------------- archive listing (tree) with a preview of the chosen entry */
  function archiveView(cx, entries, label, open, extraNote) {
    const root = { kids: new Map(), size: 0, files: 0 };
    let total = 0, files = 0;
    for (const e of entries) {
      const parts = e.name.split('/').filter(Boolean);
      if (!parts.length) continue;
      let n = root;
      for (let i = 0; i < parts.length; i++) {
        const last = i === parts.length - 1;
        if (!n.kids.has(parts[i])) n.kids.set(parts[i], { name: parts[i], kids: new Map(), size: 0, files: 0, entry: null });
        const c = n.kids.get(parts[i]);
        if (last && !e.dir) { c.entry = e; c.leaf = true; }
        if (!e.dir) { c.size += e.size; c.files++; }
        n = c;
      }
      if (!e.dir) { total += e.size; files++; }
    }
    const b = body(cx, 'kv-split');
    const list = h('div', 'kv-scroll kv-grow kv-tree');
    const detail = h('div', 'kv-detail');
    detail.hidden = true;
    b.append(list, detail);
    const sorted = (n) => [...n.kids.values()].sort((a, c) => (!!a.leaf - !!c.leaf) || a.name.localeCompare(c.name, undefined, { numeric: true }));
    const row = (n) => `<span class="nm">${esc(n.name)}</span><span class="sz">${n.leaf ? sizeTxt(n.size) : `${num(n.files)} file${n.files === 1 ? '' : 's'} · ${sizeTxt(n.size)}`}</span>`;
    function render(n, depth) {
      if (n.leaf) {
        const r = leaf(FILE_ICON + row(n));
        r.classList.add('file');
        r.onclick = () => { list.querySelectorAll('.kv-row.on').forEach((x) => x.classList.remove('on')); r.classList.add('on'); show(n.entry); };
        return r;
      }
      return branch(DIR_ICON + row(n), (kids) => inChunks(sorted(n), kids, (c) => render(c, depth + 1)), depth === 0 && root.kids.size === 1);
    }
    inChunks(sorted(root), list, (c) => render(c, 0));
    let inner = null;
    async function show(e) {
      inner?.dispose();
      detail.hidden = false;
      detail.innerHTML = `<div class="kv-dhead"><b>${esc(e.name)}</b><span>${sizeTxt(e.size)}</span><button class="kv-btn" title="Close">×</button></div>`;
      detail.querySelector('button').onclick = () => { inner?.dispose(); inner = null; detail.hidden = true; };
      const host = h('div', 'kv-inner');
      detail.appendChild(host);
      try {
        if (e.link) throw new Error('A link to another entry');
        const bytes = await open(e);
        inner = mount(host, memSrc(e.name.split('/').pop(), bytes), null, () => {});
      } catch (err) { note(host, 'Cannot open this entry', err.message); }
    }
    cx.later(() => inner?.dispose());
    cx.info(`${label} · ${num(files)} files · ${sizeTxt(total)} unpacked${extraNote ? ' · ' + extraNote : ''}`);
  }
  const FILE_ICON = '<svg class="fi" viewBox="0 0 16 16"><path d="M9 2H4.5A1.5 1.5 0 0 0 3 3.5v9A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5V6z"/><path d="M9 2v4h4"/></svg>';
  const DIR_ICON = '<svg class="fi" viewBox="0 0 16 16"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"/></svg>';

  VIEW.zip = async (cx) => {
    const z = await zipOpen(cx.src);
    archiveView(cx, z.entries, 'ZIP', (e) => z.read(e));
  };
  VIEW.tar = async (cx) => {
    const stream = cx.src.path ? (await fetch(cx.src.url)).body : new Blob([await cx.src.bytes()]).stream();
    const { entries, stopped } = await tarList(stream, false);
    archiveView(cx, entries, 'TAR', (e) => cx.src.range(e.off, e.off + e.size), stopped && 'listing stopped after ' + stopped);
  };
  VIEW.targz = async (cx) => {
    const raw = cx.src.path ? (await fetch(cx.src.url)).body : new Blob([await cx.src.bytes()]).stream();
    const { entries, stopped } = await tarList(raw.pipeThrough(new DecompressionStream('gzip')), true);
    archiveView(cx, entries, 'TAR.GZ', async (e) => { if (!e.data) throw new Error('Too big to open inside a compressed archive'); return e.data; }, stopped && 'listing stopped after ' + stopped);
  };
  VIEW.gz = async (cx) => {
    const inner = cx.src.name.replace(/\.(gz|gzip|bgz)$/i, '') || 'file';
    const k = kind(inner);
    if (k?.viewer === 'tar') return VIEW.targz(cx);
    const n = await cx.src.size();
    const { bytes, partial } = await gunzip(await cx.src.range(0, Math.min(n, 8 * MB)), 16 * MB);
    const more = partial || n > 8 * MB;
    cx.later(mount(cx.root, memSrc(inner, bytes, more), null, (s) => cx.info(`gzip, ${sizeTxt(n)} · ${s}${more ? ' (start of the file)' : ''}`)).dispose);
  };

  /* ---------------------------------------------------------------- Word (mammoth + WYSIWYG OOXML editor) */
  VIEW.docx = async (cx) => {
    await script(LIB.edit);
    return window.KCV_EDIT.docx(cx, editHelpers());
  };

  /* ---------------------------------------------------------------- spreadsheets (SheetJS + inline grid editor) */
  VIEW.sheet = async (cx) => {
    await script(LIB.edit);
    return window.KCV_EDIT.sheet(cx, editHelpers());
  };

  /* ---------------------------------------------------------------- PowerPoint (own reader: slide XML in the zip) */
  const RNS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
  const kidsOf = (n, name) => (n ? [...n.children].filter((c) => c.localName === name) : []);
  const kid = (n, name) => (n ? [...n.children].find((c) => c.localName === name) || null : null);
  const desc = (n, name) => (n ? n.getElementsByTagNameNS('*', name)[0] || null : null);
  const at = (n, a) => (n ? n.getAttribute(a) : null);
  function joinZip(dir, rel) {
    if (rel.startsWith('/')) return rel.slice(1);
    const parts = dir.split('/').filter(Boolean);
    for (const p of rel.split('/')) { if (p === '..') parts.pop(); else if (p && p !== '.') parts.push(p); }
    return parts.join('/');
  }
  function hsl(hex) {
    const r = parseInt(hex.slice(0, 2), 16) / 255, g = parseInt(hex.slice(2, 4), 16) / 255, b = parseInt(hex.slice(4, 6), 16) / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    let hh = 0, s = 0;
    if (mx !== mn) { const d = mx - mn; s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn); hh = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4; hh /= 6; }
    return [hh, s, l];
  }
  function hex(hh, s, l) {
    const f = (n) => { const k = (n + hh * 12) % 12, a = s * Math.min(l, 1 - l); return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)))).toString(16).padStart(2, '0'); };
    return f(0) + f(8) + f(4);
  }
  function color(c, theme) {
    if (!c) return null;
    const MAP = { tx1: 'dk1', tx2: 'dk2', bg1: 'lt1', bg2: 'lt2' };
    let v = c.localName === 'srgbClr' ? at(c, 'val') : c.localName === 'schemeClr' ? theme.colors[MAP[at(c, 'val')] || at(c, 'val')]
      : c.localName === 'sysClr' ? at(c, 'lastClr') || (at(c, 'val') === 'window' ? 'FFFFFF' : '000000') : c.localName === 'prstClr' ? ({ black: '000000', white: 'FFFFFF', red: 'FF0000' }[at(c, 'val')] || '000000') : null;
    if (!v || !/^[0-9a-f]{6}$/i.test(v)) return null;
    let alpha = 1;
    for (const m of c.children) {
      const x = +at(m, 'val') / 100000;
      let [hh, s, l] = hsl(v);
      if (m.localName === 'lumMod') l *= x; else if (m.localName === 'lumOff') l += x; else if (m.localName === 'shade') l *= x; else if (m.localName === 'tint') l += (1 - l) * (1 - x); else if (m.localName === 'alpha') { alpha = x; continue; } else continue;
      v = hex(hh, s, Math.max(0, Math.min(1, l)));
    }
    return alpha < 1 ? `rgba(${parseInt(v.slice(0, 2), 16)},${parseInt(v.slice(2, 4), 16)},${parseInt(v.slice(4, 6), 16)},${alpha})` : '#' + v;
  }
  const fillOf = (n, theme) => { if (!n) return undefined; if (kid(n, 'noFill')) return null; const sf = kid(n, 'solidFill'); if (sf) return color(sf.firstElementChild, theme); const gf = kid(n, 'gradFill'); if (gf) return color(desc(gf, 'gs')?.firstElementChild, theme); return undefined; };

  VIEW.pptx = async (cx) => {
    const u8 = await cx.src.bytes(300 * MB);
    const z = await zipOpen(memSrc(cx.src.name, u8));
    const files = new Map(z.entries.map((e) => [e.name, e]));
    const bytesOf = async (p) => { const e = files.get(p); return e ? z.read(e) : null; };
    const xml = async (p) => { const b = await bytesOf(p); return b ? new DOMParser().parseFromString(utf8(b), 'application/xml') : null; };
    const relsOf = async (p) => {
      const dir = p.slice(0, p.lastIndexOf('/') + 1);
      const d = await xml(dir + '_rels/' + p.slice(dir.length) + '.rels');
      const m = {};
      if (d) for (const r of d.getElementsByTagNameNS('*', 'Relationship')) m[at(r, 'Id')] = { type: (at(r, 'Type') || '').split('/').pop(), target: at(r, 'TargetMode') === 'External' ? null : joinZip(dir, at(r, 'Target') || '') };
      return m;
    };
    const parts = new Map();
    async function part(p) {
      if (!p) return null;
      if (!parts.has(p)) parts.set(p, (async () => {
        const doc = await xml(p);
        if (!doc) return null;
        const rels = await relsOf(p);
        const ph = { idx: {}, type: {} };
        for (const sp of doc.getElementsByTagNameNS('*', 'sp')) {
          const x = desc(kid(sp, 'nvSpPr'), 'ph');
          if (!x) continue;
          const t = at(x, 'type') || 'obj';
          if (at(x, 'idx') != null) ph.idx[at(x, 'idx')] ||= sp;
          ph.type[t] ||= sp;
        }
        return { doc, rels, ph, path: p };
      })());
      return parts.get(p);
    }
    const relOfType = (P, type) => P && Object.values(P.rels).find((r) => r.type === type)?.target;
    const themes = new Map();
    async function themeOf(p) {
      const th = { colors: { dk1: '000000', lt1: 'FFFFFF', dk2: '44546A', lt2: 'E7E6E6', accent1: '4472C4', accent2: 'ED7D31', accent3: 'A5A5A5', accent4: 'FFC000', accent5: '5B9BD5', accent6: '70AD47' }, major: '', minor: '' };
      if (!p) return th;
      if (themes.has(p)) return themes.get(p);
      const d = await xml(p);
      const cs = desc(d?.documentElement, 'clrScheme');
      if (cs) for (const c of cs.children) { const v = c.firstElementChild; const val = v && (at(v, 'val') && v.localName === 'srgbClr' ? at(v, 'val') : at(v, 'lastClr')); if (val) th.colors[c.localName] = val; }
      th.major = at(desc(desc(d?.documentElement, 'majorFont'), 'latin'), 'typeface') || '';
      th.minor = at(desc(desc(d?.documentElement, 'minorFont'), 'latin'), 'typeface') || '';
      themes.set(p, th);
      return th;
    }
    const pres = await xml('ppt/presentation.xml');
    if (!pres) throw new Error('Not a PowerPoint file (no ppt/presentation.xml)');
    const presRels = await relsOf('ppt/presentation.xml');
    const sz = desc(pres.documentElement, 'sldSz');
    const W = +at(sz, 'cx') || 12192000, H = +at(sz, 'cy') || 6858000;
    const ids = kidsOf(desc(pres.documentElement, 'sldIdLst'), 'sldId').map((s) => s.getAttributeNS(RNS, 'id') || at(s, 'r:id'));
    const b = body(cx, 'kv-scroll kv-slides');
    const slideBindings = [];
    const cq = (emu) => `${(emu / W) * 100}cqw`;
    const pct = (v, of) => `${(v / of) * 100}%`;

    async function picURL(P, rid) {
      const t = P.rels[rid]?.target;
      if (!t) return null;
      const e = extOf(t);
      const data = await bytesOf(t);
      if (!data) return null;
      if (e === 'svg') return svgURL(utf8(data));
      if (e === 'tif' || e === 'tiff') {
        try {
          const { canvas } = await decodeTiffCanvas(ab(data));
          const blob = await new Promise((r) => canvas.toBlob(r, 'image/png'));
          const u = URL.createObjectURL(blob);
          cx.later(() => URL.revokeObjectURL(u));
          return u;
        } catch { return null; }
      }
      return IMGTYPE[e] ? objURL(cx, data, IMGTYPE[e]) : null;
    }
    async function bgOf(P, theme) {
      const bg = desc(desc(P?.doc.documentElement, 'cSld'), 'bg');
      if (!bg) return undefined;
      const pr = kid(bg, 'bgPr');
      if (pr) {
        const blip = desc(pr, 'blip');
        if (blip) { const u = await picURL(P, blip.getAttributeNS(RNS, 'embed')); if (u) return `center / cover no-repeat url("${u}")`; }
        const f = fillOf(pr, theme);
        if (f) return f;
      }
      const ref = kid(bg, 'bgRef');
      if (ref) return color(ref.firstElementChild, theme) || undefined;
      return undefined;
    }

    for (let si = 0; si < ids.length; si++) {
      const sp = presRels[ids[si]]?.target;
      const S = await part(sp);
      if (!S) continue;
      const L = await part(relOfType(S, 'slideLayout'));
      const M = await part(relOfType(L, 'slideMaster'));
      const theme = await themeOf(relOfType(M, 'theme'));
      const txStyles = desc(M?.doc.documentElement, 'txStyles');
      const wrap = h('div', 'kv-slidewrap');
      const hidden = at(S.doc.documentElement, 'show') === '0';
      wrap.appendChild(h('div', 'kv-slideno', `${si + 1}${hidden ? ' · hidden' : ''}`));
      const slide = h('div', 'kv-slide');
      slide.style.aspectRatio = `${W} / ${H}`;
      slide.style.background = (await bgOf(S, theme)) || (await bgOf(L, theme)) || (await bgOf(M, theme)) || '#fff';
      wrap.appendChild(slide);
      b.appendChild(wrap);

      const phOf = (node) => { const x = desc(node.firstElementChild, 'ph'); return x ? { type: at(x, 'type') || 'obj', idx: at(x, 'idx') } : null; };
      const chainOf = (ph) => {
        if (!ph) return [];
        const out = [];
        const l = L && ((ph.idx != null && L.ph.idx[ph.idx]) || L.ph.type[ph.type]);
        if (l) out.push(l);
        const mt = { ctrTitle: 'title', subTitle: 'body', obj: 'body' }[ph.type] || ph.type;
        const m = M && M.ph.type[mt];
        if (m) out.push(m);
        return out;
      };
      const xfrmOf = (n) => kid(kid(n, 'spPr') || kid(n, 'grpSpPr'), 'xfrm') || kid(n, 'xfrm');
      function box(n, chain, tf) {
        let x = xfrmOf(n);
        for (const c of chain) { if (x) break; x = xfrmOf(c); }
        if (!x || !kid(x, 'off')) return null;
        const off = kid(x, 'off'), ext = kid(x, 'ext');
        return { ...tf({ x: +at(off, 'x'), y: +at(off, 'y'), w: +at(ext, 'cx'), h: +at(ext, 'cy') }), rot: +(at(x, 'rot') || 0) / 60000 };
      }
      function place(d, g) {
        d.style.left = pct(g.x, W); d.style.top = pct(g.y, H); d.style.width = pct(g.w, W); d.style.height = pct(g.h, H);
        if (g.rot) d.style.transform = `rotate(${g.rot}deg)`;
      }
      function textBox(n, chain, ph, styleFont) {
        const tx = kid(n, 'txBody');
        if (!tx) return null;
        const isTitle = ph && /title/i.test(ph.type);
        const masterStyle = kid(txStyles, isTitle ? 'titleStyle' : ph ? 'bodyStyle' : 'otherStyle');
        const lst = [kid(tx, 'lstStyle'), ...chain.map((c) => kid(kid(c, 'txBody'), 'lstStyle')), masterStyle];
        const bodyPrs = [kid(tx, 'bodyPr'), ...chain.map((c) => kid(kid(c, 'txBody'), 'bodyPr'))];
        const anchor = bodyPrs.map((x) => at(x, 'anchor')).find(Boolean) || 't';
        const scale = +(at(desc(kid(tx, 'bodyPr'), 'normAutofit'), 'fontScale') || 100000) / 100000;
        const ins = (k, d) => { const v = bodyPrs.map((x) => at(x, k)).find((y) => y != null); return v == null ? d : +v; };
        const d = h('div', 'kv-tx');
        d.style.justifyContent = { t: 'flex-start', ctr: 'center', b: 'flex-end' }[anchor] || 'flex-start';
        d.style.padding = `${cq(ins('tIns', 45720))} ${cq(ins('rIns', 91440))} ${cq(ins('bIns', 45720))} ${cq(ins('lIns', 91440))}`;
        const font = isTitle ? theme.major : theme.minor;
        if (font) d.style.fontFamily = `"${font}", var(--sans)`;
        let auto = 0;
        for (const p of kidsOf(tx, 'p')) {
          const pPr = kid(p, 'pPr');
          const lvl = +(at(pPr, 'lvl') || 0);
          const look = (fn) => { for (const s of [pPr, ...lst.map((l) => kid(l, `lvl${lvl + 1}pPr`))]) { const v = s && fn(s); if (v !== undefined && v !== null) return v; } return null; };
          const defR = (fn) => look((s) => { const r = kid(s, 'defRPr'); return r ? fn(r) : null; });
          const pe = h('div', 'kv-p');
          const algn = look((s) => at(s, 'algn'));
          pe.style.textAlign = { ctr: 'center', r: 'right', just: 'justify' }[algn] || 'left';
          const marL = look((s) => at(s, 'marL'));
          if (marL) pe.style.paddingLeft = cq(+marL);
          const bu = look((s) => (kid(s, 'buNone') ? 'none' : kid(s, 'buChar') ? at(kid(s, 'buChar'), 'char') : kid(s, 'buAutoNum') ? 'num' : null));
          const runs = [...p.children].filter((c) => c.localName === 'r' || c.localName === 'br' || c.localName === 'fld');
          const baseSz = (+(defR((r) => at(r, 'sz')) || (isTitle ? 4400 : 1800)) / 100) * scale;
          if (bu && bu !== 'none' && runs.some((r) => r.textContent.trim())) {
            auto++;
            pe.appendChild(h('span', 'kv-bu', esc(bu === 'num' ? auto + '.' : bu)));
            pe.lastChild.style.fontSize = cq(baseSz * 12700);
          }
          let any = false;
          for (const r of runs) {
            if (r.localName === 'br') { pe.appendChild(document.createElement('br')); continue; }
            const rPr = kid(r, 'rPr');
            const s = h('span');
            s.textContent = kid(r, 't')?.textContent ?? '';
            const szv = (at(rPr, 'sz') ? +at(rPr, 'sz') / 100 * scale : baseSz);
            s.style.fontSize = cq(szv * 12700);
            s.dataset.pt = String(Math.round(szv * 10) / 10);
            const bold = at(rPr, 'b') ?? defR((x) => at(x, 'b'));
            if (bold === '1' || bold === 'true') s.style.fontWeight = '700';
            if ((at(rPr, 'i') ?? defR((x) => at(x, 'i'))) === '1') s.style.fontStyle = 'italic';
            if (at(rPr, 'u') && at(rPr, 'u') !== 'none') s.style.textDecoration = 'underline';
            const c = (rPr && fillOf(rPr, theme)) ?? styleFont ?? defR((x) => fillOf(x, theme) ?? null);
            if (c) s.style.color = c;
            const lf = at(kid(rPr, 'latin'), 'typeface');
            if (lf && !lf.startsWith('+')) s.style.fontFamily = `"${lf}", var(--sans)`;
            pe.appendChild(s);
            any = true;
          }
          if (!any) { const s = h('span', '', '&nbsp;'); s.style.fontSize = cq((+(at(kid(p, 'endParaRPr'), 'sz') || 0) / 100 * scale || baseSz) * 12700); pe.appendChild(s); }
          d.appendChild(pe);
        }
        slideBindings.push({ slidePath: S.path, slideDoc: S.doc, txNode: tx, domEl: d, slideW: W });
        return d;
      }
      async function shape(n, tf, parent) {
        const tag = n.localName;
        if (tag === 'grpSp') {
          const x = xfrmOf(n);
          let sub = tf;
          if (x && kid(x, 'chOff')) {
            const off = kid(x, 'off'), ext = kid(x, 'ext'), co = kid(x, 'chOff'), ce = kid(x, 'chExt');
            const sx = (+at(ext, 'cx') || 1) / (+at(ce, 'cx') || 1), sy = (+at(ext, 'cy') || 1) / (+at(ce, 'cy') || 1);
            sub = (g) => tf({ x: +at(off, 'x') + (g.x - +at(co, 'x')) * sx, y: +at(off, 'y') + (g.y - +at(co, 'y')) * sy, w: g.w * sx, h: g.h * sy });
          }
          for (const c of n.children) await shape(c, sub, parent);
          return;
        }
        if (!['sp', 'pic', 'graphicFrame', 'cxnSp'].includes(tag)) return;
        const ph = phOf(n);
        const chain = chainOf(ph);
        const g = box(n, chain, tf);
        if (!g) return;
        const d = h('div', 'kv-shape');
        place(d, g);
        const spPr = kid(n, 'spPr');
        const st = kid(n, 'style');
        if (tag === 'sp' || tag === 'cxnSp') {
          let fill = fillOf(spPr, theme);
          if (fill === undefined && st && +at(kid(st, 'fillRef'), 'idx') > 0 && tag === 'sp') fill = color(kid(st, 'fillRef')?.firstElementChild, theme);
          if (fill) d.style.background = fill;
          const ln = kid(spPr, 'ln');
          let line = ln ? fillOf(ln, theme) : undefined;
          if (line === undefined && st && +at(kid(st, 'lnRef'), 'idx') > 0) line = color(kid(st, 'lnRef')?.firstElementChild, theme);
          if (line) { const w = Math.max(1, (+at(ln, 'w') || 12700) / 12700); d.style.border = `${w}px solid ${line}`; if (tag === 'cxnSp') { d.style.borderWidth = `${w}px 0 0 0`; d.style.height = '0'; } }
          const prst = at(desc(spPr, 'prstGeom'), 'prst');
          if (prst === 'ellipse') d.style.borderRadius = '50%'; else if (prst === 'roundRect') d.style.borderRadius = '6cqw';
          const fontColor = st ? color(kid(st, 'fontRef')?.firstElementChild, theme) : null;
          const t = tag === 'sp' && textBox(n, chain, ph, fontColor);
          if (t) d.appendChild(t);
          if (!fill && !line && !(t && t.textContent.trim())) return;
        } else if (tag === 'pic') {
          const blip = desc(kid(n, 'blipFill'), 'blip');
          const u = blip && await picURL(S, blip.getAttributeNS(RNS, 'embed'));
          if (u) { const img = h('img'); img.alt = ''; img.src = u; d.appendChild(img); } else d.appendChild(h('div', 'kv-ph', 'image'));
        } else if (tag === 'graphicFrame') {
          const tbl = desc(n, 'tbl');
          if (tbl) {
            const t = h('table', 'kv-stbl');
            for (const tr of kidsOf(tbl, 'tr')) {
              const r = h('tr');
              for (const tc of kidsOf(tr, 'tc')) {
                if (at(tc, 'hMerge') === '1' || at(tc, 'vMerge') === '1') continue;
                const td = h('td');
                if (at(tc, 'gridSpan')) td.colSpan = +at(tc, 'gridSpan');
                if (at(tc, 'rowSpan')) td.rowSpan = +at(tc, 'rowSpan');
                const f = fillOf(kid(tc, 'tcPr'), theme);
                if (f) td.style.background = f;
                const tb = textBox(tc, [], null, null);
                if (tb) { tb.style.padding = '0'; td.appendChild(tb); }
                r.appendChild(td);
              }
              t.appendChild(r);
            }
            d.appendChild(t);
          } else d.appendChild(h('div', 'kv-ph', desc(n, 'chart') ? 'chart' : 'diagram'));
        }
        parent.appendChild(d);
      }
      const tree = desc(desc(S.doc.documentElement, 'cSld'), 'spTree');
      for (const c of tree ? tree.children : []) await shape(c, (g) => g, slide);
    }
    if (!ids.length) note(b, 'This presentation has no slides');
    cx.info(`PowerPoint · ${num(ids.length)} slide${ids.length === 1 ? '' : 's'}`);
    if (cx.src.path && slideBindings.length) {
      await script(LIB.edit);
      window.KCV_EDIT.attachPptx(cx, editHelpers(), { u8, slideBindings, slideCount: ids.length });
    }
  };

  /* ---------------------------------------------------------------- fonts */
  let fontSeq = 0;
  async function sfntNames(u8) {
    const v = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const tag = utf8(u8.subarray(0, 4));
    const tables = {};
    if (tag === 'wOFF') {
      const n = v.getUint16(12);
      for (let i = 0; i < n; i++) { const p = 44 + i * 20; tables[utf8(u8.subarray(p, p + 4))] = { off: v.getUint32(p + 4), len: v.getUint32(p + 8), orig: v.getUint32(p + 12) }; }
    } else if (tag === 'wOF2') return { format: 'WOFF2' };
    else {
      const n = v.getUint16(4);
      for (let i = 0; i < n; i++) { const p = 12 + i * 16; tables[utf8(u8.subarray(p, p + 4))] = { off: v.getUint32(p + 8), len: v.getUint32(p + 12) }; }
    }
    const get = async (t) => { const x = tables[t]; if (!x) return null; let d = u8.subarray(x.off, x.off + x.len); if (x.orig && x.len < x.orig) d = (await inflate(d, 'deflate')).bytes; return new DataView(d.buffer, d.byteOffset, d.byteLength); };
    const out = { format: tag === 'wOFF' ? 'WOFF' : tag === 'OTTO' ? 'OpenType (CFF)' : 'TrueType' };
    const nm = await get('name');
    if (nm) {
      const count = nm.getUint16(2), so = nm.getUint16(4);
      const found = {};
      for (let i = 0; i < count; i++) {
        const p = 6 + i * 12;
        const pid = nm.getUint16(p), lang = nm.getUint16(p + 4), id = nm.getUint16(p + 6), len = nm.getUint16(p + 8), off = nm.getUint16(p + 10);
        if (![1, 2, 4, 5, 16, 17].includes(id)) continue;
        const bytes = new Uint8Array(nm.buffer, nm.byteOffset + so + off, Math.min(len, nm.byteLength - so - off));
        let s;
        if (pid === 3 || pid === 0) { s = ''; for (let j = 0; j + 1 < bytes.length; j += 2) s += String.fromCharCode((bytes[j] << 8) | bytes[j + 1]); }
        else if (pid === 1) s = String.fromCharCode(...bytes);
        else continue;
        const score = (pid === 3 && lang === 0x409 ? 3 : pid === 3 ? 2 : 1);
        if (!found[id] || found[id].score < score) found[id] = { s, score };
      }
      out.family = (found[16] || found[1])?.s; out.style = (found[17] || found[2])?.s; out.full = found[4]?.s; out.version = found[5]?.s;
    }
    const mx = await get('maxp');
    if (mx) out.glyphs = mx.getUint16(4);
    return out;
  }
  VIEW.font = async (cx) => {
    const u8 = await cx.src.bytes(64 * MB);
    const fam = 'kv-font-' + (++fontSeq);
    const face = new FontFace(fam, ab(u8));
    await face.load().catch(() => { throw new Error('The browser could not load this font'); });
    document.fonts.add(face);
    cx.later(() => document.fonts.delete(face));
    const meta = await sfntNames(u8).catch(() => ({}));
    const tools = bar(cx);
    const input = h('input', 'kv-input');
    input.placeholder = 'Type to try the font';
    input.spellcheck = false;
    tools.append(input);
    const b = body(cx, 'kv-scroll');
    const sheet = h('div', 'kv-font');
    sheet.style.setProperty('--f', `"${fam}"`);
    const PANGRAM = 'The quick brown fox jumps over the lazy dog';
    sheet.innerHTML = `<div class="kv-fhead"><div class="big" style="font-family:var(--f), var(--mono)">Aa</div><div><b>${esc(meta.full || meta.family || cx.src.name)}</b>` +
      `<span>${esc([meta.style, meta.format, meta.glyphs ? num(meta.glyphs) + ' glyphs' : '', meta.version].filter(Boolean).join(' · '))}</span></div></div>` +
      `<div class="kv-chars" style="font-family:var(--f), var(--mono)">ABCDEFGHIJKLMNOPQRSTUVWXYZ<br>abcdefghijklmnopqrstuvwxyz<br>0123456789 &amp;@#%?!.,:;'"()[]{}/\\*+-=&lt;&gt;€£$</div>` +
      [12, 16, 20, 28, 40, 64].map((s) => `<div class="kv-sample"><span class="kv-dim">${s}px</span><div class="t" style="font-family:var(--f), var(--mono);font-size:${s}px">${PANGRAM}</div></div>`).join('');
    input.oninput = () => sheet.querySelectorAll('.kv-sample .t').forEach((t) => { t.textContent = input.value || PANGRAM; });
    b.appendChild(sheet);
    cx.info([meta.family, meta.format, sizeTxt(u8.length)].filter(Boolean).join(' · '));
  };

  /* ---------------------------------------------------------------- SQLite (own reader of the file format: no wasm needed) */
  async function sqliteOpen(src) {
    const size = await src.size();
    const whole = size <= 96 * MB ? await src.bytes() : null;
    const hdr = whole ? whole.subarray(0, 100) : await src.range(0, 100);
    if (utf8(hdr.subarray(0, 15)) !== 'SQLite format 3') throw new Error('Not an SQLite database');
    const hv = new DataView(hdr.buffer, hdr.byteOffset, 100);
    let ps = hv.getUint16(16);
    if (ps === 1) ps = 65536;
    const U = ps - hdr[20];
    const enc = hv.getUint32(56);
    const dec = new TextDecoder(enc === 2 ? 'utf-16le' : enc === 3 ? 'utf-16be' : 'utf-8');
    const cache = new Map();
    async function page(n) {
      if (whole) return whole.subarray((n - 1) * ps, n * ps);
      const blk = Math.floor(((n - 1) * ps) / MB);
      if (!cache.has(blk)) { if (cache.size > 64) cache.clear(); cache.set(blk, src.range(blk * MB, (blk + 1) * MB)); }
      const b = await cache.get(blk);
      const o = (n - 1) * ps - blk * MB;
      return b.subarray(o, o + ps);
    }
    const varint = (b, i) => { let v = 0; for (let k = 0; k < 8; k++) { const c = b[i + k]; v = v * 128 + (c & 0x7f); if (c < 0x80) return [v, k + 1]; } return [v * 256 + b[i + 8], 9]; };
    async function payload(pg, off, P, table) {
      const X = table ? U - 35 : Math.floor(((U - 12) * 64) / 255) - 23;
      if (P <= X) return pg.subarray(off, off + P);
      const Mn = Math.floor(((U - 12) * 32) / 255) - 23;
      const K = Mn + ((P - Mn) % (U - 4));
      const local = K <= X ? K : Mn;
      const out = new Uint8Array(P);
      out.set(pg.subarray(off, off + local));
      let got = local;
      let next = new DataView(pg.buffer, pg.byteOffset).getUint32(off + local);
      for (let guard = 0; got < P && next && guard < 100000; guard++) {
        const op = await page(next);
        const n = Math.min(P - got, U - 4);
        out.set(op.subarray(4, 4 + n), got);
        got += n;
        next = new DataView(op.buffer, op.byteOffset).getUint32(0);
      }
      return out;
    }
    function record(b) {
      const [hs, n0] = varint(b, 0);
      const types = [];
      for (let i = n0; i < hs;) { const [t, n] = varint(b, i); types.push(t); i += n; }
      const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
      let p = hs;
      const out = [];
      const int = (n) => { let x = 0; for (let i = 0; i < n; i++) x = x * 256 + b[p + i]; if (b[p] & 0x80) x -= 2 ** (8 * n); return x; };
      for (const t of types) {
        if (t === 0) out.push(null);
        else if (t >= 1 && t <= 6) { const n = [0, 1, 2, 3, 4, 6, 8][t]; out.push(t === 6 ? v.getBigInt64(p).toString() : int(n)); p += n; }
        else if (t === 7) { out.push(v.getFloat64(p)); p += 8; }
        else if (t === 8 || t === 9) out.push(t - 8);
        else if (t >= 12 && t % 2 === 0) { const n = (t - 12) / 2; out.push({ blob: n, head: b.subarray(p, p + Math.min(n, 16)) }); p += n; }
        else if (t >= 13) { const n = (t - 13) / 2; out.push(dec.decode(b.subarray(p, p + n))); p += n; }
      }
      return out;
    }
    /* walk a b-tree in order, calling fn(rowid, values) until it returns false */
    async function walk(root, fn) {
      let go = true;
      async function visit(n, depth) {
        if (!go || depth > 40) return;
        const pg = await page(n);
        const o = n === 1 ? 100 : 0;
        const t = pg[o];
        const dv = new DataView(pg.buffer, pg.byteOffset, pg.byteLength);
        const cells = dv.getUint16(o + 3);
        const hl = t === 2 || t === 5 ? 12 : 8;
        for (let i = 0; i < cells && go; i++) {
          let c = dv.getUint16(o + hl + i * 2);
          if (t === 13) { const [P, a] = varint(pg, c); const [rid, b2] = varint(pg, c + a); go = fn(rid, record(await payload(pg, c + a + b2, P, true))) !== false; }
          else if (t === 10) { const [P, a] = varint(pg, c); go = fn(null, record(await payload(pg, c + a, P, false))) !== false; }
          else if (t === 5) { await visit(dv.getUint32(c), depth + 1); }
          else if (t === 2) { await visit(dv.getUint32(c), depth + 1); c += 4; if (!go) break; const [P, a] = varint(pg, c); go = fn(null, record(await payload(pg, c + a, P, false))) !== false; }
          else throw new Error('Damaged page ' + n);
        }
        if ((t === 5 || t === 2) && go) await visit(dv.getUint32(o + 8), depth + 1);
      }
      await visit(root, 0);
    }
    async function count(root, limit = 2000000) {
      let n = 0;
      async function visit(p, depth) {
        if (depth > 40 || n > limit) return;
        const pg = await page(p);
        const o = p === 1 ? 100 : 0, t = pg[o];
        const dv = new DataView(pg.buffer, pg.byteOffset, pg.byteLength);
        const cells = dv.getUint16(o + 3);
        if (t === 13 || t === 10) { n += cells; return; }
        if (t === 2) n += cells;
        for (let i = 0; i < cells; i++) await visit(dv.getUint32(dv.getUint16(o + 12 + i * 2)), depth + 1);
        await visit(dv.getUint32(o + 8), depth + 1);
      }
      await visit(root, 0);
      return n;
    }
    const schema = [];
    await walk(1, (rid, v) => { schema.push({ type: v[0], name: v[1], tbl: v[2], root: v[3], sql: v[4] || '' }); });
    return { schema, walk, count, ps, size, whole: !!whole, enc };
  }
  function sqlColumns(sql) {
    const a = sql.indexOf('('), z = sql.lastIndexOf(')');
    if (a < 0 || z < a) return { names: [], alias: -1 };
    const body2 = sql.slice(a + 1, z);
    const defs = [];
    let depth = 0, q = null, cur = '';
    for (const c of body2) {
      if (q) { cur += c; if (c === q) q = null; continue; }
      if (c === '"' || c === "'" || c === '`' || c === '[') { q = c === '[' ? ']' : c; cur += c; continue; }
      if (c === '(') depth++; else if (c === ')') depth--;
      if (c === ',' && depth === 0) { defs.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    if (cur.trim()) defs.push(cur.trim());
    const names = [];
    let alias = -1;
    for (const d of defs) {
      if (/^(constraint|primary\s+key|unique|check|foreign\s+key)\b/i.test(d)) continue;
      const m = /^("(?:[^"]|"")+"|\[[^\]]+\]|`[^`]+`|'[^']+'|\S+)/.exec(d);
      const name = m ? m[1].replace(/^["[`']|["\]`']$/g, '') : d;
      if (/^\S+\s+integer\s+primary\s+key\b(?!\s+desc)/i.test(d)) alias = names.length;
      names.push(name);
    }
    return { names, alias };
  }
  const sqlCell = (v) => (v === null ? 'NULL' : typeof v === 'object' && v.blob !== undefined ? `[blob ${sizeTxt(v.blob)}]` : String(v));
  VIEW.sqlite = async (cx) => {
    const db = await sqliteOpen(cx.src);
    const tables = db.schema.filter((s) => s.type === 'table' && s.root > 0);
    const views = db.schema.filter((s) => s.type === 'view');
    const counts = {};
    if (db.whole) for (const t of tables) counts[t.name] = await db.count(t.root).catch(() => null);
    const tools = bar(cx);
    const b = body(cx, 'kv-scroll');
    const opts = tables.map((t) => [t.name, `${t.name}${counts[t.name] != null ? ` (${num(counts[t.name])})` : ''}`]);
    opts.push(['__schema', 'Schema (all CREATE statements)']);
    async function show(name) {
      b.innerHTML = '';
      if (name === '__schema') { b.appendChild(h('pre', 'kv-text', esc(db.schema.filter((s) => s.sql).map((s) => s.sql + ';').join('\n\n')))); return; }
      const t = tables.find((x) => x.name === name);
      const { names, alias } = sqlColumns(t.sql);
      const rows = [];
      const without = /without\s+rowid/i.test(t.sql);
      await db.walk(t.root, (rid, v) => {
        const r = v.map(sqlCell);
        if (alias >= 0 && !without && (v[alias] === null || v[alias] === undefined)) r[alias] = String(rid);
        rows.push(r);
        return rows.length < 5000;
      });
      b.appendChild(table(rows, { cols: names }));
      if (rows.length >= 5000) b.appendChild(h('div', 'kv-dim pad', 'Showing the first 5,000 rows.'));
    }
    if (!tables.length) { note(b, 'No tables in this database'); } else {
      tools.append(select(opts, tables[0].name, show), spacer());
      await show(tables[0].name);
    }
    tools.appendChild(h('span', 'kv-dim', esc(`${tables.length} tables${views.length ? ` · ${views.length} views` : ''}`)));
    cx.info(`SQLite · ${num(tables.length)} tables · ${sizeTxt(db.size)}${db.whole ? '' : ' · row counts skipped (large file)'}`);
  };

  /* ---------------------------------------------------------------- anything else: what it is, and a hex view of the start */
  const MAGIC = [
    [[0x89, 0x50, 0x4e, 0x47], 'PNG image'], [[0xff, 0xd8, 0xff], 'JPEG image'], [[0x47, 0x49, 0x46, 0x38], 'GIF image'], [[0x25, 0x50, 0x44, 0x46], 'PDF document'],
    [[0x50, 0x4b, 0x03, 0x04], 'ZIP archive (or a format built on ZIP)'], [[0x1f, 0x8b], 'gzip compressed'], [[0x42, 0x5a, 0x68], 'bzip2 compressed'],
    [[0xfd, 0x37, 0x7a, 0x58, 0x5a, 0x00], 'XZ compressed'], [[0x28, 0xb5, 0x2f, 0xfd], 'Zstandard compressed'], [[0x04, 0x22, 0x4d, 0x18], 'LZ4 compressed'],
    [[0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c], '7-Zip archive'], [[0x52, 0x61, 0x72, 0x21], 'RAR archive'], [[0x7f, 0x45, 0x4c, 0x46], 'ELF executable (Linux)'],
    [[0x4d, 0x5a], 'Windows executable or DLL'], [[0xcf, 0xfa, 0xed, 0xfe], 'Mach-O executable (macOS)'], [[0xca, 0xfe, 0xba, 0xbe], 'Mach-O universal binary or Java class'],
    [[0x00, 0x61, 0x73, 0x6d], 'WebAssembly module'], [[0x53, 0x51, 0x4c, 0x69, 0x74, 0x65], 'SQLite database'], [[0xd0, 0xcf, 0x11, 0xe0], 'Microsoft Office 97-2003 document (OLE)'],
    [[0x89, 0x48, 0x44, 0x46], 'HDF5 data (.h5, .h5ad, .hdf5)'], [[0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59], 'NumPy array (.npy)'], [[0x50, 0x41, 0x52, 0x31], 'Parquet table'],
    [[0x41, 0x52, 0x52, 0x4f, 0x57, 0x31], 'Arrow / Feather table'], [[0x1a, 0x45, 0xdf, 0xa3], 'Matroska / WebM video'], [[0x4f, 0x67, 0x67, 0x53], 'Ogg media'],
    [[0x66, 0x4c, 0x61, 0x43], 'FLAC audio'], [[0x49, 0x44, 0x33], 'MP3 audio'], [[0x49, 0x49, 0x2a, 0x00], 'TIFF image'], [[0x4d, 0x4d, 0x00, 0x2a], 'TIFF image'],
    [[0x42, 0x4d], 'BMP image'], [[0x00, 0x00, 0x01, 0x00], 'Windows icon'], [[0x77, 0x4f, 0x46, 0x46], 'WOFF font'], [[0x77, 0x4f, 0x46, 0x32], 'WOFF2 font'],
    [[0x4f, 0x54, 0x54, 0x4f], 'OpenType font'], [[0x00, 0x01, 0x00, 0x00], 'TrueType font'], [[0x43, 0x52, 0x41, 0x4d], 'CRAM alignment file'],
    [[0x42, 0x41, 0x49, 0x01], 'BAM index'], [[0x46, 0x43, 0x53, 0x33], 'Flow cytometry data (FCS 3)'], [[0x46, 0x43, 0x53, 0x32], 'Flow cytometry data (FCS 2)'],
    [[0x80, 0x04], 'Python pickle'], [[0x80, 0x05], 'Python pickle'], [[0x80, 0x02], 'Python pickle'], [[0x64, 0x65, 0x78, 0x0a], 'Android DEX'],
  ];
  function detect(u) {
    if (u.length >= 12 && u[4] === 0x66 && u[5] === 0x74 && u[6] === 0x79 && u[7] === 0x70) return 'MP4 / QuickTime media';
    if (u.length >= 12 && utf8(u.subarray(0, 4)) === 'RIFF') return { WAVE: 'WAV audio', AVI: 'AVI video', WEBP: 'WebP image' }[utf8(u.subarray(8, 12)).trim()] || 'RIFF data';
    for (const [sig, name] of MAGIC) if (sig.every((b, i) => u[i] === b)) return name;
    return null;
  }
  function looksText(u) {
    const s = u.subarray(0, 8192);
    if (s.includes(0)) return false;
    try { new TextDecoder('utf-8', { fatal: true }).decode(s.subarray(0, Math.max(0, s.length - 4))); return true; } catch { return false; }
  }
  VIEW.hex = async (cx) => {
    const n = await cx.src.size();
    const head = await cx.src.range(0, Math.min(n, 64 * 1024));
    const b = body(cx, 'kv-scroll');
    if (looksText(head)) {
      const { text } = await cx.src.text(MB);
      note(b, n > 5e6 ? 'Too big to edit here' : 'Text file', `${sizeTxt(n)}${n > MB ? '. Showing the first 1 MB, read-only.' : ''}`);
      b.appendChild(h('pre', 'kv-text', esc(text)));
      cx.info(`text · ${sizeTxt(n)}${n > MB ? ' · first 1 MB' : ''}`);
      return;
    }
    const type = detect(head);
    note(b, type || 'Binary file', `${sizeTxt(n)}${n > head.length ? `. The first ${sizeTxt(head.length)} is shown.` : ''}`);
    const lines = [];
    const HX = Array.from({ length: 256 }, (_, i) => i.toString(16).padStart(2, '0'));
    for (let o = 0; o < head.length; o += 16) {
      let hx = '', asc = '';
      for (let i = 0; i < 16; i++) {
        const c = head[o + i];
        hx += (o + i < head.length ? HX[c] : '  ') + (i === 7 ? '  ' : ' ');
        asc += o + i >= head.length ? '' : c >= 32 && c < 127 ? String.fromCharCode(c) : '·';
      }
      lines.push(`<span class="pos">${o.toString(16).padStart(8, '0')}</span>  ${hx} <span class="asc">${esc(asc)}</span>`);
    }
    b.appendChild(h('pre', 'kv-hex', lines.join('\n')));
    cx.info(`${type || 'binary'} · ${sizeTxt(n)}`);
  };
  VIEW.text = async (cx) => {                       // a text file inside an archive
    const { text, partial } = await cx.src.text(MB);
    const b = body(cx, 'kv-scroll');
    b.appendChild(h('pre', 'kv-text', esc(text)));
    cx.info(partial ? 'first 1 MB' : `${num(text.split('\n').length)} lines`);
  };

  /* ---------------------------------------------------------------- mounting */
  function mount(host, src, tab, info, viewer, hooks) {
    const root = h('div', 'kv');
    host.appendChild(root);
    const disposers = [];
    let alive = true;
    const cx = {
      root, src, tab,
      later: (fn) => disposers.push(fn),
      info: (s) => { if (alive) info(s); },
      setDirty: (d) => { if (alive) hooks?.setDirty?.(d); },
      setSaver: (fn) => { if (alive) hooks?.setSaver?.(fn); },
    };
    const run = async () => {
      let v = viewer || kind(src.name)?.viewer;
      if (!v || v === 'html' && src.mem) {
        const head = await src.range(0, 8192);
        v = looksText(head) ? 'text' : 'hex';
      }
      try {
        await VIEW[v](cx);
      } catch (e) {
        if (!alive) return;
        console.warn('viewer', v, e);
        disposers.splice(0).forEach((fn) => { try { fn(); } catch { /* ignore */ } });
        root.innerHTML = '';
        const b = body(cx, 'kv-scroll');
        note(b, `Could not show this as ${{ hex: 'hex', text: 'text' }[v] ? 'a file' : v}`, e.message || String(e));
        if (v !== 'hex' && v !== 'text') {
          try {
            const head = await src.range(0, 8192);
            const sub = h('div', 'kv-fallback');
            b.appendChild(sub);
            disposers.push(mount(sub, src, tab, () => {}, looksText(head) ? 'text' : 'hex').dispose);
          } catch { /* nothing more to show */ }
        }
      }
    };
    run();
    return {
      dispose() {
        if (!alive) return;
        alive = false;
        disposers.splice(0).forEach((fn) => { try { fn(); } catch { /* ignore */ } });
        root.remove();
      },
    };
  }
  /* files.js: show a tab's file in its viewer; returns a function that tears the view down */
  function render(host, tab, info, hooks) {
    const v = tab.viewer || 'hex';
    return mount(host, fileSrc(tab.path, tab.kind === 'text' ? tab : null), tab, info, v, hooks).dispose;
  }

  window.KCV = { kind, defaultMode, render };
})();
