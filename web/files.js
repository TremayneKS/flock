'use strict';
/* The right-hand side: the project's files as a tree (drag one into the chat to reference it), an editor with tabs
   (CodeMirror 6, Ctrl+S saves), a preview of what the agent builds, and the chat's and project's notes. */
(() => {
  const { S, $, LS, esc, el, base, api, toast } = window.KCH;
  const F = { root: null, open: new Set(), kids: new Map(), sel: null, tabs: [], cur: null, cm: null, cmLoading: null, timer: 0 };

  const FILEICON = '<svg class="fi" viewBox="0 0 16 16"><path d="M9 2H4.5A1.5 1.5 0 0 0 3 3.5v9A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5V6z"/><path d="M9 2v4h4"/></svg>';
  const DIRICON = '<svg class="fi" viewBox="0 0 16 16"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"/></svg>';
  const TW = '<span class="tw"><svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg></span>';
  const sizeTxt = (n) => n == null ? '' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
  const norm = (p) => String(p || '').replace(/\//g, '\\').toLowerCase();
  const parentOf = (p) => p.replace(/[\\/][^\\/]+$/, '');

  /* ---------------------------------------------------------------- tree */
  async function list(path, force) {
    if (!force && F.kids.has(norm(path))) return F.kids.get(norm(path));
    const r = await api('/api/files/list?path=' + encodeURIComponent(path));
    F.kids.set(norm(path), r.entries);
    return r.entries;
  }

  async function setRoot(path) {
    F.root = path;
    F.kids.clear();
    F.open = new Set(LS.get('open:' + path, []));
    $('treeRoot').textContent = base(path);
    $('treeRoot').title = path;
    await paintTree();
    loadProjectNotes();
    const tabs = LS.get('tabs:' + path, []);
    F.tabs = [];
    F.cur = null;
    paintTabs();
    showEditor(null);
    for (const t of tabs.slice(0, 8)) await openFile(t, true);
    if (F.tabs.length) activate(F.tabs[F.tabs.length - 1].path);
  }

  async function paintTree() {
    const box = $('tree');
    const frag = document.createDocumentFragment();
    async function walk(dir, depth) {
      let entries;
      try { entries = await list(dir); } catch (e) { frag.appendChild(el('div', 'node', `<span class="nm" style="padding-left:${depth * 14 + 20}px">${esc(e.message)}</span>`)); return; }
      for (const e of entries) {
        const n = el('div', 'node' + (e.dir ? ' dir' : '') + (e.heavy ? ' heavy' : '') + (F.open.has(norm(e.path)) ? ' open' : '') + (norm(F.sel) === norm(e.path) ? ' sel' : ''));
        n.style.paddingLeft = depth * 14 + 4 + 'px';
        n.innerHTML = (e.dir ? TW + DIRICON : '<span class="tw"></span>' + FILEICON) + `<span class="nm">${esc(e.name)}</span>` + (e.dir ? '' : `<span class="sz">${sizeTxt(e.size)}</span>`);
        n.dataset.path = e.path;
        n.draggable = true;
        n.addEventListener('dragstart', (ev) => { ev.dataTransfer.setData('text/kch-path', e.path); ev.dataTransfer.setData('text/plain', e.path); });
        n.onclick = () => (e.dir ? toggleDir(e.path) : (select(e.path), openFile(e.path)));
        n.oncontextmenu = (ev) => { ev.preventDefault(); menu(ev, e); };
        frag.appendChild(n);
        if (e.dir && F.open.has(norm(e.path))) await walk(e.path, depth + 1);
      }
      if (!entries.length && depth === 0) frag.appendChild(el('div', 'node', '<span class="nm" style="padding-left:20px;color:var(--faint)">Empty folder</span>'));
    }
    await walk(F.root, 0);
    box.innerHTML = '';
    box.appendChild(frag);
  }

  function select(p) {
    F.sel = p;
    document.querySelectorAll('#tree .node').forEach((n) => n.classList.toggle('sel', norm(n.dataset.path) === norm(p)));
  }

  async function toggleDir(p) {
    const k = norm(p);
    if (F.open.has(k)) F.open.delete(k); else F.open.add(k);
    LS.set('open:' + F.root, [...F.open]);
    select(p);
    await paintTree();
  }

  function refreshSoon() {
    clearTimeout(F.timer);
    F.timer = setTimeout(async () => { F.kids.clear(); await paintTree(); }, 350);
  }

  function menu(ev, e) {
    const items = [];
    const add = (label, fn) => { const b = el('button', 'item', esc(label)); b.onclick = () => { $('popover').hidden = true; fn(); }; items.push(b); };
    if (!e.dir) add('Open', () => openFile(e.path));
    add('Add to chat', () => window.KCH.addChip(e.path));
    if (/\.html?$/i.test(e.name)) add('Preview', () => preview(e.path));
    add('Copy path', () => navigator.clipboard?.writeText(e.path));
    items.push(el('div', 'sep'));
    const dir = e.dir ? e.path : parentOf(e.path);
    add('New file here…', () => newEntry(dir, false));
    add('New folder here…', () => newEntry(dir, true));
    add('Rename…', async () => {
      const name = window.prompt('New name', e.name);
      if (!name || name === e.name) return;
      try { await api('/api/files/rename', { path: e.path, to: parentOf(e.path) + '\\' + name }); refreshSoon(); } catch (err) { toast(err.message); }
    });
    add('Delete', async () => {
      if (!window.confirm(`Move ${e.name} to the harness bin? (its trash folder, so it can be brought back)`)) return;
      try { await api('/api/files/delete', { path: e.path }); closeTab(e.path, true); refreshSoon(); } catch (err) { toast(err.message); }
    });
    const pop = $('popover');
    pop.innerHTML = '';
    items.forEach((i) => pop.appendChild(i));
    pop.hidden = false;
    pop.style.top = Math.min(ev.clientY, window.innerHeight - 320) + 'px';
    pop.style.left = Math.min(ev.clientX, window.innerWidth - 260) + 'px';
    pop.style.right = 'auto'; pop.style.bottom = 'auto';
    setTimeout(() => document.addEventListener('mousedown', function close(x) { if (!pop.contains(x.target)) pop.hidden = true; else document.addEventListener('mousedown', close, { once: true }); }, { once: true }), 0);
  }

  async function newEntry(dir, isDir) {
    const name = window.prompt(isDir ? 'Folder name' : 'File name');
    if (!name) return;
    const p = dir.replace(/[\\/]$/, '') + '\\' + name.replace(/\//g, '\\');
    try {
      await api('/api/files/new', { path: p, dir: isDir });
      F.open.add(norm(dir));
      refreshSoon();
      if (!isDir) setTimeout(() => openFile(p), 400);
    } catch (err) { toast(err.message); }
  }
  $('newFileBtn').onclick = () => newEntry(F.sel && F.kids.has(norm(F.sel)) ? F.sel : F.root, false);
  $('newDirBtn').onclick = () => newEntry(F.sel && F.kids.has(norm(F.sel)) ? F.sel : F.root, true);
  $('refreshTree').onclick = refreshSoon;

  /* ---------------------------------------------------------------- editor */
  const LANG = { js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx', ts: 'typescript', tsx: 'tsx', html: 'html', htm: 'html',
    css: 'css', json: 'json', md: 'markdown', py: 'python', sh: 'shell', bash: 'shell', yml: 'yaml', yaml: 'yaml', toml: 'toml', svg: 'html', xml: 'html',
    ipynb: 'json', geojson: 'json', webmanifest: 'json', jsonc: 'json', har: 'json' };
  const langOf = (p) => LANG[(p.split('.').pop() || '').toLowerCase()] || 'text';

  async function cm() {
    if (F.cm) return F.cm;
    if (!F.cmLoading) F.cmLoading = import('/static/editor/cm.js').then((m) => { F.cm = m; return m; }).catch(() => null);
    return F.cmLoading;
  }

  async function openFile(path, quiet) {
    let t = F.tabs.find((x) => norm(x.path) === norm(path));
    if (!t) {
      t = { path, text: '', mtime: 0, dirty: false, kind: 'text' };
      const k = window.KCV?.kind(path);           // viewers.js: which viewer, and whether the file is text the editor can open
      if (k && !k.text) { t.kind = 'view'; t.viewer = k.viewer; }
      else {
        try {
          const r = await api('/api/files/read?path=' + encodeURIComponent(path));
          if (r.binary || r.too_big) { t.kind = 'view'; t.viewer = r.too_big && k ? k.viewer : 'hex'; t.big = true; }
          else { t.text = r.text; t.mtime = r.mtime; t.crlf = r.crlf; if (k) { t.viewer = k.viewer; t.mode = window.KCV.defaultMode(k.viewer); } }
        } catch (e) { if (!quiet) toast(e.message); return; }
      }
      F.tabs.push(t);
      saveTabs();
    }
    if (!quiet) { window.KCH.showRight(); showPane('files'); activate(path); }
    paintTabs();
  }

  function saveTabs() { LS.set('tabs:' + F.root, F.tabs.map((t) => t.path)); }

  function paintTabs() {
    const box = $('edTabs');
    box.innerHTML = '';
    for (const t of F.tabs) {
      const b = el('div', 'etab' + (t === F.cur ? ' on' : '') + (t.dirty ? ' dirty' : ''), `<span title="${esc(t.path)}">${esc(base(t.path))}</span><button class="x" title="Close"><svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>`);
      b.onclick = (e) => { if (e.target.closest('.x')) { closeTab(t.path); } else activate(t.path); };
      b.onauxclick = (e) => { if (e.button === 1) closeTab(t.path); };
      box.appendChild(b);
    }
  }

  function closeTab(path, force) {
    const i = F.tabs.findIndex((x) => norm(x.path) === norm(path));
    if (i < 0) return;
    const t = F.tabs[i];
    if (t.dirty && !force && !window.confirm(`${base(path)} has unsaved changes. Close anyway?`)) return;
    F.tabs.splice(i, 1);
    saveTabs();
    if (F.cur === t) { F.cur = null; const n = F.tabs[i] || F.tabs[i - 1]; if (n) activate(n.path); else showEditor(null); }
    paintTabs();
  }

  async function activate(path) {
    const t = F.tabs.find((x) => norm(x.path) === norm(path));
    if (!t) return;
    if (F.cur && F.cur.view && F.cur !== t) F.cur.state = F.cur.view.getState?.();
    F.cur = t;
    paintTabs();
    await showEditor(t);
    select(path);
  }

  async function showEditor(t) {
    const host = $('editor');
    F.unview?.(); F.unview = null;
    $('edPreview').hidden = !(t && /\.html?$/i.test(t.path));
    $('edSave').disabled = !(t && t.dirty);
    $('edInfo').textContent = t ? `‎${t.path}${t.kind === 'text' ? ' · ' + langOf(t.path) + (t.crlf ? ' · CRLF' : '') : ''}‎` : '';   // marks: the start is cut, not the end
    const modes = $('edMode');
    modes.hidden = !(t && t.kind === 'text' && t.viewer);
    modes.querySelectorAll('button').forEach((b) => b.classList.toggle('on', !!t && b.dataset.m === (t.mode || 'source')));
    if (!t) { host.innerHTML = '<div class="empty">Open a file from the tree</div>'; F.view = null; return; }
    if (t.kind === 'view' || (t.viewer && t.mode === 'view')) {        // a viewer (viewers.js) instead of the editor
      if (t.view) { t.state = t.view.getState?.(); t.view.destroy?.(); t.view = null; }
      t.viewerSave = null;
      host.innerHTML = '';
      F.view = null;
      const unview = window.KCV.render(
        host, t,
        (s) => { if (F.cur === t) $('edInfo').textContent = `‎${t.path}${s ? ' · ' + s : ''}‎`; },
        {
          setDirty: (d) => { t.dirty = !!d; paintTabs(); if (F.cur === t) $('edSave').disabled = !t.dirty; },
          setSaver: (fn) => { t.viewerSave = fn; },
        }
      );
      F.unview = () => { t.viewerSave = null; unview(); };
      return;
    }
    const m = await cm();
    host.innerHTML = '';
    if (m) {
      t.view = m.createEditor(host, { doc: t.text, language: langOf(t.path), state: t.state,
        onChange: (text) => { t.text = text; if (!t.dirty) { t.dirty = true; paintTabs(); $('edSave').disabled = false; } },
        onSave: () => save(t) });
      F.view = t.view;
    } else {
      const ta = el('textarea', 'plain');
      ta.value = t.text;
      ta.spellcheck = false;
      ta.oninput = () => { t.text = ta.value; if (!t.dirty) { t.dirty = true; paintTabs(); $('edSave').disabled = false; } };
      ta.onkeydown = (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(t); } };
      host.appendChild(ta);
      F.view = null;
    }
  }

  async function save(t) {
    t = t || F.cur;
    if (!t || !t.dirty) return;
    if (t.viewerSave) {
      try {
        t._justSaved = Date.now();
        await t.viewerSave();
        t._justSaved = Date.now();
        t.dirty = false;
        paintTabs(); $('edSave').disabled = true;
        toast('Saved ' + base(t.path), 1200);
      } catch (e) { toast('Save failed: ' + (e.message || e)); }
      return;
    }
    try {
      const r = await api('/api/files/write', { path: t.path, content: t.text, mtime: t.mtime, crlf: t.crlf });
      t.mtime = r.mtime; t.dirty = false;
      paintTabs(); $('edSave').disabled = true;
      toast('Saved ' + base(t.path), 1200);
    } catch (e) {
      if (e.data?.conflict && window.confirm(`${base(t.path)} changed on disk since you opened it (the agent?). Overwrite it with your version?`)) {
        const r = await api('/api/files/write', { path: t.path, content: t.text, force: true, crlf: t.crlf });
        t.mtime = r.mtime; t.dirty = false; paintTabs(); $('edSave').disabled = true;
      } else if (!e.data?.conflict) toast(e.message);
    }
  }
  $('edSave').onclick = () => save();
  $('edMode').querySelectorAll('button').forEach((b) => { b.onclick = () => { const t = F.cur; if (!t || t.mode === b.dataset.m) return; if (t.view) { t.state = t.view.getState?.(); } t.mode = b.dataset.m; showEditor(t); }; });
  $('edRef').onclick = () => { if (F.cur) window.KCH.addChip(F.cur.path); };
  $('edPreview').onclick = () => { if (F.cur) preview(F.cur.path); };
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && F.cur && (document.activeElement?.closest('#editor') || document.activeElement?.closest('#pane-files'))) { e.preventDefault(); save(); }
  });

  /* a file the agent (or anything) changed */
  async function onFileEvent(ev) {
    refreshSoon();
    const t = F.tabs.find((x) => norm(x.path) === norm(ev.path));
    if (t && t.kind === 'view' && F.cur === t) {
      if (t._justSaved && Date.now() - t._justSaved < 2500) return;
      if (t.dirty) { toast(`${base(t.path)} changed on disk; you have unsaved edits`); return; }
      clearTimeout(F.viewTimer);
      F.viewTimer = setTimeout(() => { if (F.cur === t && !t.dirty) showEditor(t); }, 500);
    }
    if (t && t.kind === 'text') {
      if (t.dirty) { toast(`${base(t.path)} changed on disk; you have unsaved edits in the editor`); }
      else {
        try {
          const r = await api('/api/files/read?path=' + encodeURIComponent(t.path));
          if (r.text !== undefined && r.text !== t.text) {
            t.text = r.text; t.mtime = r.mtime;
            if (F.cur === t && t.view?.setValue) t.view.setValue(r.text, true); else t.state = null;
            if (F.cur === t && !t.view) showEditor(t);
          }
        } catch { /* deleted */ }
      }
    }
    if (F.prevPath && norm(parentOf(ev.path)).startsWith(norm(parentOf(F.prevPath)))) {
      clearTimeout(F.prevTimer);
      F.prevTimer = setTimeout(() => { try { $('prevFrame').contentWindow.location.reload(); } catch { $('prevFrame').src = $('prevFrame').src; } }, 600);
    }
    setTimeout(() => {
      const n = [...document.querySelectorAll('#tree .node')].find((x) => norm(x.dataset.path) === norm(ev.path));
      if (n) { n.classList.remove('flash'); void n.offsetWidth; n.classList.add('flash'); }
    }, 450);
  }

  /* ---------------------------------------------------------------- preview */
  async function preview(target) {
    window.KCH.showRight();
    showPane('preview');
    let url = target;
    if (!/^https?:\/\//i.test(target)) {
      const r = await api('/api/preview-url?path=' + encodeURIComponent(target));
      url = r.url;
      F.prevPath = target;
    } else F.prevPath = null;
    $('prevUrl').value = F.prevPath || url;
    $('prevFrame').src = url;
  }
  $('prevGo').onclick = () => { const v = $('prevUrl').value.trim(); if (v) preview(/^[a-z]:|^\\|^\//i.test(v) || /^https?:/i.test(v) ? v : F.root.replace(/[\\/]$/, '') + '\\' + v); };
  $('prevUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('prevGo').click(); });
  $('prevReload').onclick = () => { const f = $('prevFrame'); if (f.src) f.src = f.src; };

  /* ---------------------------------------------------------------- tabs of the panel */
  function showPane(name) {
    document.querySelectorAll('#rightTabs [data-tab]').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
    for (const p of ['files', 'preview', 'notes']) $('pane-' + p).hidden = p !== name;
  }
  document.querySelectorAll('#rightTabs [data-tab]').forEach((b) => { b.onclick = () => showPane(b.dataset.tab); });

  /* ---------------------------------------------------------------- notes */
  let notesDirty = false;
  $('chatNotes').addEventListener('input', () => { notesDirty = true; $('chatNotesState').textContent = 'Not saved'; });
  function setChatNotes(text) {
    if (notesDirty && document.activeElement === $('chatNotes')) return;
    $('chatNotes').value = text || '';
    notesDirty = false;
    $('chatNotesState').textContent = text ? `${text.length.toLocaleString()} characters` : 'Empty so far';
  }
  function setChatLog(text) { $('chatLog').textContent = text || 'Nothing yet.'; $('chatLog').scrollTop = 1e9; }
  $('saveChatNotes').onclick = async () => {
    if (!S.chat) return;
    await api(`/api/chats/${S.chat}/notes`, { text: $('chatNotes').value });
    notesDirty = false;
    $('chatNotesState').textContent = 'Saved';
  };
  async function loadProjectNotes() {
    const r = await api('/api/project-notes?project=' + encodeURIComponent(F.root)).catch(() => ({ text: '' }));
    $('projNotes').value = r.text || '';
    $('projNotesState').textContent = r.text ? 'Read at the start of each chat' : 'Empty';
  }
  $('saveProjNotes').onclick = async () => {
    await api('/api/project-notes', { project: F.root, text: $('projNotes').value });
    $('projNotesState').textContent = 'Saved: new chats read it';
  };

  window.KCF = { setRoot, onFileEvent, refreshSoon, setChatNotes, setChatLog, openFile, preview, showPane };
})();
