'use strict';
/* Unified desktop sheets, chat sections, subagents panel, model selector, screen capture, and narrow-window drawer support.
   Loaded after app.js and chat-list.js. */
(() => {
  const { S, $, LS, esc, el, api, toast } = window.KCH;
  const NARROW_WIN = window.matchMedia('(max-width: 600px)');

  const ICONS = {
    plus: '<svg viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg>',
    clip: '<svg viewBox="0 0 16 16"><path d="M10.5 4.5l-5 5a1.8 1.8 0 0 0 2.5 2.5l5.2-5.2a3 3 0 0 0-4.3-4.3L3.7 7.7a4.2 4.2 0 0 0 6 6l4-4"/></svg>',
    chev: '<svg viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg>',
    check: '<svg class="tick" viewBox="0 0 16 16"><path d="M3.2 8.4l3.1 3.1 6.5-6.8"/></svg>',
    close: '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
    select: '<svg viewBox="0 0 16 16"><rect x="2.5" y="2.5" width="11" height="11" rx="2"/><path d="M5.4 8.2l1.9 1.9 3.5-3.8"/></svg>',
    edit: '<svg viewBox="0 0 16 16"><path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/><circle cx="5.5" cy="4.5" r="1.2" fill="currentColor"/><circle cx="10.5" cy="8" r="1.2" fill="currentColor"/><circle cx="6.5" cy="11.5" r="1.2" fill="currentColor"/></svg>',
    up: '<svg viewBox="0 0 16 16"><path d="M4 10l4-4 4 4"/></svg>',
    down: '<svg viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg>',
    trash: '<svg viewBox="0 0 16 16"><path d="M3 4.5h10M6 4.5V3h4v1.5M5 4.5l.6 8.5h4.8l.6-8.5"/></svg>',
    fork: '<svg viewBox="0 0 16 16"><path d="M5 3v4.5a3 3 0 0 0 3 3h3M11 8.5l2 2-2 2M5 7.5V13"/></svg>',
  };

  /* ---------------------------------------------------------------- narrow-window drawer scrim */
  const scrim = el('div', '');
  scrim.id = 'drawerScrim';
  document.body.appendChild(scrim);
  scrim.addEventListener('click', () => closeDrawers());

  const extras = el('div', 'm-extras');
  const moved = ['projectBtn', 'ctxBtn', 'meetBtn', 'setBtn', 'themeBtn'];
  const home = {};

  function syncScrim() {
    const sideOpen = KCH.sideShown();
    if (sideOpen) KCH.flushList?.();
    const open = NARROW_WIN.matches && (sideOpen || KCH.rightShown());
    scrim.classList.toggle('on', open);
  }
  KCH.onLayout = syncScrim;

  function closeDrawers() {
    if (!NARROW_WIN.matches) return false;
    if (!KCH.sideShown() && !KCH.rightShown()) return false;
    KCH.layout.mside = false;
    KCH.layout.mright = false;
    KCH.saveLayout();
    return true;
  }

  /* ---------------------------------------------------------------- modal sheet component */
  const sheetScrim = el('div', 'm-sheet-scrim');
  const sheet = el('div', 'm-sheet');
  sheet.innerHTML = `<div class="m-sheet-handle"><i></i></div>` +
    `<div class="m-sheet-head"><h3 id="mSheetTitle"></h3><button class="m-close" aria-label="Close">${ICONS.close}</button></div>` +
    `<div class="m-sheet-body" id="mSheetBody"></div>`;
  document.body.append(sheetScrim, sheet);

  let sheetOnClose = null;
  let sheetGen = 0;
  function sheetOpen() { return sheet.classList.contains('open'); }
  function openSheet(title, buildFn, onClose) {
    sheetGen++;
    $('mSheetTitle').textContent = title || '';
    const body = $('mSheetBody');
    body.innerHTML = '';
    buildFn(body);
    sheetOnClose = onClose || null;
    sheetScrim.classList.add('open');
    sheet.classList.add('open');
    body.scrollTop = 0;
  }
  function closeSheet() {
    if (!sheetOpen()) return false;
    sheetGen++;
    sheetScrim.classList.remove('open');
    sheet.classList.remove('open');
    const cb = sheetOnClose;
    sheetOnClose = null;
    cb?.();
    return true;
  }
  sheetScrim.addEventListener('click', closeSheet);
  sheet.querySelector('.m-close').addEventListener('click', closeSheet);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sheetOpen()) { e.preventDefault(); e.stopPropagation(); closeSheet(); }
  }, true);
  KCH.openSheet = openSheet;
  KCH.closeSheet = closeSheet;

  /* ---------------------------------------------------------------- sidebar Select button */
  const selToggle = el('button', 'm-selbtn', ICONS.select);
  selToggle.title = 'Select chats to move or archive';
  selToggle.setAttribute('aria-label', 'Select chats');
  $('newChat').after(selToggle);

  S.allChats = true;

  /* ---------------------------------------------------------------- chat header helpers, archive & build-update bars */
  const titleWrap = document.querySelector('#chatHead .title');
  const titleChev = el('span', 'm-title-chev', ICONS.chev);
  titleChev.setAttribute('aria-hidden', 'true');
  titleWrap.appendChild(titleChev);
  titleWrap.addEventListener('click', () => {
    if (!NARROW_WIN.matches || !S.chat) return;
    openChatActionsSheet();
  });

  const mForkBtn = el('button', 'm-fork-btn', ICONS.fork);
  mForkBtn.id = 'mForkBtn';
  mForkBtn.type = 'button';
  mForkBtn.title = 'Fork chat';
  mForkBtn.setAttribute('aria-label', 'Fork chat');
  mForkBtn.hidden = true;
  mForkBtn.onclick = () => KCH.forkChat?.(S.chat);
  titleWrap.after(mForkBtn);

  const updBar = el('div', 'm-upd-bar');
  updBar.id = 'mUpdBar';
  updBar.hidden = true;
  updBar.innerHTML = '<div class="m-upd-txt"><b>Screen update ready</b><small>Reloading only refreshes the view. Your chats and model context stay safe.</small></div>' +
    '<button class="primary" data-reload>Reload</button>' +
    '<button class="m-upd-x" data-dismiss title="Dismiss" aria-label="Dismiss">&times;</button>';
  function safeReload() {
    try {
      const draft = $('input')?.value || '';
      if (draft.trim()) sessionStorage.setItem('kch.reloadDraft', JSON.stringify({ chat: S.chat || '', text: draft }));
    } catch { /* sessionStorage unavailable */ }
    location.reload();
  }
  updBar.querySelector('[data-reload]').onclick = safeReload;
  $('chatHead').before(updBar);

  const archBar = el('div', 'm-arch-bar');
  archBar.id = 'mArchBar';
  archBar.hidden = true;
  archBar.innerHTML = '<span>This chat is archived.</span><button class="link" data-restore>Restore</button>';
  archBar.querySelector('[data-restore]').onclick = () => { if (S.chat) KCH.archiveChats?.([S.chat], [], false); };
  $('chatHead').after(archBar);

  function paintArchBar(meta) {
    if (!meta || !meta.archived) { archBar.hidden = true; return; }
    archBar.hidden = false;
    const why = meta.archive_reason && meta.archive_reason !== 'manual' ? ` (${meta.archive_reason})` : '';
    archBar.querySelector('span').textContent = `Archived chat${why}`;
  }

  function fmtAgo(ts) {
    if (!ts) return 'not synced yet';
    const sec = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
    if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
    return `${Math.round(sec / 86400)} d ago`;
  }

  const headTools = el('span', 'm-head-tools');
  const headSync = el('button', 'm-head-btn m-head-sync');
  headSync.type = 'button';
  headSync.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.2 6.2A5.4 5.4 0 0 0 3.6 4.6M2.8 9.8a5.4 5.4 0 0 0 9.6 1.6"/><path d="M3.4 2.2v2.6H6M12.6 13.8v-2.6H10"/></svg>';
  headSync.setAttribute('aria-label', 'Sync imported chat');
  headSync.hidden = true;
  const headSubs = el('button', 'm-head-btn m-head-subs');
  headSubs.type = 'button';
  headSubs.hidden = true;
  headSubs.onclick = () => KCH.openSubagentsSheet?.();
  headTools.append(headSubs, headSync);
  $('chatHead')?.append(headTools);

  function paintSyncBar(meta) {
    if (!meta?.live_import) { headSync.hidden = true; return; }
    const ts = meta.synced_at || (S.sourcesSynced || {})[meta.source] || 0;
    headSync.hidden = false;
    headSync.title = 'Bring this imported chat up to date (last: ' + fmtAgo(ts) + ')';
    headSync.onclick = async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      btn.classList.add('spin');
      try {
        let j = await api(`/api/chats/${S.chat}/sync`, {});
        while (j && j.state === 'running') {
          await new Promise((r) => setTimeout(r, 400));
          j = await api('/api/chat-import/job?id=' + encodeURIComponent(j.id));
        }
        await window.loadChats();
        if (S.chat && !S.busy.has(S.chat)) await KCH.openChat(S.chat);
        toast('Synced');
      } catch (e) {
        toast(e.message || 'Sync failed');
      }
      btn.disabled = false;
      btn.classList.remove('spin');
    };
  }

  function paintHeadSubs() {
    const subs = S.meta?.sub_of ? [] : (S.embeddedSubs || []);
    const running = subs.filter((c) => c.busy).length;
    headSubs.hidden = !subs.length;
    headSubs.classList.toggle('run', running > 0);
    headSubs.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="5.5" width="10" height="7.5" rx="2.2"/><path d="M8 5.5V3.2"/><circle cx="8" cy="2.5" r=".9"/><circle cx="6.1" cy="9" r=".9"/><circle cx="9.9" cy="9" r=".9"/><path d="M1.6 8.6v2M14.4 8.6v2"/></svg>';
    headSubs.append(el('b', 'm-head-n', String(subs.length)));
    headSubs.title = subs.length + ' subagent' + (subs.length === 1 ? '' : 's') + (running ? ', ' + running + ' working' : '');
    headSubs.setAttribute('aria-label', headSubs.title);
    if (!S.meta?.live_import) headSync.hidden = true;
  }
  KCH.paintHeadSubs = paintHeadSubs;

  /* ---------------------------------------------------------------- Recent strip + Sections bar + Auto-sort banner + Select bar */
  const recentBox = el('div', 'm-recent');
  recentBox.hidden = true;
  recentBox.innerHTML = '<div class="m-recent-lbl">Recent</div><div class="m-recent-row" id="mRecentRow"></div>';

  const secWrap = el('div', 'm-secwrap');
  secWrap.innerHTML = `<div class="m-secbar" id="mSecBar" role="tablist" aria-label="Chat sections"></div>` +
    `<button class="m-secedit" id="mSecEdit" title="Edit sections" aria-label="Edit sections">${ICONS.edit}</button>`;

  const sortBar = el('div', 'm-sortbar');
  sortBar.hidden = true;
  sortBar.innerHTML = `<span>Chats sorted into sections</span><button class="link" data-undo>Undo</button><button class="m-x" data-dismiss aria-label="Dismiss">${ICONS.close}</button>`;

  const selBar = el('div', 'm-selbar');
  selBar.hidden = true;
  selBar.innerHTML = `<span id="mSelCount">0 selected</span><button class="ghost" data-archive>Archive</button><button class="primary" data-move>Move…</button><button class="ghost" data-done>Done</button>`;

  document.querySelector('#side .search').after(recentBox, secWrap, sortBar, selBar);

  let curSection = LS.get('activeSection', 'general');
  function sectionsList() {
    return (S.sections && S.sections.sections && S.sections.sections.length)
      ? S.sections.sections
      : [{ id: 'general', name: 'General' }, { id: 'work', name: 'Work' }, { id: 'personal', name: 'Personal' }];
  }
  function activeSection() {
    const list = sectionsList();
    if (!list.some((s) => s.id === curSection)) curSection = list[0].id;
    return curSection;
  }
  KCH.activeSection = activeSection;

  function setActiveSection(sid) {
    curSection = sid;
    LS.set('activeSection', sid);
    paintSectionsUI();
    KCH.paintList();
  }

  function paintSectionsUI() {
    const bar = $('mSecBar');
    if (!bar) return;
    const list = sectionsList();
    const cur = activeSection();
    const busySecs = new Set();
    for (const c of S.chats || []) {
      if (S.busy.has(c.id) && c.section) busySecs.add(c.section);
    }
    bar.replaceChildren(...list.map((s) => {
      const b = el('button', s.id === cur ? 'on' : '');
      b.dataset.sec = s.id;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', s.id === cur ? 'true' : 'false');
      b.innerHTML = `<span>${esc(s.name)}</span>${busySecs.has(s.id) ? '<i class="sdot"></i>' : ''}`;
      b.onclick = () => setActiveSection(s.id);
      return b;
    }));

    const cb = S.sections && S.sections.cleanup_banner;
    if (cb && cb.text) {
      sortBar.hidden = false;
      sortBar.dataset.which = 'cleanup';
      sortBar.dataset.undoId = cb.undo_id || '';
      sortBar.querySelector('span').textContent = cb.text;
    } else if (S.sections && S.sections.banner) {
      sortBar.hidden = false;
      sortBar.dataset.which = 'autosort';
      sortBar.dataset.undoId = S.sections.banner_undo_id || '';
      sortBar.querySelector('span').textContent = 'Chats sorted into sections';
    } else {
      sortBar.hidden = true;
    }
    paintRecent();
  }

  sortBar.querySelector('[data-undo]').onclick = async () => {
    try {
      const uid = sortBar.dataset.undoId || S.sections?.cleanup_banner?.undo_id || S.sections?.banner_undo_id || undefined;
      const r = await api('/api/sections/undo', { id: uid });
      if (r.ok) {
        S.sections = r.state;
        toast(r.label ? `Undid: ${r.label}` : 'Undone');
        await window.loadChats();
        if (S.chat && S.meta) {
          const cur = (S.chats || []).find((x) => x.id === S.chat);
          if (cur) { S.meta.archived = !!cur.archived; S.meta.archive_reason = cur.archive_reason || ''; paintArchBar(S.meta); }
        }
      } else {
        toast(r.error || 'Nothing to undo');
      }
    } catch (e) { toast(e.message); }
  };
  sortBar.querySelector('[data-dismiss]').onclick = async () => {
    const which = sortBar.dataset.which || undefined;
    sortBar.hidden = true;
    try { const r = await api('/api/sections/dismiss-banner', { which }); if (r.state) S.sections = r.state; } catch { /* ignore */ }
  };
  $('mSecEdit').onclick = () => openEditSectionsSheet();

  /* ---- Recent quick-switch strip ---- */
  function getRecent() { return LS.get('recentChats', []); }
  function recordRecent(id) {
    if (!id) return;
    const list = getRecent().filter((x) => x !== id);
    list.unshift(id);
    LS.set('recentChats', list.slice(0, 8));
    paintRecent();
  }
  function paintRecent() {
    const row = $('mRecentRow');
    if (!row) return;
    const q = (S.filter || '').trim();
    if (q) { recentBox.hidden = true; return; }
    const byId = new Map((S.chats || []).filter((c) => !c.archived).map((c) => [c.id, c]));
    const secNames = new Map(sectionsList().map((s) => [s.id, s.name]));
    let ids = getRecent().filter((id) => byId.has(id));
    if (ids.length < 3) {
      const extra = (S.chats || []).filter((c) => !c.archived).slice().sort((a, b) => (b.updated || 0) - (a.updated || 0)).map((c) => c.id);
      for (const id of extra) {
        if (!ids.includes(id)) ids.push(id);
        if (ids.length >= 5) break;
      }
    }
    ids = ids.slice(0, 5);
    if (!ids.length) { recentBox.hidden = true; return; }
    recentBox.hidden = false;
    row.replaceChildren(...ids.map((id) => {
      const c = byId.get(id);
      const busy = S.busy.has(id);
      const b = el('button', 'm-rchip' + (id === S.chat ? ' on' : ''));
      const sn = secNames.get(c.section) || '';
      const ago = busy ? (KCH.actAgo?.(id) || '') : (KCH.bgLabel?.(id) || '');
      b.title = busy ? (KCH.actLabel?.(id) || '') : (ago ? 'Running in the background: ' + (KCH.actLabel?.(id) || '') : '');
      b.innerHTML = `${busy ? '<span class="rdot"></span>' : ''}<span class="rt">${esc(c.title || 'New chat')}</span>` +
        (ago ? `<span class="rsec ago">${esc(ago)}</span>` : '') +
        (sn ? `<span class="rsec">${esc(sn)}</span>` : '');
      b.onclick = async () => {
        await window.openChat(id);
        closeDrawers();
      };
      return b;
    }));
  }

  /* ---- Multi-select mode ---- */
  const multiSelect = {
    active: false,
    chats: new Set(),
    groups: new Set(),
    enter(init) {
      this.active = true;
      this.chats.clear();
      this.groups.clear();
      if (init?.chat) this.chats.add(init.chat);
      if (init?.group) this.groups.add(init.group);
      this.sync();
    },
    exit() {
      this.active = false;
      this.chats.clear();
      this.groups.clear();
      this.sync();
    },
    toggleChat(id) {
      if (this.chats.has(id)) this.chats.delete(id); else this.chats.add(id);
      this.sync();
    },
    toggleGroup(id) {
      if (this.groups.has(id)) this.groups.delete(id); else this.groups.add(id);
      this.sync();
    },
    count() { return this.chats.size + this.groups.size; },
    sync() {
      selToggle.classList.toggle('on', this.active);
      selBar.hidden = !this.active;
      const n = this.count();
      $('mSelCount').textContent = n ? `${n} selected` : 'Pick chats or groups';
      selBar.querySelector('[data-move]').disabled = n === 0;
      const archBtn = selBar.querySelector('[data-archive]');
      if (archBtn) archBtn.disabled = n === 0;
      KCH.paintList();
    },
  };
  KCH.phoneSelect = multiSelect;

  selToggle.onclick = () => (multiSelect.active ? multiSelect.exit() : multiSelect.enter());
  selBar.querySelector('[data-done]').onclick = () => multiSelect.exit();
  selBar.querySelector('[data-archive]')?.addEventListener('click', () => {
    if (!multiSelect.count()) return;
    KCH.archiveChats([...multiSelect.chats], [...multiSelect.groups], true);
  });
  selBar.querySelector('[data-move]').onclick = () => {
    if (!multiSelect.count()) return;
    openMoveSheet({ chatIds: [...multiSelect.chats], groupIds: [...multiSelect.groups] });
  };

  async function archiveChats(chatIds, groupIds, archived = true) {
    try {
      const r = await api('/api/sections/archive', { chats: chatIds || [], groups: groupIds || [], archived: !!archived });
      if (r.state) S.sections = r.state;
      if (multiSelect.active) multiSelect.exit();
      await window.loadChats();
      if (S.chat && S.meta) {
        const cur = (S.chats || []).find((x) => x.id === S.chat);
        if (cur) { S.meta.archived = !!cur.archived; S.meta.archive_reason = cur.archive_reason || ''; paintArchBar(S.meta); }
      }
      if (r.undo_id) showUndoToast(r.label || (archived ? 'Archived' : 'Restored'), r.undo_id);
      else toast(r.label || (archived ? 'Already archived' : 'Not archived'));
    } catch (e) { toast(e.message); }
  }
  KCH.archiveChats = archiveChats;

  async function doSectionMove(secId, chatIds, groupIds) {
    try {
      const r = await api('/api/sections/move', { section: secId, chats: chatIds, groups: groupIds });
      if (r.state) S.sections = r.state;
      multiSelect.exit();
      await window.loadChats();
      if (r.undo_id) showUndoToast(r.label || 'Moved', r.undo_id);
      else toast(r.label || 'Already there');
    } catch (e) { toast(e.message); }
  }

  function showUndoToast(msg, undoId) {
    const t = $('toast');
    t.innerHTML = '';
    t.appendChild(document.createTextNode(msg + ' '));
    const u = el('button', 'link', 'Undo');
    u.style.color = 'inherit';
    u.style.fontWeight = '600';
    u.style.marginLeft = '6px';
    u.onclick = async () => {
      t.hidden = true;
      try {
        const r = await api('/api/sections/undo', { id: undoId });
        if (r.ok) { S.sections = r.state; await window.loadChats(); toast('Undone'); }
      } catch (e) { toast(e.message); }
    };
    t.appendChild(u);
    t.hidden = false;
    clearTimeout(toast.h);
    toast.h = setTimeout(() => { t.hidden = true; }, 5500);
  }

  function openMoveSheet({ chat, group, chatIds, groupIds }) {
    const secs = sectionsList();
    const curSec = group ? group.section : chat ? chat.section : null;
    const title = group ? group.name : chat ? (chat.title || 'Chat') : `Move ${((chatIds?.length || 0) + (groupIds?.length || 0))} items`;
    openSheet(title, (body) => {
      body.appendChild(el('div', 'm-sh-grp', 'Move to section'));
      for (const s of secs) {
        const on = curSec === s.id;
        const row = el('button', 'm-sh-row' + (on ? ' on' : ''));
        row.innerHTML = `<span class="txt"><b>${esc(s.name)}</b></span>${on ? ICONS.check : ''}`;
        row.onclick = () => {
          closeSheet();
          if (group) doSectionMove(s.id, [], [group.id]);
          else if (chat) doSectionMove(s.id, [chat.id], []);
          else doSectionMove(s.id, chatIds || [], groupIds || []);
        };
        body.appendChild(row);
      }
      if (chat || group) {
        body.appendChild(el('div', 'm-sh-grp', 'Actions'));
        if (chat) {
          const fk = el('button', 'm-sh-row');
          fk.innerHTML = `<span class="ico">${ICONS.fork}</span><span class="txt"><b>Fork</b></span>`;
          fk.onclick = async () => { closeSheet(); await KCH.forkChat?.(chat.id); closeDrawers(); };
          body.appendChild(fk);
        }
        const multi = el('button', 'm-sh-row');
        multi.innerHTML = `<span class="ico">${ICONS.select}</span><span class="txt"><b>Select multiple</b><small>Pick several chats or groups to move or archive together</small></span>`;
        multi.onclick = () => { closeSheet(); multiSelect.enter(group ? { group: group.id } : { chat: chat.id }); };
        body.appendChild(multi);

        if (chat) {
          const arch = el('button', 'm-sh-row');
          arch.innerHTML = `<span class="txt"><b>${chat.archived ? 'Restore from archive' : 'Archive chat'}</b><small>${chat.archived ? 'Return this chat to its section list' : 'Hide from the list without deleting (still searchable)'}</small></span>`;
          arch.onclick = () => { closeSheet(); archiveChats([chat.id], [], !chat.archived); };
          body.appendChild(arch);

          const ren = el('button', 'm-sh-row');
          ren.innerHTML = '<span class="txt"><b>Rename</b></span>';
          ren.onclick = async () => {
            closeSheet();
            const n = window.prompt('Chat name', chat.title || '');
            if (!n || !n.trim()) return;
            await api(`/api/chats/${chat.id}/meta`, { title: n.trim() });
            chat.title = n.trim();
            if (chat.id === S.chat) $('chatTitle').textContent = n.trim();
            KCH.paintList();
          };
          body.appendChild(ren);

          const del = el('button', 'm-sh-row danger');
          del.innerHTML = '<span class="txt"><b>Delete chat</b></span>';
          del.onclick = () => { closeSheet(); KCH.deleteChat(chat.id); };
          body.appendChild(del);
        }
        if (group) {
          const archG = el('button', 'm-sh-row');
          archG.innerHTML = '<span class="txt"><b>Archive all in this group</b><small>Hide every chat in this group without deleting</small></span>';
          archG.onclick = () => { closeSheet(); archiveChats([], [group.id], true); };
          body.appendChild(archG);

          const ren = el('button', 'm-sh-row');
          ren.innerHTML = '<span class="txt"><b>Rename group</b></span>';
          ren.onclick = async () => {
            closeSheet();
            const n = window.prompt('Group name', group.name || '');
            if (!n || !n.trim() || n.trim() === group.name) return;
            const r = await api('/api/groups/update', { id: group.id, name: n.trim() });
            S.groups = r.groups;
            await window.loadChats();
          };
          body.appendChild(ren);

          const nc = el('button', 'm-sh-row');
          nc.innerHTML = '<span class="txt"><b>New chat in this group</b></span>';
          nc.onclick = async () => { closeSheet(); await KCH.newChat({ group: group.id }); closeDrawers(); };
          body.appendChild(nc);
        }
      }
    });
  }
  KCH.openMoveSheet = openMoveSheet;
  KCH.openEditSectionsSheet = openEditSectionsSheet;
  KCH.doSectionMove = doSectionMove;

  function openChatActionsSheet() {
    if (!S.chat) return;
    const isArch = !!S.meta?.archived;
    const isSub = !!S.meta?.sub_of;
    openSheet(S.meta?.title || 'This chat', (body) => {
      const fk = el('button', 'm-sh-row');
      fk.innerHTML = `<span class="ico">${ICONS.fork}</span><span class="txt"><b>Fork</b></span>`;
      fk.onclick = () => { closeSheet(); KCH.forkChat?.(S.chat); };
      body.appendChild(fk);

      if (isSub) {
        const rep = el('button', 'm-sh-row');
        const pBusy = !!(S.parentInfo?.busy || S.busy.has(S.meta.sub_of));
        rep.innerHTML = `<span class="txt"><b>Report back to main chat</b>` +
          `<small>${pBusy ? 'Main chat is working · send findings or steer it' : 'Send findings from this subagent to the main chat'}</small></span>`;
        rep.onclick = () => openReportMainSheet();
        body.appendChild(rep);
      } else {
        const ren = el('button', 'm-sh-row');
        ren.innerHTML = '<span class="txt"><b>Rename chat</b><small>Change the title shown in your list</small></span>';
        ren.onclick = () => { closeSheet(); $('renameBtn').click(); };
        body.appendChild(ren);

        const cmp = el('button', 'm-sh-row');
        cmp.innerHTML = '<span class="txt"><b>Compact memory</b><small>Summarise earlier turns to free up context</small></span>';
        cmp.onclick = () => { closeSheet(); $('compactBtn').click(); };
        body.appendChild(cmp);

        const subCount = (S.cfg?.subagents?.presets || []).length;
        const subRow = el('button', 'm-sh-row');
        subRow.innerHTML = `<span class="txt"><b>Subagents${subCount ? ` (${subCount})` : ''}</b><small>Open or configure embedded subagent chats</small></span>`;
        subRow.onclick = () => openSubagentsSheet();
        body.appendChild(subRow);

        const arch = el('button', 'm-sh-row');
        arch.innerHTML = `<span class="txt"><b>${isArch ? 'Restore from archive' : 'Archive chat'}</b><small>${isArch ? 'Bring back to its section' : 'Hide from the list without deleting'}</small></span>`;
        arch.onclick = () => { closeSheet(); archiveChats([S.chat], [], !isArch); };
        body.appendChild(arch);

        const del = el('button', 'm-sh-row danger');
        del.innerHTML = '<span class="txt"><b>Delete chat</b><small>Remove from the list (files on disk stay)</small></span>';
        del.onclick = () => { closeSheet(); $('deleteBtn').click(); };
        body.appendChild(del);
      }
    });
  }

  function openEditSectionsSheet() {
    let draft = sectionsList().map((s) => ({ ...s }));
    openSheet('Edit sections', (body) => {
      const render = () => {
        body.innerHTML = '';
        body.appendChild(el('div', 'm-sh-grp', 'Rename, reorder, add or remove'));
        draft.forEach((s, i) => {
          const row = el('div', 'm-edsec');
          const inp = el('input', '');
          inp.value = s.name;
          inp.placeholder = 'Section name';
          inp.oninput = () => { s.name = inp.value; };
          const up = el('button', '', ICONS.up);
          up.disabled = i === 0;
          up.title = 'Move up';
          up.onclick = () => { [draft[i - 1], draft[i]] = [draft[i], draft[i - 1]]; render(); };
          const dn = el('button', '', ICONS.down);
          dn.disabled = i === draft.length - 1;
          dn.title = 'Move down';
          dn.onclick = () => { [draft[i + 1], draft[i]] = [draft[i], draft[i + 1]]; render(); };
          const rm = el('button', '', ICONS.trash);
          rm.disabled = draft.length <= 1;
          rm.title = 'Remove section';
          rm.onclick = () => { draft.splice(i, 1); render(); };
          row.append(inp, up, dn, rm);
          body.appendChild(row);
        });
        const foot = el('div', 'm-edsec-foot');
        const add = el('button', 'ghost', '+ Add section');
        add.onclick = () => { draft.push({ id: '', name: '' }); render(); body.querySelectorAll('input')[draft.length - 1]?.focus(); };
        const resort = el('button', 'ghost', 'Re-sort chats');
        resort.onclick = async () => {
          closeSheet();
          try {
            const r = await api('/api/sections/autosort', {});
            S.sections = r.state;
            await window.loadChats();
            showUndoToast('Sorted chats into sections', r.undo_id);
          } catch (e) { toast(e.message); }
        };
        const cleanBtn = el('button', 'ghost', 'Clean up residue');
        cleanBtn.onclick = async () => {
          closeSheet();
          try {
            const r = await api('/api/sections/cleanup', {});
            S.sections = r.state;
            await window.loadChats();
            if (r.undo_id) showUndoToast(r.label || 'Cleaned up chats', r.undo_id);
            else toast(r.label || 'Already clean');
          } catch (e) { toast(e.message); }
        };
        const save = el('button', 'primary', 'Save');
        save.onclick = async () => {
          const clean = draft.filter((x) => x.name && x.name.trim());
          if (!clean.length) return toast('Keep at least one section');
          try {
            const r = await api('/api/sections/config', { sections: clean });
            S.sections = r.state;
            closeSheet();
            await window.loadChats();
            toast('Sections saved');
          } catch (e) { toast(e.message); }
        };
        foot.append(add, resort, cleanBtn, save);
        body.appendChild(foot);
      };
      render();
    });
  }

  /* ---------------------------------------------------------------- Effort tracking for model sheet & narrow composer */
  let lastLevels = [];
  let lastLevelId = '';
  const effortBtn = el('button', '');
  effortBtn.id = 'mEffortBtn';
  effortBtn.type = 'button';
  effortBtn.title = 'Reasoning effort';
  const tSpacer = el('span', 'm-tspacer');
  $('attachBtn').after(effortBtn, tSpacer);

  function paintEffortPill(levels, curId) {
    lastLevels = Array.isArray(levels) ? levels : [];
    lastLevelId = curId || '';
    const ep = KCH.curEp();
    const cur = lastLevels.find((l) => l.id === curId) || lastLevels[0];
    if (!ep || ep.no_thinking || !cur) {
      effortBtn.hidden = true;
      return;
    }
    effortBtn.hidden = false;
    effortBtn.textContent = cur.label;
    effortBtn.classList.toggle('high', ['think', 'deep', 'high', 'xhigh', 'max'].includes(cur.id));
  }
  KCH.onReasonPaint = paintEffortPill;
  effortBtn.onclick = () => {
    const ep = KCH.curEp();
    if (!lastLevels.length) return;
    openSheet(`Effort (${ep?.name || 'Model'})`, (body) => {
      for (const lv of lastLevels) {
        const on = lv.id === lastLevelId;
        const row = el('button', 'm-sh-row' + (on ? ' on' : ''));
        row.innerHTML = `<span class="txt"><b>${esc(lv.label)}</b>${lv.hint ? `<small>${esc(lv.hint)}</small>` : ''}</span>${on ? ICONS.check : ''}`;
        row.onclick = async () => {
          closeSheet();
          try { await KCH.pickReason(lv.id); } catch (e) { toast(e.message); }
        };
        body.appendChild(row);
      }
    });
  };

  /* ---------------------------------------------------------------- Unified Model Picker Sheet */
  const DEFAULT_PRIORITY = ['anthropic', 'claude', 'openai', 'chatgpt', 'gemini', 'deepseek', 'openrouter'];
  let statusChecking = false;
  let statusUnreachable = false;
  let showMoreOffline = false;
  let onSheetStatuses = null;
  KCH.onStatuses = () => { onSheetStatuses?.(); };

  function modelScore(ep) {
    const localUse = LS.get('modelUse', {}) || {};
    const srvUse = S.cfg?.model_usage || {};
    const hits = (localUse[ep.id] || 0) * 10 + (srvUse[ep.id] || 0);
    const priIdx = DEFAULT_PRIORITY.indexOf(ep.id);
    const priBonus = priIdx >= 0 ? (DEFAULT_PRIORITY.length - priIdx) : 0;
    return hits * 100 + priBonus;
  }
  function noteModelPick(eid) {
    const m = LS.get('modelUse', {}) || {};
    m[eid] = (m[eid] || 0) + 1;
    LS.set('modelUse', m);
  }
  function statusFor(ep) {
    if (statusChecking) return 'checking';
    if (statusUnreachable) return 'offline';
    const s = S.statuses[ep.id];
    if (s) return s.state;
    if (['claude-code', 'codex', 'agy', 'opencode-go', 'anthropic'].includes(ep.kind)) return ep.has_key === false ? 'signed out' : 'ready';
    if (ep.has_key === false && !/127\.0\.0\.1|localhost/.test(ep.base || '')) return 'signed out';
    const h = S.health[ep.id];
    if (h) return h.ok ? 'ready' : 'offline';
    return /127\.0\.0\.1|localhost/.test(ep.base || '') ? 'offline' : 'ready';
  }

  let statusFetching = false;
  async function refreshStatuses(force = false) {
    if (statusFetching && !force) return;
    statusFetching = true;
    statusUnreachable = false;
    try {
      const r = await api('/api/endpoints/statuses' + (force ? '?force=1' : ''));
      if (r && r.statuses) {
        S.statuses = r.statuses;
        KCH.paintModel();
      }
    } catch {
      statusUnreachable = true;
      S.statuses = {};
    } finally {
      statusFetching = false;
    }
  }

  const COLD_CLAUDE_WARN_TOKENS = 20_000;
  let pendingColdEp = null;

  function claudeSwitchInfo(ep, cur) {
    if (!ep || ep.kind !== 'claude-code' || !ep.model) return null;
    if (!S.meta) return null;
    const hasSession = !!(S.meta.has_claude_session || S.meta.claude_model);
    if (!hasSession) return null;
    const lastModel = S.meta.claude_model || (cur && cur.kind === 'claude-code' ? cur.model : '');
    if (!lastModel || lastModel === ep.model) return null;
    const usedAt = Number((S.meta.claude_used && S.meta.claude_used[ep.model]) || 0);
    if (usedAt > 0 && (Date.now() / 1000 - usedAt) < 3300) {
      return { warm: true, text: 'Cache warm from earlier this hour' };
    }
    const tokens = Number(S.meta.ctx?.tokens || 0);
    if (tokens >= COLD_CLAUDE_WARN_TOKENS) {
      const k = Math.max(1, Math.round(tokens / 1000));
      return { cold: true, k, text: `This reads the whole chat again once, about ${k} k` };
    }
    return null;
  }

  async function commitModelSwitch(ep) {
    try {
      if (S.chat) await api(`/api/chats/${S.chat}/meta`, { endpoint: ep.id });
      await api('/api/config', { endpoint: ep.id });
      S.cfg.endpoint = ep.id;
      S.meta = { ...(S.meta || {}), endpoint: ep.id };
      noteModelPick(ep.id);
      KCH.paintModel();
      KCH.paintReason();
      KCH.paintCtx();
      pendingColdEp = null;
      closeSheet();
      if (!ep.runtime) KCH.checkHealth(ep);
    } catch (err) {
      toast(err.message || 'Could not switch model');
    }
  }

  function modelRow(ep, cur, body) {
    const on = cur && ep.id === cur.id;
    const st = statusFor(ep);
    const dotCls = st === 'ready' ? 'ok' : (st === 'signed out' || st === 'checking') ? 'wait' : 'bad';
    const pillCls = st === 'ready' ? 'ready' : st === 'signed out' ? 'out' : st === 'checking' ? 'chk' : 'off';
    const pillTxt = (st === 'offline' && statusUnreachable) ? 'unreachable' : st;
    const sw = on ? null : claudeSwitchInfo(ep, cur);
    const noteHtml = sw?.cold
      ? `<small class="m-cold-warn">${esc(sw.text).replace(/(\d+ k)$/, '<span style="white-space:nowrap">$1</span>')}</small>`
      : sw?.warm ? `<small class="m-warm-note">${esc(sw.text)}</small>` : '';
    const row = el('button', 'm-sh-row' + (on ? ' on' : ''));
    row.innerHTML = `<span class="dot ${dotCls}"></span><span class="txt"><b>${esc(ep.name)}</b>${noteHtml}</span>` +
      `<span class="st-pill ${pillCls}">${esc(pillTxt)}</span>${on ? ICONS.check : ''}`;
    row.onclick = async () => {
      if (sw?.cold && (!pendingColdEp || pendingColdEp.ep.id !== ep.id)) {
        pendingColdEp = { ep, info: sw };
        renderModelSheetBody(body);
        body.scrollTop = 0;
        return;
      }
      await commitModelSwitch(ep);
    };
    return row;
  }

  function renderModelSheetBody(body) {
    body.innerHTML = '';
    if (pendingColdEp) {
      const { ep, info } = pendingColdEp;
      const box = el('div', 'm-cold-confirm');
      box.innerHTML = `<div><b>Switch to ${esc(ep.name)}?</b><small>${esc(info.text)}</small></div>` +
        '<div class="m-owner-acts"><button class="primary" data-go>Switch</button><button class="ghost" data-cancel>Cancel</button></div>';
      box.querySelector('[data-go]').onclick = () => commitModelSwitch(ep);
      box.querySelector('[data-cancel]').onclick = () => { pendingColdEp = null; renderModelSheetBody(body); };
      body.appendChild(box);
    }
    if (statusUnreachable) {
      body.appendChild(el('div', 'm-sh-note', 'Could not reach the server to check model status. Retry once connected.'));
    }
    const eps = S.cfg?.endpoints || [];
    const cur = KCH.curEp();
    const visible = eps.filter((ep) => ep.show_in_picker !== false || (cur && ep.id === cur.id));
    const hiddenCount = eps.length - visible.length;

    if (!visible.length) {
      body.appendChild(el('div', 'm-sh-note', 'No models connected yet. Click Manage connections below to add an API key, local server, or CLI subscription.'));
    }

    const primary = [];
    const offline = [];
    for (const ep of visible) {
      const st = statusFor(ep);
      if (!statusChecking && !statusUnreachable && st === 'offline') offline.push(ep);
      else primary.push(ep);
    }
    primary.sort((a, b) => modelScore(b) - modelScore(a));
    offline.sort((a, b) => modelScore(b) - modelScore(a));

    for (const ep of primary) body.appendChild(modelRow(ep, cur, body));

    if (offline.length) {
      const activeInOffline = cur && offline.some((e) => e.id === cur.id);
      const open = showMoreOffline || activeInOffline;
      const more = el('button', 'm-more-btn' + (open ? ' open' : ''));
      more.innerHTML = `<span>More (${offline.length} offline)</span>${ICONS.chev}`;
      more.onclick = () => { showMoreOffline = !open; renderModelSheetBody(body); };
      body.appendChild(more);
      if (open) {
        for (const ep of offline) body.appendChild(modelRow(ep, cur, body));
      }
    }

    if (cur && !cur.no_thinking && lastLevels.length) {
      const box = el('div', 'm-sh-eff');
      box.innerHTML = `<div class="m-sh-eff-lbl">Effort for ${esc(cur.name)}</div><div class="m-sh-eff-row"></div>`;
      const row = box.querySelector('.m-sh-eff-row');
      for (const lv of lastLevels) {
        const b = el('button', lv.id === lastLevelId ? 'on' : '', esc(lv.label));
        b.onclick = async () => {
          try {
            await KCH.pickReason(lv.id);
            renderModelSheetBody(body);
          } catch (err) { toast(err.message); }
        };
        row.appendChild(b);
      }
      body.appendChild(box);
    }

    const subPresets = S.cfg?.subagents?.presets || [];
    const activeSubs = S.embeddedSubs || [];
    const subRow = el('button', 'm-sh-row');
    const busyN = activeSubs.filter((x) => x.busy).length;
    const subHint = activeSubs.length
      ? `${activeSubs.length} in this chat${busyN ? ` (${busyN} working)` : ''}` + (subPresets.length ? ` · ${subPresets.length} saved` : '')
      : subPresets.length
        ? subPresets.map((p) => `${p.name} (${p.model_name || p.model})`).slice(0, 3).join(', ') + (subPresets.length > 3 ? ` +${subPresets.length - 3}` : '')
        : 'Open an embedded subagent chat on any model';
    subRow.innerHTML = `<span class="txt"><b>Subagents${activeSubs.length ? ` (${activeSubs.length})` : subPresets.length ? ` (${subPresets.length})` : ''}</b><small>${esc(subHint)}</small></span>`;
    subRow.onclick = () => openSubagentsSheet();
    body.appendChild(subRow);

    if (cur?.runtime && S.runtime?.enabled && KCH.runtimeNodes) {
      const rtWrap = el('div', 'm-sh-eff');
      for (const n of KCH.runtimeNodes()) rtWrap.appendChild(n);
      body.appendChild(rtWrap);
    }

    const manage = el('button', 'm-sh-row');
    const hiddenTxt = hiddenCount ? ` (${hiddenCount} hidden)` : '';
    manage.innerHTML = `<span class="txt"><b>Manage connections…</b><small>Add API keys, local servers, or subscriptions${esc(hiddenTxt)}</small></span>`;
    manage.onclick = () => { closeSheet(); window.KCS?.open('connections'); };
    body.appendChild(manage);
  }

  function openModelSheet() {
    let bodyRef = null;
    const hadStatuses = Object.keys(S.statuses || {}).length > 0;
    statusChecking = !hadStatuses;
    statusUnreachable = false;
    showMoreOffline = false;
    pendingColdEp = null;
    openSheet('Model', (body) => {
      bodyRef = body;
      renderModelSheetBody(body);
    });
    const myGen = sheetGen;
    const repaint = () => {
      if (sheetOpen() && sheetGen === myGen && bodyRef && bodyRef.isConnected) renderModelSheetBody(bodyRef);
    };
    onSheetStatuses = () => { statusChecking = false; repaint(); };
    const prevSig = JSON.stringify([S.statuses, (S.cfg?.endpoints || []).map((e) => e.id)]);
    refreshStatuses(false).finally(() => {
      statusChecking = false;
      const nextSig = JSON.stringify([S.statuses, (S.cfg?.endpoints || []).map((e) => e.id)]);
      if (!hadStatuses || nextSig !== prevSig) repaint();
    });
  }
  KCH.openModelSheet = openModelSheet;

  $('modelBtn').addEventListener('click', (e) => {
    e.stopImmediatePropagation();
    e.preventDefault();
    openModelSheet();
  }, true);

  /* ---------------------------------------------------------------- Subagents Sheet & Report-to-Main Sheet */
  function openSubagentsSheet() {
    let bodyRef = null;
    let myGen = 0;
    const targetParent = S.chat;
    const depthNow = Number(S.meta?.depth || 0);
    const maxDepth = Number(S.cfg?.subagents?.max_depth ?? 2);
    const canNest = depthNow < maxDepth;
    let chatSubs = [...(S.embeddedSubs || [])];
    const syncPills = () => {
      if (!bodyRef || !bodyRef.isConnected || !sheetOpen() || sheetGen !== myGen) return;
      const eps = S.cfg?.endpoints || [];
      bodyRef.querySelectorAll('[data-st-ep]').forEach((pill) => {
        const ep = eps.find((e) => e.id === pill.dataset.stEp);
        const st = ep ? (S.statuses[ep.id]?.state || (statusUnreachable ? 'offline' : statusFor(ep))) : 'offline';
        const pillCls = st === 'ready' ? 'ready' : st === 'signed out' ? 'out' : st === 'checking' ? 'chk' : 'off';
        pill.className = 'st-pill ' + pillCls;
        pill.textContent = st;
      });
    };
    openSheet('Subagents', (body) => {
      bodyRef = body;
      const render = () => {
        body.innerHTML = '';
        const list = S.cfg?.subagents?.presets || [];
        const eps = S.cfg?.endpoints || [];
        body.appendChild(el('div', 'm-sh-note', depthNow
          ? "These are this subagent chat's own subagents. The main chat's ones stay with the main chat; go Back to see them."
          : "Each subagent is an embedded chat with this chat's context (auto-sized for its model + ParentContext search). Chat directly while the main chat works, or report findings back."));

        if (chatSubs.length) {
          body.appendChild(el('div', 'm-sh-grp', `In this chat (${chatSubs.length})`));
          for (const c of chatSubs) {
            const stCls = c.busy ? 'run' : c.status;
            const stLbl = c.busy ? ('Working' + (KCH.actAgo?.(c.id) ? ' · ' + KCH.actAgo(c.id) : ''))
              : (c.status === 'done' ? 'Done' : c.status === 'failed' ? 'Failed' : 'Ready');
            const card = el('div', 'm-sub-card m-sub-thread');
            card.setAttribute('role', 'button');
            card.tabIndex = 0;
            card.innerHTML = `<div class="m-sub-top"><span class="sub-dot ${stCls}"></span>` +
              `<div class="m-sub-info"><b>${esc(c.name)}</b><small>${esc(c.model_name)}</small></div>` +
              `<span class="st-pill ${c.busy ? 'ready' : ''}">${esc(stLbl)}</span>` +
              '<span class="m-sub-go">Open ›</span></div>' +
              (c.last_preview || c.description ? `<div class="m-sub-inst">${esc(c.last_preview || c.description)}</div>` : '');
            card.onclick = () => { closeSheet(); KCH.openChat(c.id); };
            card.onkeydown = (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); closeSheet(); KCH.openChat(c.id); } };
            body.appendChild(card);
          }
        }

        if (list.length) {
          body.appendChild(el('div', 'm-sh-grp', `Saved subagents (${list.length})`));
          for (const p of list) {
            const ep = eps.find((e) => e.id === p.model);
            const known = ep && S.statuses[ep.id];
            const st = known ? known.state : (ep ? statusFor(ep) : 'offline');
            const pillCls = st === 'ready' ? 'ready' : st === 'signed out' ? 'out' : st === 'checking' ? 'chk' : 'off';
            const card = el('div', 'm-sub-card');
            card.innerHTML = `<div class="m-sub-top"><div class="m-sub-info"><b>${esc(p.name)}</b>` +
              `<small>${esc(p.model_name || p.model)}</small></div>` +
              `<span class="st-pill ${pillCls}" data-st-ep="${esc(p.model)}">${esc(st)}</span>` +
              '<button class="primary m-sub-use" type="button" data-open>Chat</button>' +
              `<button class="m-sub-rm" type="button" data-rm title="Remove" aria-label="Remove">${ICONS.trash}</button></div>` +
              (p.instructions ? `<div class="m-sub-inst">${esc(p.instructions)}</div>` : '');
            const openBtn = card.querySelector('[data-open]');
            openBtn.onclick = async () => {
              if (!canNest) return toast(`Subagents go at most ${maxDepth} deep. Go back to the main chat to start another.`);
              openBtn.disabled = true;
              try {
                closeSheet();
                await KCH.spawnSubagent({ parent: targetParent, preset_id: p.id, fork_context: true });
              } catch (e) {
                toast(e.message);
              }
            };
            card.querySelector('[data-rm]').onclick = async () => {
              try {
                const r = await api('/api/subagents/presets/delete', { id: p.id });
                S.cfg = r.config;
                render();
                toast(`Removed ${p.name}`);
              } catch (e) { toast(e.message); }
            };
            body.appendChild(card);
          }
        }

        if (!canNest) {
          body.appendChild(el('div', 'm-sh-note', `This chat is already ${depthNow} deep. Open the main chat to start another subagent.`));
        } else if (!eps.length) {
          body.appendChild(el('div', 'm-sh-note', 'Connect at least one model in Settings > Connections before starting a subagent.'));
        } else {
          body.appendChild(el('div', 'm-sh-grp', 'Start a subagent'));
          const form = el('div', 'm-sub-form');
          const orderedEps = [...eps].sort((a, b) => modelScore(b) - modelScore(a));
          form.innerHTML = '<label class="m-sub-lbl">Name (optional)</label>' +
            '<input class="m-sub-inp" data-name type="text" placeholder="e.g. Reviewer, Researcher, Quick check" maxlength="60">' +
            '<label class="m-sub-lbl">Model</label>' +
            `<select class="m-sub-sel" data-model>${orderedEps.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join('')}</select>` +
            '<label class="m-sub-lbl">Role (optional)</label>' +
            '<input class="m-sub-inp" data-inst type="text" placeholder="Optional focus or instructions" maxlength="500">' +
            '<div class="m-sub-checks">' +
            '<label><input type="checkbox" data-fork checked> Include main chat context</label>' +
            '<label><input type="checkbox" data-save> Save as named preset</label>' +
            '</div>' +
            '<button class="primary m-sub-add" type="button" data-add>Open subagent chat</button>';
          const addBtn = form.querySelector('[data-add]');
          addBtn.onclick = async () => {
            const name = form.querySelector('[data-name]').value.trim();
            const model = form.querySelector('[data-model]').value;
            const instructions = form.querySelector('[data-inst]').value.trim();
            const fork_context = form.querySelector('[data-fork]').checked;
            const savePreset = form.querySelector('[data-save]').checked;
            if (!model) return toast('Pick a model for the subagent');
            if (savePreset && !name) return toast('Enter a name to save this preset');
            addBtn.disabled = true;
            try {
              if (savePreset && name) {
                const pr = await api('/api/subagents/presets', { name, model, instructions });
                if (pr.config) S.cfg = pr.config;
              }
              closeSheet();
              await KCH.spawnSubagent({ parent: targetParent, name, model, instructions, fork_context });
            } catch (e) {
              toast(e.message);
              addBtn.disabled = false;
            }
          };
          body.appendChild(form);
        }
      };
      render();
      if (targetParent) {
        api(`/api/chats/${encodeURIComponent(targetParent)}/subagents`).then((r) => {
          chatSubs = r.subagents || [];
          if (sheetOpen() && sheetGen === myGen && bodyRef?.isConnected) {
            render();
            syncPills();
          }
        }).catch(() => {});
      }
    });
    myGen = sheetGen;
    onSheetStatuses = () => { if (sheetOpen() && sheetGen === myGen) syncPills(); };
    refreshStatuses(false).finally(() => {
      statusChecking = false;
      syncPills();
    });
  }
  KCH.openSubagentsSheet = openSubagentsSheet;

  function openReportMainSheet() {
    if (!S.meta?.sub_of) return;
    const pBusy = !!(S.parentInfo?.busy || S.busy.has(S.meta.sub_of));
    const pTitle = S.parentInfo?.title || 'Main chat';
    const defTxt = KCH.latestSubagentReply?.() || '';
    openSheet('Report to main chat', (body) => {
      body.appendChild(el('div', 'm-sh-note', `Main chat: ${pTitle}${pBusy ? ' (currently working)' : ''}`));
      const box = el('div', 'm-sub-form');
      box.innerHTML = '<label class="m-sub-lbl">Findings to send</label>' +
        `<textarea class="m-rep-ta" rows="5" placeholder="Summary of findings for the main chat...">${esc(defTxt)}</textarea>` +
        `<button class="primary m-sub-add" type="button" data-send>${pBusy ? 'Steer main chat now' : 'Send to main model'}</button>` +
        '<button class="ghost m-sub-add m-sub-quiet" type="button" data-quiet>Hand off quietly (for next turn)</button>';
      const ta = box.querySelector('.m-rep-ta');
      const go = async (wake, btn) => {
        const txt = ta.value.trim();
        if (!txt) return toast('Write a summary or wait for a reply first');
        btn.disabled = true;
        try {
          const r = await api(`/api/chats/${encodeURIComponent(S.chat)}/report-main`, { summary: txt, wake_main: wake });
          closeSheet();
          toast(r.message || 'Sent to main chat');
        } catch (e) {
          toast(e.message);
          btn.disabled = false;
        }
      };
      const bSend = box.querySelector('[data-send]');
      const bQuiet = box.querySelector('[data-quiet]');
      bSend.onclick = () => go(true, bSend);
      bQuiet.onclick = () => go(false, bQuiet);
      body.appendChild(box);
    });
  }
  KCH.openReportMainSheet = openReportMainSheet;

  /* ---------------------------------------------------------------- Desktop "See my screen" quick capture */
  const seeScreenBtn = el('button', 'iconbtn');
  seeScreenBtn.id = 'seeScreenBtn';
  seeScreenBtn.type = 'button';
  seeScreenBtn.title = 'See my screen: capture desktop screenshot and attach';
  seeScreenBtn.setAttribute('aria-label', 'See my screen');
  seeScreenBtn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1.8" y="2.8" width="12.4" height="8.6" rx="1.6"/><path d="M5.5 13.4h5M8 11.4v2"/><circle cx="8" cy="7.1" r="1.6"/></svg>';
  $('attachBtn')?.after(seeScreenBtn);

  async function captureAndAttachScreen(silent = false) {
    seeScreenBtn.classList.add('busy');
    try {
      const r = await api('/api/screen/capture', { chat: S.chat || '', display: 0 });
      if (r && r.ok && r.path) {
        if (!S.chips.includes(r.path)) {
          S.chips.push(r.path);
          KCH.paintChips?.();
        }
        if (!silent) toast('Desktop screen captured', 1500);
        return r.path;
      }
    } catch (e) {
      if (!silent) toast(e.message || 'Could not capture screen');
    } finally {
      seeScreenBtn.classList.remove('busy');
    }
    return null;
  }
  seeScreenBtn.onclick = () => captureAndAttachScreen(false);

  const SCREEN_ASK_RE = /\b(?:see|look at|check|read|what(?:'s| is) on|capture|screenshot of)\s+(?:my|the|this)\s+screen\b/i;

  KCH.beforeSend = async (text, files) => {
    if (!NARROW_WIN.matches && text && (!files || !files.length) && SCREEN_ASK_RE.test(text)) {
      const shot = await captureAndAttachScreen(true);
      if (shot && Array.isArray(files) && !files.includes(shot)) {
        files.push(shot);
      }
    }
    return true;
  };

  /* ---------------------------------------------------------------- Responsive narrow-window layout */
  function apply() {
    document.body.classList.toggle('phone', NARROW_WIN.matches);
    const ab = $('attachBtn');
    if (NARROW_WIN.matches) {
      if (!extras.isConnected) $('side').appendChild(extras);
      for (const id of moved) {
        const node = $(id);
        if (node && node.parentNode !== extras) { home[id] = [node.parentNode, node.nextSibling]; extras.appendChild(node); }
      }
      if (ab) { ab.innerHTML = ICONS.plus; ab.title = 'Reference a file'; }
    } else {
      for (const id of moved) {
        const node = $(id), h = home[id];
        if (node && h && node.parentNode === extras) h[0].insertBefore(node, h[1] && h[1].parentNode === h[0] ? h[1] : null);
      }
      extras.remove();
      if (ab) { ab.innerHTML = ICONS.clip; ab.title = 'Reference a file (or drag one in)'; }
    }
    KCH.applyLayout();
    KCH.autoSize?.();
    paintSectionsUI();
  }
  NARROW_WIN.addEventListener('change', apply);
  apply();

  extras.addEventListener('click', (e) => { if (e.target.closest('#setBtn, #projectBtn, #meetBtn') && NARROW_WIN.matches) KCH.setSide(false); });

  /* ---------------------------------------------------------------- Build-ID freshness watcher */
  let knownBuild = null;
  let pendingBuild = null;
  let dismissedBuild = null;
  let updatePending = false;
  updBar.querySelector('[data-dismiss]').onclick = () => {
    dismissedBuild = pendingBuild || knownBuild;
    updBar.hidden = true;
  };
  function isSafeToReload() {
    const draft = ($('input')?.value || '').trim().length > 0;
    const hasChips = (S.chips || []).length > 0;
    const hasQueue = (S.queue || []).some((q) => !q.readonly);
    const busy = S.busy.size > 0;
    const dlgOpen = !!document.querySelector('dialog[open]');
    const dismissed = !!(pendingBuild && dismissedBuild === pendingBuild);
    return !draft && !hasChips && !hasQueue && !busy && !dlgOpen && !dismissed;
  }
  async function checkBuild() {
    try {
      const r = await api('/api/build');
      const b = r && r.build;
      if (!b) return;
      if (!knownBuild) {
        knownBuild = S.build || b;
        if (b === knownBuild) return;
      }
      if (b !== knownBuild) {
        updatePending = true;
        pendingBuild = b;
        if (isSafeToReload()) safeReload();
        else if (dismissedBuild !== b) updBar.hidden = false;
      }
    } catch { /* server offline or restarting */ }
  }
  setTimeout(checkBuild, 1500);
  setInterval(checkBuild, 60_000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkBuild(); });
  window.addEventListener('focus', () => checkBuild());
  KCH.onReconnect = () => checkBuild();
  KCH.onResume = () => checkBuild();

  function restoreReloadDraft(chatId) {
    try {
      const raw = sessionStorage.getItem('kch.reloadDraft');
      if (!raw) return;
      const d = JSON.parse(raw);
      if (!d || !d.text) { sessionStorage.removeItem('kch.reloadDraft'); return; }
      if (d.chat && chatId && d.chat !== chatId) return;
      const inp = $('input');
      if (inp && !inp.value.trim()) {
        inp.value = d.text;
        KCH.autoSize?.();
      }
      sessionStorage.removeItem('kch.reloadDraft');
    } catch { /* ignore */ }
  }

  /* ---------------------------------------------------------------- openChat & list hooks */
  KCH.onOpenChat = (id, meta) => {
    const project = meta && meta.project;
    if (id && project && project !== S.project) {
      S.project = project;
      LS.set('project', project);
      LS.set('chat:' + project, id);
      $('projectName').textContent = KCH.base(project);
      window.KCF?.setRoot(project);
      KCH.paintSub();
    }
    if (!meta?.sub_of && !meta?.group_member_of) recordRecent(id);
    restoreReloadDraft(id);
    paintSyncBar(meta);
    paintArchBar(meta);
    titleChev.hidden = !meta;
    mForkBtn.hidden = !meta;
    if (updatePending && isSafeToReload()) safeReload();
  };
  KCH.onListPainted = () => paintSectionsUI();
  KCH.onBusyChange = () => {
    paintSectionsUI();
    if (updatePending && isSafeToReload()) safeReload();
  };

  setTimeout(() => refreshStatuses(false), 400);
})();
