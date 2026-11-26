'use strict';
/* Import chats (Settings > Import, or drop an export on the chat list): a ChatGPT or Claude.ai export (.zip, .json, or a
   path on disk for big ones), or Claude Code and Codex sessions on this computer. The server does the work in a background
   job (harness/chat_import.py); this page uploads, shows what is in the file, then the progress and a summary. */
(() => {
  const { S, $, esc, el, api, toast } = window.KCH;
  let box = null;                      // the mounted section
  let pendingFile = null;              // a file dropped on the chat list before the section was open

  const month = (ts) => ts ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', year: 'numeric' }) : '?';
  const day = (ts) => ts ? new Date(ts * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '';
  const mb = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + ' MB' : Math.max(1, Math.round(n / 1e3)) + ' kB';

  function mount(body) {
    box = el('div', 'ci');
    box.innerHTML = `<h3>Import chats</h3>
      <p class="muted">From ChatGPT or Claude.ai (their data export), or Claude Code and Codex sessions on this computer. Imported chats go in the <b>Imported</b> project with their original dates, and you can carry on any of them with any model. Importing the same thing again skips what is already here; a Claude Code session that went on since gets its new messages. Claude Code forks and side sessions sit under the session they came from, each family in its own group.</p>
      <div class="ci-updwrap"></div>
      <div class="ci-drop" tabindex="0"><b>Drop the export here</b><span class="muted">ChatGPT: Settings > Data controls > Export data. Claude.ai: Settings > Privacy > Export data. The .zip as it arrives, its conversations.json, or the unzipped folder.</span>
        <div class="ci-row"><button class="ghost" data-a="pick">Choose file</button><input class="ci-path" placeholder="or the full path of the .zip, .json or folder (best for very big exports)" spellcheck="false"><button class="ghost" data-a="path">Read</button></div>
        <input type="file" accept=".zip,.json,application/zip,application/json" hidden></div>
      <div class="ci-status" hidden></div>
      <div class="ci-row"><span class="muted">On this computer:</span><button class="ghost" data-a="claude-code">Claude Code sessions</button><button class="ghost" data-a="codex">Codex sessions</button></div>
      <div class="ci-local" hidden></div>`;
    body.appendChild(box);
    if (upd.count) mountUpdate(box.querySelector('.ci-updwrap'));
    const drop = box.querySelector('.ci-drop'), file = box.querySelector('input[type=file]');
    box.querySelector('[data-a=pick]').onclick = () => file.click();
    file.onchange = () => { if (file.files[0]) upload(file.files[0]); file.value = ''; };
    box.querySelector('[data-a=path]').onclick = () => { const p = box.querySelector('.ci-path').value.trim(); if (p) scan(p); };
    box.querySelector('.ci-path').onkeydown = (e) => { if (e.key === 'Enter') box.querySelector('[data-a=path]').click(); };
    ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('on'); }));
    ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('on')));
    drop.addEventListener('drop', (e) => { e.preventDefault(); const f = e.dataTransfer.files?.[0]; if (f) upload(f); });
    box.querySelectorAll('[data-a=claude-code],[data-a=codex]').forEach((b) => { b.onclick = () => local(b.dataset.a, b); });
    if (pendingFile) { const f = pendingFile; pendingFile = null; upload(f); }
  }

  function status(html) {
    const s = box.querySelector('.ci-status');
    s.hidden = false;
    s.innerHTML = html;
    return s;
  }
  function bar(frac, text) {
    return `<div class="ci-bar"><i style="width:${Math.max(2, Math.min(100, frac * 100)).toFixed(1)}%"></i></div><div class="muted">${esc(text)}</div>`;
  }

  /* ---- an export: upload (or a path), look inside, import */
  function upload(f) {
    if (!/\.(zip|json)$/i.test(f.name)) return toast('Drop the export .zip or its conversations.json');
    status(bar(0, `Copying ${f.name} (${mb(f.size)})…`));
    const x = new XMLHttpRequest();
    x.open('POST', '/api/chat-import/upload');
    x.setRequestHeader('X-KC-App', '1');
    x.setRequestHeader('X-File-Name', encodeURIComponent(f.name));
    x.setRequestHeader('Content-Type', 'application/octet-stream');
    x.upload.onprogress = (e) => { if (e.lengthComputable) status(bar(e.loaded / e.total, `Copying ${f.name}: ${mb(e.loaded)} of ${mb(e.total)}`)); };
    x.onload = () => {
      let r = {};
      try { r = JSON.parse(x.responseText); } catch { /* not JSON */ }
      if (x.status !== 200) return status(`<div class="ci-err">${esc(r.error || 'Upload failed')}</div>`);
      scan(r.path, f.name);
    };
    x.onerror = () => status('<div class="ci-err">Upload failed</div>');
    x.send(f);
  }

  async function poll(job, onTick) {
    while (job.state === 'running') {
      onTick(job);
      await new Promise((r) => setTimeout(r, 400));
      job = await api('/api/chat-import/job?id=' + job.id);
    }
    return job;
  }

  async function scan(path, name) {
    status(bar(0, 'Reading…'));
    let j;
    try { j = await api('/api/chat-import/scan', { path }); } catch (e) { return status(`<div class="ci-err">${esc(e.message)}</div>`); }
    j = await poll(j, (x) => status(bar(x.total ? x.done / x.total : 0, `${x.phase || 'Reading'}… ${mb(x.done)} of ${mb(x.total)}`)));
    if (j.state !== 'done') return status(`<div class="ci-err">${esc(j.error || 'Stopped')}</div>`);
    const r = j.result, fresh = r.count - r.already;
    const s = status(`<div><b>${r.count.toLocaleString()} chat${r.count === 1 ? '' : 's'} from ${esc(r.label)}</b>, ${esc(month(r.first))} to ${esc(month(r.last))}.` +
      (r.already ? ` ${r.already.toLocaleString()} already imported.` : '') + `</div><div class="muted">${esc(name || path)}</div>` +
      `<div class="ci-row"><button class="primary" data-go ${fresh ? '' : 'disabled'}>${fresh ? `Import ${fresh.toLocaleString()} chat${fresh === 1 ? '' : 's'}` : 'Nothing new to import'}</button></div>`);
    s.querySelector('[data-go]').onclick = () => run({ path });
  }

  async function run(body) {
    let j;
    try { j = await api('/api/chat-import/run', body); } catch (e) { return status(`<div class="ci-err">${esc(e.message)}</div>`); }
    const stop = () => api('/api/chat-import/cancel', { id: j.id }).catch(() => {});
    j = await poll(j, (x) => {
      const r = x.result || {};
      const s = status(bar(x.total ? x.done / x.total : 0, `${x.phase}… ${(r.imported || 0).toLocaleString()} imported` + (r.skipped ? `, ${r.skipped} skipped` : '')) + '<div class="ci-row"><button class="link" data-stop>Stop</button></div>');
      s.querySelector('[data-stop]').onclick = stop;
    });
    const r = j.result || {};
    const s = status((j.state === 'error' ? `<div class="ci-err">${esc(j.error)}</div>` : '') +
      `<div><b>Imported ${(r.imported || 0).toLocaleString()} chat${r.imported === 1 ? '' : 's'}</b> (${(r.messages || 0).toLocaleString()} messages) in ${r.secs ?? j.secs} s` +
      (j.state === 'cancelled' ? ', then stopped' : '') + '.' + (r.updated ? ` ${r.updated.toLocaleString()} updated with new messages.` : '') +
      (r.nested ? ` ${r.nested} nested under the session they came from` + (r.groups ? `, ${r.groups} new group${r.groups === 1 ? '' : 's'}.` : '.') : '') +
      (r.skipped ? ` ${r.skipped.toLocaleString()} skipped (already here or empty).` : '') +
      (r.failed ? ` ${r.failed} could not be read.` : '') + '</div>' +
      (r.errors?.length ? `<details class="muted"><summary>What went wrong</summary><pre>${esc(r.errors.join('\n'))}</pre></details>` : '') +
      (r.project && (r.imported || r.updated) ? '<div class="ci-row"><button class="primary" data-open>Show them</button></div>' : ''));
    if (r.project && r.imported) {           // a first import makes the "Imported" project: list it in the project menu
      try { S.projects = (await api('/api/state')).projects; } catch {}
    }
    s.querySelector('[data-open]')?.addEventListener('click', async () => { $('dialog').close(); await KCH.setProject?.(r.project); });
    if ((r.imported || r.updated || r.nested) && S.project === r.project) KCH.setProject?.(r.project);
    if (body.source) local(body.source);
    updStatus();                           // the update button by "Chats" shows once there are imported sessions
  }

  /* ---- sessions on this computer */
  async function local(source, btn) {
    const pane = box.querySelector('.ci-local');
    box.querySelectorAll('[data-a=claude-code],[data-a=codex]').forEach((b) => b.classList.toggle('on', b.dataset.a === source));
    pane.hidden = false;
    pane.innerHTML = '<div class="muted">Looking for sessions…</div>';
    let r;
    try { r = await api('/api/chat-import/local?source=' + source); } catch (e) { pane.innerHTML = `<div class="ci-err">${esc(e.message)}</div>`; return; }
    const label = source === 'codex' ? 'Codex' : 'Claude Code';
    if (!r.sessions.length) { pane.innerHTML = `<div class="muted">No ${label} sessions found in ${esc(r.roots.join(', '))}.</div>`; return; }
    const sel = new Set();
    const cc = source === 'claude-code';
    const open = (x) => x.state ? x.state !== 'imported' : !x.imported;       // new, or went on since its import
    const fresh = r.sessions.filter((x) => x.state === 'new' || (!x.state && !x.imported)).length;
    const grown = r.sessions.filter((x) => x.state === 'grown').length;
    pane.innerHTML = (cc ? `<div class="ci-row"><button class="primary" data-everything ${fresh || grown ? '' : 'disabled'}>` +
      (fresh || grown ? `Import all: ${[fresh ? fresh + ' new' : '', grown ? grown + ' with new messages' : ''].filter(Boolean).join(', ')}` : 'All imported and up to date') +
      `</button><span class="muted">Keeps forks and side sessions under the session they came from.</span></div>` : '') +
      `<div class="ci-row"><input class="ci-q" type="search" placeholder="Search ${r.sessions.length} ${label} sessions" spellcheck="false">` +
      `<label class="ci-chk"><input type="checkbox" data-all> Select all</label></div><div class="ci-list scroll"></div>` +
      `<div class="ci-row"><button class="${cc ? 'ghost' : 'primary'}" data-imp disabled>Import</button><label class="ci-chk"><input type="checkbox" data-own> Put each in the folder it ran in (adds those projects)</label></div>`;
    const list = pane.querySelector('.ci-list'), q = pane.querySelector('.ci-q'), all = pane.querySelector('[data-all]'), imp = pane.querySelector('[data-imp]');
    const shown = () => {
      const t = q.value.trim().toLowerCase();
      return r.sessions.filter((s) => !t || `${s.title} ${s.first} ${s.project} ${s.family || ''}`.toLowerCase().includes(t));
    };
    const KIND = { fork: 'fork', side: 'side session', handoff: 'handed off' };
    const paint = () => {
      const rows = shown();
      const t = q.value.trim();
      list.innerHTML = '';
      for (const s of rows.slice(0, 600)) {
        const row = el('label', 'ci-item' + (open(s) ? '' : ' done') + (s.depth && !t ? ' sub' : ''));
        if (s.depth && !t) row.style.setProperty('--d', Math.min(s.depth, 4));
        row.innerHTML = `<input type="checkbox" ${sel.has(s.path) ? 'checked' : ''} ${open(s) ? '' : 'disabled'}><div class="txt"><b></b><span></span></div>`;
        row.querySelector('b').textContent = s.title || s.first;
        row.querySelector('span').textContent = [s.kind ? KIND[s.kind] : '', KCH.base(s.project) || '?', day(s.updated), mb(s.size),
          s.state === 'grown' ? 'has new messages' : !open(s) ? 'imported' : '', s.title ? s.first : ''].filter(Boolean).join(' · ');
        row.title = s.project;
        row.querySelector('input').onchange = (e) => { if (e.target.checked) sel.add(s.path); else sel.delete(s.path); count(); };
        list.appendChild(row);
      }
      if (rows.length > 600) list.appendChild(el('div', 'muted', `${rows.length - 600} more: search to narrow it down`));
      if (!rows.length) list.appendChild(el('div', 'muted', 'Nothing matches'));
      const avail = rows.filter(open);
      all.checked = avail.length > 0 && avail.every((s) => sel.has(s.path));
    };
    const count = () => {
      const size = r.sessions.filter((s) => sel.has(s.path)).reduce((a, s) => a + s.size, 0);
      imp.disabled = !sel.size;
      imp.textContent = sel.size ? `Import ${sel.size} session${sel.size === 1 ? '' : 's'} (${mb(size)})` : 'Import';
      const avail = shown().filter(open);
      all.checked = avail.length > 0 && avail.every((s) => sel.has(s.path));
    };
    q.oninput = paint;
    all.onchange = () => { for (const s of shown()) if (open(s)) { if (all.checked) sel.add(s.path); else sel.delete(s.path); } paint(); count(); };
    imp.onclick = () => run({ source, files: [...sel], own_folders: pane.querySelector('[data-own]').checked });
    pane.querySelector('[data-everything]')?.addEventListener('click', () => run({ source, all: true, own_folders: pane.querySelector('[data-own]').checked }));
    paint();
    count();
  }

  /* ---- drop an export on the chat list */
  const side = $('side');
  const isExport = (e) => [...(e.dataTransfer?.items || [])].some((i) => i.kind === 'file') && ![...(e.dataTransfer?.types || [])].includes('text/kch-path');
  ['dragenter', 'dragover'].forEach((t) => side.addEventListener(t, (e) => { if (isExport(e)) { e.preventDefault(); side.classList.add('ci-target'); } }));
  side.addEventListener('dragleave', (e) => { if (!side.contains(e.relatedTarget)) side.classList.remove('ci-target'); });
  side.addEventListener('drop', (e) => {
    side.classList.remove('ci-target');
    const f = e.dataTransfer.files?.[0];
    if (!f) return;
    e.preventDefault();
    if (!/\.(zip|json)$/i.test(f.name)) return toast('Drop a ChatGPT or Claude.ai export (.zip or .json) here to import it');
    pendingFile = f;
    window.KCS?.open('import');
  });

  /* ---- Update imported chats: the button by "Chats", a group's and a chat's menu, Settings > Import. One click brings
     the imported Claude Code and Codex chats up to date with their sessions on this computer (new messages, new forks
     under their parent, new sessions); a line under "Chats" shows the progress, then what changed. */
  const upd = { job: null, seen: null, count: 0, auto: 'off' };
  const line = $('updLine'), ubtn = $('updImportsBtn');
  const X = '<svg viewBox="0 0 10 10"><path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" stroke-width="1.4" fill="none"/></svg>';
  const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : (many || one + 's')}`;
  const HOW = { new: 'new', grown: 'new messages', continued: 'new messages, after what you said here', rebuilt: 'history changed there, rebuilt' };

  function summary(r) {
    const bits = [];
    if (r.updated) bits.push(`${plural(r.updated, 'chat')} updated`);
    if (r.imported) bits.push(`${r.imported.toLocaleString()} new`);
    if (r.skipped) bits.push(`${r.skipped.toLocaleString()} unchanged`);
    if (r.busy) bits.push(`${r.busy} busy (next time)`);
    if (r.failed) bits.push(`${r.failed} could not be read`);
    return bits.join(', ') || 'No imported sessions found on this computer';
  }
  function paintLine(j, open) {
    if (!line) return;
    const r = j.result || {};
    line.hidden = false;
    if (j.state === 'running') {
      const frac = j.total > 1 ? j.done / j.total : 0;
      line.innerHTML = `<div class="row"><span>${esc(j.phase && j.phase !== 'Done' ? j.phase : 'Updating imported chats')}…</span><button class="link" data-stop>Stop</button></div>` +
        `<div class="ci-bar"><i style="width:${Math.max(3, Math.min(100, frac * 100)).toFixed(1)}%"></i></div>`;
      line.querySelector('[data-stop]').onclick = () => api('/api/chat-import/cancel', { id: j.id }).catch(() => {});
      return;
    }
    const changed = r.changed || [];
    const what = r.scope === 'chat' ? 'This chat' : r.scope === 'group' ? 'This group' : 'Imported chats';
    let text = (j.state === 'error' ? `<b class="err">${esc(j.error || 'Update failed')}</b> ` : '') +
      (changed.length || r.busy || r.failed ? esc(summary(r)) + (j.state === 'cancelled' ? ', then stopped' : '') + '.'
        : `${what} ${r.scope === 'all' || !r.scope ? 'are' : 'is'} up to date` + (r.skipped ? ` (${plural(r.skipped, 'chat')} checked).` : '.'));
    line.innerHTML = `<div class="row"><span>${text}</span>` + (changed.length ? `<button class="link" data-show>${open ? 'Hide' : 'Show'}</button>` : '') +
      `<button class="x" data-x title="Close">${X}</button></div>` +
      (r.errors?.length && open ? `<div class="err">${esc(r.errors.slice(0, 5).join('\n'))}</div>` : '');
    if (open && changed.length) {
      const list = el('div', 'list scroll');
      for (const c of changed) {
        const b = el('button', '', '<b></b><i></i>');
        b.querySelector('b').textContent = c.title || 'Imported chat';
        b.querySelector('i').textContent = (HOW[c.how] || c.how) + (c.how !== 'new' && c.added ? ` (+${c.added})` : c.how === 'new' ? ` (${plural(c.added || 0, 'message')})` : '');
        b.title = c.continued ? 'You carried this chat on here: what came later from the session is after your messages' : '';
        b.onclick = () => openChanged(c);
        list.appendChild(b);
      }
      line.appendChild(list);
    }
    line.querySelector('[data-show]')?.addEventListener('click', () => paintLine(j, !open));
    line.querySelector('[data-x]').onclick = () => { line.hidden = true; upd.seen = j.id; try { localStorage.setItem('kch:updSeen', j.id); } catch { /* private */ } };
  }
  async function openChanged(c) {
    if (c.project && S.project && c.project.toLowerCase() !== S.project.toLowerCase()) await KCH.setProject?.(c.project);
    await KCH.openChat(c.id);
  }

  async function follow(j, quiet) {
    upd.job = j;
    ubtn?.classList.add('spin');
    if (!quiet) paintLine(j);
    while (j.state === 'running') {
      await new Promise((r) => setTimeout(r, 500));
      try { j = await api('/api/chat-import/job?id=' + j.id); } catch { break; }
      upd.job = j;
      if (!quiet) paintLine(j);
    }
    ubtn?.classList.remove('spin');
    const r = j.result || {};
    const changed = r.changed || [];
    if (!quiet || changed.length || j.state === 'error') paintLine(j);
    if (changed.length) {
      await KCH.loadChats();
      if (S.chat && changed.some((c) => c.id === S.chat) && !S.busy.has(S.chat)) await KCH.openChat(S.chat);
      if (r.imported) { try { S.projects = (await api('/api/state')).projects; } catch { /* the list still works */ } }
    }
    if (!quiet && !changed.length && !r.busy && !r.failed && j.state === 'done') {
      setTimeout(() => { if (upd.job?.id === j.id && line.querySelector('[data-x]')) line.hidden = true; }, 6000);
    }
    return j;
  }

  /* scope: 'all', or 'group' / 'chat' with its id */
  async function update(scope = 'all', id = null) {
    if (upd.job?.state === 'running') return toast('An update is already running');
    let j;
    try { j = await api('/api/chat-import/update', { scope, id }); } catch (e) { return toast(e.message); }
    return follow(j, false);
  }

  async function updStatus() {
    let st;
    try { st = await api('/api/chat-import/status'); } catch { return; }
    upd.count = st.imported;
    upd.auto = st.auto;
    if (ubtn) ubtn.hidden = !st.imported;
    const j = st.job;
    if (!j || upd.job?.state === 'running') return;
    let seen = upd.seen;
    try { seen = seen || localStorage.getItem('kch:updSeen'); } catch { /* private */ }
    if (j.state === 'running') follow(j, !!j.result?.auto);
    else if (j.id !== seen && j.id !== upd.job?.id && j.result?.auto && j.result.changed?.length) { upd.job = j; paintLine(j); }
  }
  ubtn?.addEventListener('click', () => update('all'));
  setTimeout(updStatus, 1200);
  setInterval(updStatus, 120_000);           // an automatic update (Settings) shows up when it runs

  /* Settings > Import: the same button and the automatic choice */
  function mountUpdate(into) {
    const row = el('div', 'ci-row ci-upd');
    row.innerHTML = `<button class="primary" data-up>Update imported chats</button><label class="ci-chk">Automatically <select data-auto>` +
      '<option value="off">Off</option><option value="5min">Every 5 minutes</option><option value="start">When the app starts</option><option value="hourly">When the app starts, then every hour</option><option value="daily">Once a day</option></select></label>';
    const sel = row.querySelector('[data-auto]');
    sel.value = upd.auto || 'off';
    api('/api/chat-import/status').then((st) => { sel.value = st.auto; }).catch(() => {});
    sel.onchange = async () => {
      try { await api('/api/chat-import/auto', { auto: sel.value }); upd.auto = sel.value; toast(sel.value === 'off' ? 'Imported chats update when you click Update' : 'Imported chats will update by themselves', 1800); } catch (e) { toast(e.message); }
    };
    row.querySelector('[data-up]').onclick = () => { $('dialog').close(); update('all'); };
    into.appendChild(row);
    into.appendChild(el('p', 'muted', 'Pulls in what your Claude Code and Codex sessions on this computer said since they were imported: new messages, new forks and side sessions (under the chat they came from), and new sessions. Your renames and moves are kept. If you carried a chat on here, what came later from the session goes after your messages. Also in the list: the update button next to "Chats", a group\'s menu, and right-click on an imported chat.'));
  }

  window.KCI = { mount, update, status: updStatus };
})();
