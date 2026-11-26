'use strict';
/* Settings: model connections (presets for subscriptions, API keys and local models, or any OpenAI-compatible endpoint),
   the model-server launcher when the profile has one, tools and MCP servers, importing skills and MCP servers from
   Claude Code and Codex, and updates (Flock itself and its tools). */
(() => {
  const { S, $, esc, el, api, toast } = window.KCH;
  const dlg = $('dialog');
  const KIND = { anthropic: 'Anthropic API', openai: 'OpenAI-compatible', 'opencode-go': 'OpenCode Go', 'claude-code': 'Claude Code CLI', codex: 'Codex CLI', agy: 'Antigravity CLI' };

  function pages() {
    const p = [['connections', 'Connections']];
    if (S.cfg?.features?.runtime) p.push(['runtime', S.runtime?.label || 'Model server']);
    return p.concat([['tools', 'Tools'], ['import', 'Import'], ['updates', 'Updates'], ['general', 'General']]);
  }

  function open(page = 'connections', adding = false) {
    dlg.innerHTML = `<div class="dlg"><nav><h2>Settings</h2>${pages().map(([k, l]) => `<button data-p="${k}">${esc(l)}</button>`).join('')}<div class="spacer"></div><button data-close>Close</button></nav><div class="body" id="setBody"></div></div>`;
    dlg.querySelectorAll('nav [data-p]').forEach((b) => { b.onclick = () => show(b.dataset.p); });
    dlg.querySelector('[data-close]').onclick = () => dlg.close();
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); }, { once: true });
    dlg.showModal();
    markUpdate(lastApp);
    show(page);
    if (adding) editEp($('setBody'), null);
  }
  $('setBtn').onclick = () => open();
  document.addEventListener('keydown', (e) => {          // Ctrl+, opens Settings, as in most desktop apps
    if ((e.ctrlKey || e.metaKey) && e.key === ',' && !dlg.open) { e.preventDefault(); open(); }
  });

  function show(p) {
    dlg.querySelectorAll('nav [data-p]').forEach((b) => b.classList.toggle('on', b.dataset.p === p));
    const body = $('setBody');
    body.innerHTML = '';
    ({ connections, runtime, tools, import: imports, updates, general })[p](body);
  }

  async function refreshCfg() {
    const st = await api('/api/state');
    S.cfg = st.config; S.runtime = st.runtime;
    window.KCH.applyFeatures?.();
  }
  async function after(r) { S.cfg = r.config; await refreshCfg(); window.KCH.repaint?.(); }

  /* ---- connections */
  function connections(body) {
    body.appendChild(el('h3', '', 'Model connections'));
    body.appendChild(el('p', 'muted', 'Your Claude or ChatGPT plan, an API key, or a model on your own machine. Keys are saved in their own files (never in the settings) and are never shown again.'));
    if (!S.cfg.endpoints.length) body.appendChild(el('p', '', 'Nothing is connected yet.'));
    for (const ep of S.cfg.endpoints) {
      const c = el('div', 'card');
      const shown = ep.show_in_picker !== false;
      const tags = [KIND[ep.kind] || ep.kind, ep.id === S.cfg.endpoint ? 'default' : '', !shown ? 'hidden from picker' : '',
        ep.from_profile ? (ep.detected ? 'found on this computer' : 'profile') : '',
        ep.kind in { openai: 1, anthropic: 1, 'opencode-go': 1 } && !ep.has_key && !/127\.0\.0\.1|localhost/.test(ep.base || '') ? 'needs a key' : ''].filter(Boolean);
      c.innerHTML = `<div class="top"><span class="dot"></span><b>${esc(ep.name)}</b>${tags.map((t) => `<span class="tag">${esc(t)}</span>`).join('')}<span class="spacer"></span>` +
        `<button class="link" data-a="pick" title="Show or hide in the model picker">${shown ? 'Hide' : 'Show'}</button>` +
        `<button class="link" data-a="test">Test</button><button class="link" data-a="edit">Edit</button><button class="link danger-link" data-a="del">Remove</button></div>` +
        `<div class="sub">${esc(ep.base || '')} ${ep.model ? '· model ' + esc(ep.model) : ep.kind === 'claude-code' || ep.kind === 'codex' ? '· the CLI\'s default model' : ''} · window ${KCH.fmtK(ep.context)}${ep.runtime ? ' · started by the harness (' + esc(ep.runtime) + ')' : ''}${ep.vision ? ' · sees images' : ''}${ep.effort_default ? ' · effort ' + esc((ep.levels || []).find((x) => x.id === ep.effort_default)?.label || ep.effort_default) : ''}${ep.key_source ? ' · key from ' + esc(ep.key_source) : ''}</div>`;
      c.querySelector('[data-a="pick"]').onclick = async () => {
        try {
          await after(await api('/api/endpoints', { endpoint: { id: ep.id, show_in_picker: !shown } }));
          show('connections');
          toast(!shown ? `${ep.name} shown in picker` : `${ep.name} hidden from picker (connection kept)`);
        } catch (err) { toast(err.message); }
      };
      c.querySelector('[data-a="test"]').onclick = async () => {
        const d = c.querySelector('.dot');
        d.className = 'dot wait';
        const r = await api('/api/endpoints/health?id=' + encodeURIComponent(ep.id)).catch((e) => ({ ok: false, error: e.message }));
        d.className = 'dot ' + (r.ok ? 'ok' : 'bad');
        toast(r.ok ? `${ep.name} answers${r.n_ctx ? ` (server window ${r.n_ctx.toLocaleString()} tokens)` : ''}` : `${ep.name}: ${r.error || 'no answer'}`);
      };
      c.querySelector('[data-a="edit"]').onclick = () => editEp(body, ep);
      c.querySelector('[data-a="del"]').onclick = async () => {
        if (!window.confirm(`Remove ${ep.name}?${ep.from_profile ? ' (It comes from profile.json or was found on this computer: it is hidden, not deleted.)' : ' Its saved key is deleted too.'}`)) return;
        await after(await api('/api/endpoints/delete', { id: ep.id })); show('connections');
      };
      body.appendChild(c);
    }
    const add = el('button', 'primary', 'Add a connection');
    add.onclick = () => editEp(body, null);
    body.appendChild(add);
  }

  async function editEp(body, ep) {
    const presets = ep ? [] : (await api('/api/presets')).presets;
    const blank = { name: '', kind: 'openai', base: 'http://127.0.0.1:8080/v1', model: '', context: 128000, max_output: 16384 };
    let e = ep ? { ...ep } : { ...blank };
    body.innerHTML = `<h3>${ep ? 'Edit ' + esc(ep.name) : 'New connection'}</h3>`;
    const f = el('div', 'form');
    const groups = {};
    for (const p of presets) (groups[p.group] = groups[p.group] || []).push(p);
    f.innerHTML = (ep ? '' : `<label>Start from</label><select id="epPreset"><option value="">Choose…</option>${Object.entries(groups).map(([g, ps]) => `<optgroup label="${esc(g)}">${ps.map((p) => `<option value="${esc(p.preset)}">${esc(p.name)}${p.env_key_present ? ' (env key detected)' : ''}</option>`).join('')}</optgroup>`).join('')}</select><div></div><div class="muted" id="epHelp"></div>`) +
      `<label>Name</label><input id="epName">` +
      `<label>Type</label><select id="epKind">${Object.entries(KIND).map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join('')}</select>` +
      `<label class="api">Base URL</label><input class="api" id="epBase" placeholder="http://127.0.0.1:8080/v1">` +
      `<label>Model name</label><input id="epModel" placeholder="what the server calls it">` +
      `<label class="api">API key</label><input class="api" id="epKey" type="password" autocomplete="off">` +
      `<label>Context window</label><input id="epCtx" type="number">` +
      `<label class="cli">Default effort</label><div class="cli"><select id="epEffort"></select><div class="muted" id="epEffortHint">New chats start here; the top bar changes it for one chat, from its next message.</div></div>` +
      `<label class="api">Max output</label><input class="api" id="epOut" type="number">` +
      `<label class="api">Options</label><div class="chk api"><input type="checkbox" id="epLlama"> llama.cpp server (prompt cache, thinking switch)</div>` +
      `<label>Model picker</label><div class="chk"><input type="checkbox" id="epPicker"> show in the model picker</div>`;
    body.appendChild(f);
    const fill = () => {
      $('epName').value = e.name || ''; $('epKind').value = e.kind || 'openai'; $('epBase').value = e.base || '';
      $('epModel').value = e.model || ''; $('epCtx').value = e.context || 128000; $('epOut').value = e.max_output || 16384;
      $('epLlama').checked = !!e.llama;
      $('epPicker').checked = e.show_in_picker !== false;
      $('epKey').placeholder = e.has_key ? `saved (${e.key_source || 'set'}); leave empty to keep` : e.env_key_present ? `detected in ${e.key_env} (or paste to override)` : e.key_env ? `paste a key, or set ${e.key_env}` : 'optional for local servers';
      const cli = e.kind === 'claude-code' || e.kind === 'codex' || e.kind === 'agy';
      f.querySelectorAll('.api').forEach((x) => { x.hidden = cli; });
      f.querySelectorAll('.cli').forEach((x) => { x.hidden = !cli; });
      const lv = (S.cfg.effort_levels || {})[e.kind] || [];
      $('epEffort').innerHTML = lv.map((x) => `<option value="${esc(x.id)}">${esc(x.label)}: ${esc(x.hint)}</option>`).join('');
      if (lv.length) $('epEffort').value = lv.some((x) => x.id === e.effort) ? e.effort : (e.effort_default || lv[0].id);
    };
    fill();
    $('epKind').onchange = () => { e.kind = $('epKind').value; fill(); };
    $('epPreset')?.addEventListener('change', () => {
      const p = presets.find((x) => x.preset === $('epPreset').value);
      e = p ? { ...blank, ...p } : { ...blank };
      $('epHelp').textContent = p?.help || '';
      fill();
    });
    const row = el('div', '', '');
    row.style.display = 'flex'; row.style.gap = '8px';
    const save = el('button', 'primary', 'Save');
    const cancel = el('button', 'ghost', 'Cancel');
    row.append(save, cancel);
    body.appendChild(row);
    cancel.onclick = () => show('connections');
    save.onclick = async () => {
      const out = { id: ep?.id, preset: ep ? undefined : ($('epPreset').value || undefined), name: $('epName').value.trim() || 'Model',
        kind: $('epKind').value, model: $('epModel').value.trim(), context: +$('epCtx').value || 128000,
        show_in_picker: $('epPicker').checked };
      if (!['claude-code', 'codex', 'agy'].includes(out.kind)) {
        Object.assign(out, { base: $('epBase').value.trim(), max_output: +$('epOut').value || 16384, llama: $('epLlama').checked });
      } else if ($('epEffort').value) out.effort = $('epEffort').value;
      if ($('epKey').value) out.key = $('epKey').value;
      try { await after(await api('/api/endpoints', { endpoint: out })); show('connections'); toast('Saved'); } catch (err) { toast(err.message); }
    };
  }

  /* ---- the model-server launcher (profile.json "runtime") */
  const RUNNING = ['ready', 'starting', 'warming'];
  async function runtime(body) {
    await refreshCfg();
    const r = S.runtime || {};
    body.innerHTML = `<h3>${esc(r.label || 'Model server')}</h3><p class="muted">Started by the harness for ${esc(S.cfg.endpoints.find((e) => e.id === r.endpoint)?.name || r.endpoint)}, from the launcher in profile.json. It keeps running when the harness closes; stop it here.</p>`;
    const choices = Object.entries(r.choices || {});
    if (choices.length) {
      const grid = el('div', 'gpus');
      grid.style.display = 'grid'; grid.style.gridTemplateColumns = `repeat(${Math.min(3, choices.length)},1fr)`; grid.style.gap = '8px';
      for (const [id, g] of choices) {
        const on = r.choice === id && RUNNING.includes(r.state);
        const b = el('button', 'gpu' + (on ? ' on' : ''), `<b>${esc(g.label || id)}</b>${g.speed ? `<span>${esc(g.speed)}</span>` : ''}${g.rate ? `<span>${g.rate} ${esc(r.unit || '')}/hour</span>` : ''}${g.note ? `<span>${esc(g.note)}</span>` : ''}`);
        b.onclick = async () => {
          try {
            if (RUNNING.includes(r.state) && r.choice !== id) { toast(`${r.choice_label} keeps running. Press Stop first, then pick ${g.label || id}.`, 6000); return; }
            if (RUNNING.includes(r.state)) return;
            await api('/api/runtime/start', { choice: id });
            toast('Starting ' + (g.label || id));
            setTimeout(() => show('runtime'), 800);
          } catch (e) { toast(e.message); }
        };
        grid.appendChild(b);
      }
      body.appendChild(grid);
    }
    const st = el('div', 'card');
    st.innerHTML = `<div class="top"><span class="dot ${r.state === 'ready' || r.state === 'external' ? 'ok' : ['starting', 'warming', 'stopping'].includes(r.state) ? 'wait' : r.state === 'failed' ? 'bad' : ''}"></span><b>${esc(r.state)}</b><span class="sub">${esc(r.detail || '')}</span><span class="spacer"></span>${RUNNING.includes(r.state) ? '<button class="danger" id="rtStop">Stop</button>' : (!choices.length && r.state !== 'external' ? '<button class="primary" id="rtStart">Start</button>' : '')}</div>` +
      (['ready', 'warming'].includes(r.state) ? `<div class="sub">${esc(r.choice_label || r.label)} up ${Math.round(r.up_min)} min${r.rate ? `, about ${r.cost} ${esc(r.unit || '')}` : ''}</div>` : '') +
      (r.log ? `<div class="sub">Log: ${esc(r.log)}</div>` : '') + (r.tail?.length ? `<pre style="margin:0;font:11px/1.5 var(--mono);color:var(--soft);white-space:pre-wrap;max-height:12rem;overflow:auto">${esc(r.tail.join('\n'))}</pre>` : '');
    body.appendChild(st);
    st.querySelector('#rtStop')?.addEventListener('click', async () => { await api('/api/runtime/stop', {}); toast('Stopping'); setTimeout(() => show('runtime'), 800); });
    st.querySelector('#rtStart')?.addEventListener('click', async () => { try { await api('/api/runtime/start', {}); toast('Starting'); } catch (e) { toast(e.message); } setTimeout(() => show('runtime'), 800); });
    const keys = Object.keys(r.settings || {});
    if (keys.length) {
      const f = el('div', 'form');
      f.innerHTML = keys.map((k) => `<label>${esc(r.setting_labels?.[k] || k)}</label><input data-k="${esc(k)}" value="${esc(String(r.settings[k]))}">`).join('');
      body.appendChild(f);
      const sv = el('button', 'ghost', 'Save (used at the next start)');
      sv.onclick = async () => {
        const settings = {};
        f.querySelectorAll('[data-k]').forEach((i) => { const old = r.settings[i.dataset.k]; settings[i.dataset.k] = typeof old === 'number' ? +i.value : i.value; });
        await api('/api/config', { runtime: { settings } });
        toast('Saved');
      };
      body.appendChild(sv);
    }
  }

  /* ---- tools */
  async function tools(body) {
    await refreshCfg();
    body.innerHTML = '<h3>Tools</h3><p class="muted">Built in: Read, Write, Edit, Bash, Glob, Grep, TodoWrite, Notes, Skill. These come from MCP servers:</p>';
    for (const [name, m] of Object.entries(S.cfg.mcp)) {
      const row = el('div', 'imp');
      const label = { web: 'Web search and fetch (web, news, papers, clean page text)', browser: 'Browser (Playwright, headless, its own profile)' }[name] || name;
      row.innerHTML = `<button class="switch ${m.enabled && m.installed ? 'on' : ''}" ${m.installed ? '' : 'disabled'}></button><div class="txt"><b>${esc(label)}</b><span>${m.installed ? esc(m.command) : 'not installed: run python install.py'}</span></div>`;
      row.querySelector('.switch').onclick = async (e) => {
        const on = !e.currentTarget.classList.contains('on');
        e.currentTarget.classList.toggle('on', on);
        await api('/api/config', { mcp_enabled: { [name]: on } });
      };
      body.appendChild(row);
    }
    if ((S.cfg.imported_mcp || []).length) {
      body.appendChild(el('p', 'muted', 'Imported MCP servers (their tools show up as mcp__name__tool):'));
      for (const m of S.cfg.imported_mcp) body.appendChild(el('div', 'imp', `<div class="txt"><b>${esc(m.name)}</b><span>from ${esc(m.source)}</span></div>`));
    }
    const pf = el('div', 'imp', `<button class="switch ${S.cfg.project_files ? 'on' : ''}"></button><div class="txt"><b>Read AGENTS.md from the project folder</b><span>Off by default: the model gets the harness's short prompt, the project notes and the chat's notes, and nothing else.</span></div>`);
    pf.querySelector('.switch').onclick = async (e) => { const on = !e.currentTarget.classList.contains('on'); e.currentTarget.classList.toggle('on', on); await api('/api/config', { project_files: on }); };
    body.appendChild(pf);
    const sa = S.cfg.subagents || {};      // the Agent tool (harness/subagents.py)
    for (const [k, title, sub] of [
      ['enabled', 'Subagents (the Agent tool)', `Any model can hand a task to a subagent on any connected model, and Claude and ChatGPT get it as harness_agent. New chats only; nested ${sa.max_depth ?? 2} deep at most; each runs until it finishes or you press Stop.`],
      ['start_servers', 'Subagents may start the model server', 'Off: a subagent on a connection whose server is off (a cloud GPU) says so instead of starting it, since that can cost money.']]) {
      const row = el('div', 'imp', `<button class="switch ${sa[k] ? 'on' : ''}"></button><div class="txt"><b>${esc(title)}</b><span>${esc(sub)}</span></div>`);
      row.querySelector('.switch').onclick = async (e) => {
        const on = !e.currentTarget.classList.contains('on');
        e.currentTarget.classList.toggle('on', on);
        const r = await api('/api/config', { subagents: { [k]: on } });
        S.cfg = r.config;
      };
      body.appendChild(row);
    }
    const subWrap = el('div', 'sub-presets');
    function paintSubPresets() {
      subWrap.innerHTML = '';
      const list = S.cfg?.subagents?.presets || [];
      const eps = S.cfg?.endpoints || [];
      subWrap.appendChild(el('h4', 'sub-pre-h', 'Named subagents'));
      subWrap.appendChild(el('p', 'muted', 'Give any model in your model selector a name (and optional role). Chats can call your named subagents by name or spawn any connected model directly.'));
      for (const p of list) {
        const c = el('div', 'card sub-pre-card');
        c.innerHTML = `<div class="top"><b>${esc(p.name)}</b><span class="tag">${esc(p.model_name || p.model)}</span><span class="spacer"></span><button class="link danger-link" data-rm>Remove</button></div>` +
          (p.instructions ? `<div class="sub">${esc(p.instructions)}</div>` : '');
        c.querySelector('[data-rm]').onclick = async () => {
          try {
            const r = await api('/api/subagents/presets/delete', { id: p.id });
            S.cfg = r.config;
            paintSubPresets();
            toast(`Removed ${p.name}`);
          } catch (err) { toast(err.message); }
        };
        subWrap.appendChild(c);
      }
      const form = el('div', 'card sub-pre-form');
      form.innerHTML = '<div class="sub-pre-row">' +
        '<input type="text" data-name placeholder="Subagent name (e.g. Reviewer)" maxlength="60">' +
        `<select data-model>${eps.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join('')}</select>` +
        '</div><div class="sub-pre-row">' +
        '<input type="text" data-inst placeholder="Optional role or instructions (leave blank to use default)" maxlength="500">' +
        '<button class="primary" data-add>Add</button></div>';
      form.querySelector('[data-add]').onclick = async () => {
        const name = form.querySelector('[data-name]').value.trim();
        const model = form.querySelector('[data-model]').value;
        const instructions = form.querySelector('[data-inst]').value.trim();
        if (!name) return toast('Enter a name for this subagent');
        if (!model) return toast('Select a model');
        try {
          const r = await api('/api/subagents/presets', { name, model, instructions });
          S.cfg = r.config;
          paintSubPresets();
          toast(`Added ${r.preset.name} (${r.preset.model_name})`);
        } catch (err) { toast(err.message); }
      };
      subWrap.appendChild(form);
    }
    paintSubPresets();
    body.appendChild(subWrap);
    await window.KCC?.settings(body);      // computer use (web/computer.js)
  }

  /* ---- import from Claude Code and Codex */
  async function imports(body) {
    body.innerHTML = '';
    window.KCI?.mount(body);                 // chats from ChatGPT, Claude.ai, Claude Code and Codex (chat-import.js)
    body.insertAdjacentHTML('beforeend', '<h3>Skills and MCP servers from Claude Code and Codex</h3><p class="muted">Skills are linked to their folders (updates there show up here). MCP servers are read from their own config each time, so keys in them are never copied.</p><div class="muted" id="impScan">Scanning…</div>');
    const r = await api('/api/imports');
    $('impScan')?.remove();
    const groups = {};
    for (const it of r.items) (groups[it.where] = groups[it.where] || []).push(it);
    for (const [where, items] of Object.entries(groups)) {
      body.appendChild(el('div', 'section-title', `<h2>${esc(where)}</h2>`));
      for (const it of items) {
        const row = el('div', 'imp');
        row.innerHTML = `<button class="switch ${it.imported ? 'on' : ''}" ${it.importable === false ? 'disabled' : ''}></button><div class="txt"><b>${esc(it.name)}</b><span>${esc(it.description || it.path || '')}</span></div>`;
        row.querySelector('.switch').onclick = async (e) => {
          const on = !e.currentTarget.classList.contains('on');
          e.currentTarget.classList.toggle('on', on);
          await api('/api/imports', { selected: [{ kind: it.kind, source: it.source, name: it.name, path: it.path, on }] });
          await refreshCfg();
          toast(on ? `${it.name} imported (new chats see it)` : `${it.name} removed`);
        };
        body.appendChild(row);
      }
    }
    if (!r.items.length) body.appendChild(el('p', 'muted', 'Nothing found to import.'));
  }

  /* ---- updates: Flock itself (selfupdate.py), then the tools it uses (updates.py) */
  const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'never');
  let lastApp = null;

  function markUpdate(r) {   // the dot on the Settings button and on Updates: only for a definite newer version
    lastApp = r;
    const on = !!(r && r.available && !r.unknown);
    $('setBtn').classList.toggle('has-update', on);
    $('setBtn').title = on ? `Settings (${r.message})` : 'Settings';
    dlg.querySelector('nav [data-p="updates"]')?.classList.toggle('has-update', on);
  }
  async function pollUpdate() { try { markUpdate(await api('/api/updates/app')); } catch { /* restarting */ } }
  setTimeout(pollUpdate, 5000);
  setInterval(pollUpdate, 30 * 60 * 1000);

  async function updates(body, force) {
    body.innerHTML = '<h3>Updates</h3>';
    const app = el('div', 'card upd');
    body.appendChild(app);
    appCard(app, force);
    body.appendChild(el('h3', '', 'Tools'));
    const dep = el('div', 'upddeps');
    body.appendChild(dep);
    deps(dep, force);
  }

  async function appCard(c, force, result) {
    if (!result) c.innerHTML = '<div class="muted">Checking…</div>';
    let r;
    try { r = await api('/api/updates/app' + (force ? '?force=1' : '')); } catch (e) { c.innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
    markUpdate(r);
    const cur = r.current;
    const ver = `v${cur.version}${cur.commit ? ' · ' + cur.commit.slice(0, 7) : ''} · ${cur.kind === 'git' ? 'git checkout' + (cur.branch ? ' on ' + cur.branch : '') : 'zip install'}`;
    const dot = r.available ? 'wait' : r.state === 'ok' ? 'ok' : ['offline', 'error', 'rate-limited'].includes(r.state) ? 'bad' : '';
    let h = `<div class="top"><span class="dot ${dot}"></span><b>Flock</b><span class="tag">${esc(ver)}</span><span class="spacer"></span>` +
      (r.repo ? `<button class="link" data-a="check">${r.checking ? 'Checking…' : 'Check now'}</button>` : '') + '</div>' +
      `<div class="updmsg${r.available ? ' avail' : ''}">${esc(r.message)}</div>`;
    if (r.available) {
      const rel = r.latest;
      if (rel && (cur.kind !== 'git' || (r.message || '').includes('v' + rel.version))) {   // a newer release: its notes
        h += `<div class="sub">${esc(rel.name)}${rel.published ? ' · ' + esc(new Date(rel.published).toLocaleDateString()) : ''}${rel.url ? ` · <a href="${esc(rel.url)}" target="_blank" rel="noopener">on GitHub</a>` : ''}</div>`;
        if (rel.notes) h += `<div class="notes md">${window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parse(rel.notes)) : esc(rel.notes)}</div>`;
      }
      if (r.commits?.length) {
        h += `<div class="sub">New commits${r.repo ? ` (<a href="${esc(r.repo)}/commits" target="_blank" rel="noopener">all commits</a>)` : ''}:</div><ul class="commits">` +
          r.commits.map((x) => `<li><code>${esc(x.sha.slice(0, 7))}</code> ${esc(x.title)}</li>`).join('') + '</ul>';
      }
      if (r.blocked) h += `<pre class="updout">${esc(r.blocked)}</pre>`;
    }
    if (result) h += `<pre class="updout">${esc(result.out || '')}</pre>`;
    h += '<div class="acts"></div>';
    h += `<div class="imp"><button class="switch ${r.auto ? 'on' : ''}" data-a="auto" ${r.repo ? '' : 'disabled'}></button><div class="txt"><b>Check automatically</b><span>On start, then every 12 hours, in the background. It only asks GitHub what is newest; nothing is installed until you click Update.</span></div></div>`;
    h += `<p class="muted">${r.repo ? `From <a href="${esc(r.repo)}" target="_blank" rel="noopener">${esc(r.repo.replace(/^https:\/\//, ''))}</a>. Last checked ${esc(when(r.checked_at))}.` : 'Set "updates": {"repo": "https://github.com/owner/flock"} in profile.json to check one.'} ` +
      (cur.kind === 'git' ? 'This copy is a git checkout: Update is a fast-forward git pull, and it never overwrites local changes.' :
        'Update downloads the release, checks it, and keeps the replaced files so Roll back can undo it.') +
      ' Your chats, keys, settings and profile are never touched.</p>';
    c.innerHTML = h;
    const acts = c.querySelector('.acts');
    const busy = (b, txt) => { c.querySelectorAll('button').forEach((x) => { x.disabled = true; }); b.textContent = txt; };
    if (r.available && r.can_update) {
      const b = el('button', 'primary', cur.kind === 'git' ? 'Update (git pull)' : 'Download and install');
      b.onclick = async () => {
        busy(b, 'Updating…');
        const x = await api('/api/updates/apply', { id: 'harness' }).catch((e) => ({ ok: false, out: e.message }));
        toast(x.ok ? (x.restart ? 'Updated: restart to use it' : 'Up to date') : 'Not updated');
        appCard(c, false, x);
      };
      acts.appendChild(b);
    }
    if (r.restart_needed || result?.restart) {
      const b = el('button', r.available ? 'ghost' : 'primary', 'Restart now');
      b.onclick = () => restartApp(b);
      acts.appendChild(b);
    }
    if (r.backup) {
      const b = el('button', 'ghost', r.backup.kind === 'git' ? `Undo the last update (back to ${r.backup.commit})` : `Roll back to v${r.backup.version}`);
      b.onclick = async () => {
        if (!window.confirm(`${b.textContent}? You can update again later.`)) return;
        busy(b, 'Rolling back…');
        const x = await api('/api/updates/rollback', {}).catch((e) => ({ ok: false, out: e.message }));
        appCard(c, false, x);
      };
      acts.appendChild(b);
    }
    c.querySelector('[data-a="check"]')?.addEventListener('click', () => appCard(c, true));
    c.querySelector('[data-a="auto"]').onclick = async (e) => {
      const on = !e.currentTarget.classList.contains('on');
      e.currentTarget.classList.toggle('on', on);
      await api('/api/config', { updates_auto: on });
      toast(on ? 'Automatic update checks on' : 'Automatic update checks off');
    };
  }

  async function restartApp(b) {
    const before = (await api('/api/state').catch(() => ({}))).started;
    let r = await api('/api/restart', {}).catch((e) => ({ ok: false, out: e.message }));
    if (!r.ok && r.busy) {
      if (!window.confirm(`${r.out} Restarting stops them. Restart anyway?`)) return;
      r = await api('/api/restart', { force: true }).catch((e) => ({ ok: false, out: e.message }));
    }
    if (!r.ok) { toast(r.out || 'Could not restart'); return; }
    b.disabled = true;
    b.textContent = 'Restarting…';
    const t0 = Date.now();
    const wait = async () => {
      try { const s = await api('/api/state'); if (s.started && s.started !== before) { location.reload(); return; } } catch { /* not back yet */ }
      if (Date.now() - t0 > 90000) { toast('Flock did not come back: start it again (python run.py)', 10000); b.textContent = 'Restart now'; b.disabled = false; return; }
      setTimeout(wait, 1000);
    };
    setTimeout(wait, 1500);
  }

  async function deps(body, force) {
    body.innerHTML = '<div class="muted">Checking the tools…</div>';
    const r = await api('/api/updates' + (force ? '?force=1' : '')).catch((e) => ({ items: [], error: e.message }));
    body.innerHTML = '';
    const t = el('table', 'tbl');
    t.innerHTML = '<tr><th>Package</th><th>Installed</th><th>Newest</th><th></th></tr>' + r.items.map((i) =>
      `<tr><td>${esc(i.name)}${i.note ? `<div class="muted">${esc(i.note)}</div>` : ''}${i.error ? `<div class="muted">${esc(i.error)}</div>` : ''}</td><td>${esc(i.current || '')}</td><td class="${i.outdated ? 'new' : ''}">${esc(i.latest || '')}</td>` +
      `<td>${i.outdated && i.action === 'update' ? `<button class="link" data-up="${esc(i.id)}">Update</button>` : ''}</td></tr>`).join('');
    body.appendChild(t);
    const foot = el('p', 'muted', `Checked in the background at most every 12 hours; last ${esc(when(r.at))}. `);
    const btn = el('button', 'link', 'Check now');
    btn.onclick = () => deps(body, true);
    foot.appendChild(btn);
    body.appendChild(foot);
    t.querySelectorAll('[data-up]').forEach((b) => {
      b.onclick = async () => { b.textContent = 'Updating…'; const x = await api('/api/updates/apply', { id: b.dataset.up }); toast(x.ok ? 'Updated' : x.out); deps(body, true); };
    });
  }

  /* ---- general */
  function general(body) {
    body.innerHTML = '<h3>General</h3>';
    const f = el('div', 'form');
    f.innerHTML = `<label>Default model</label><select id="gEp">${S.cfg.endpoints.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join('')}</select>` +
      `<label>Default thinking</label><select id="gR"><option value="fast">Fast (no thinking)</option><option value="think">Think</option><option value="deep">Deep</option></select>` +
      `<label>Compact at</label><select id="gC"><option value="0.7">70% of the window</option><option value="0.8">80%</option><option value="0.85">85%</option><option value="0.9">90%</option></select>` +
      `<label>Mic button</label><select id="gD"><option value="">${S.cfg.features?.dictation ? 'Whisper on this computer' : 'Off (Whisper is not set up: python install.py --with-dictation)'}</option>` +
      `<option value="browser">The browser's speech recognition (sends your audio to Google's servers)</option><option value="off">Off</option></select>`;
    body.appendChild(f);
    f.querySelector('#gEp').value = S.cfg.endpoint;
    f.querySelector('#gR').value = S.cfg.reasoning;
    f.querySelector('#gC').value = String(S.cfg.compact_at);
    f.querySelector('#gD').value = ['browser', 'off'].includes(S.cfg.features?.dictation_engine) ? S.cfg.features.dictation_engine : '';
    const sv = el('button', 'primary', 'Save');
    sv.onclick = async () => {
      const r = await api('/api/config', { endpoint: $('gEp').value, reasoning: $('gR').value, compact_at: +$('gC').value,
        dictation_engine: $('gD').value });
      S.cfg = r.config;
      KCH.applyFeatures?.();
      toast('Saved');
    };
    body.appendChild(sv);
    body.appendChild(el('p', 'muted', `Data (chats, notes, logs, keys): ${esc(S.cfg.data)}. New projects go in ${esc(S.cfg.workspace)}.` +
      (S.cfg.profile ? ` This machine's own setup: ${esc(S.cfg.profile)}.` : ' No profile.json: everything here is set in the app.')));
  }

  window.KCS = { open };
})();
