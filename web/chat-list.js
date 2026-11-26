'use strict';
/* The chat list. Groups (folders the user makes, nested as deep as wanted, kept in KCH_HOME/groups.json) come first,
   then the chats in no group, by day. A chat that came from another one (an imported fork or side session) sits under
   it. Move chats by dragging them onto a group (or onto the list below the groups to take them out of one), or with
   right-click > Move to group; Ctrl+click and Shift+click pick several. Drag a group onto another: its top or bottom
   edge puts it before or after, its middle puts it inside. Search looks inside groups too. */
(() => {
  const { S, $, LS, esc, el } = window.KCH;
  const api = (...a) => KCH.api(...a);
  const toast = (...a) => KCH.toast(...a);
  const sel = new Set();               // chats picked with Ctrl/Shift+click
  const openArchFolds = new Set();     // keep an opened Archived fold open across background SSE repaints
  let lastPick = null;
  let order = [];                      // chat ids in the order shown (Shift+click ranges)
  let dragging = null;                 // what is being dragged: {chats: [...]} or {group: id}

  const SVG = {
    chev: '<svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg>',
    folder: '<svg viewBox="0 0 16 16"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"/></svg>',
    plus: '<svg viewBox="0 0 16 16"><path d="M8 3.5v9M3.5 8h9"/></svg>',
    more: '<svg viewBox="0 0 16 16"><circle cx="3.5" cy="8" r=".9"/><circle cx="8" cy="8" r=".9"/><circle cx="12.5" cy="8" r=".9"/></svg>',
    branch: '<svg viewBox="0 0 16 16"><path d="M4.5 2.5v5a3.5 3.5 0 0 0 3.5 3.5h4.5M10 8.5l2.5 2.5-2.5 2.5"/></svg>',
  };
  const KIND = { fork: 'Forked from', side: 'Side session of', handoff: 'Handed off from' };
  const LIVE_SRC = ['Claude Code', 'Codex'];      // imported from sessions on this computer: they can be updated
  const same = (a, b) => String(a || '').replace(/\\/g, '/').replace(/\/$/, '').toLowerCase() === String(b || '').replace(/\\/g, '/').replace(/\/$/, '').toLowerCase();

  function branchText(c) {
    const b = c.branch;
    if (!b) return '';
    const by = b.by || 'another chat';
    let t = `${KIND[b.kind] || 'Came from'} ${b.kind === 'fork' && b.of ? b.of : by}`;
    if (b.kind === 'fork' && b.of && b.by && b.of !== b.by) t += ` (started by ${b.by})`;
    return t;
  }
  KCH.branchText = branchText;

  function shortWhen(ts) {
    const d = new Date(ts * 1000), now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(d.getFullYear() !== now.getFullYear() ? { year: '2-digit' } : {}) });
  }

  const recentFirst = () => LS.get('groupSort', 'recent') === 'recent';

  /* ---------------------------------------------------------------- painting */
  function paint() {
    const nav = $('chatList');
    const keep = nav.scrollTop;
    nav.innerHTML = '';
    order = [];
    for (const id of [...sel]) if (!S.chats.some((c) => c.id === id)) sel.delete(id);
    const q = S.filter.trim().toLowerCase();
    const isPhone = document.body.classList.contains('phone');
    const secFilter = !q ? (KCH.activeSection?.() || null) : null;
    const secNames = new Map(((S.sections && S.sections.sections) || []).map((s) => [s.id, s.name]));
    const groups = S.groups || [];
    const byId = new Map(groups.map((g) => [g.id, g]));
    const show = new Set();
    const lift = (id) => { while (id && byId.has(id) && !show.has(id)) { show.add(id); id = byId.get(id).parent; } };
    for (const g of groups) {
      if (secFilter && g.section && g.section !== secFilter) continue;
      if (isPhone || S.allChats || !g.project || same(g.project, S.project)) lift(g.id);
    }
    const inGroup = new Map(), loose = [], archivedList = [], archInGroup = new Map();
    for (const c of S.chats) {
      if (secFilter && c.section && c.section !== secFilter) continue;
      if (c.archived && !q) {
        archivedList.push(c);
        if (c.group && byId.has(c.group)) {
          archInGroup.set(c.group, (archInGroup.get(c.group) || 0) + 1);
        }
        continue;
      }
      if (c.group && byId.has(c.group)) { lift(c.group); (inGroup.get(c.group) || inGroup.set(c.group, []).get(c.group)).push(c); }
      else loose.push(c);
    }
    // folders newest first: by the latest message of any chat inside (subfolders included); "my own order" = drag order
    const latest = new Map();
    const newest = (gid, seen = new Set()) => {
      if (latest.has(gid)) return latest.get(gid);
      if (seen.has(gid)) return 0;
      seen.add(gid);
      let t = Math.max(0, ...(inGroup.get(gid) || []).map((c) => c.updated || c.created || 0));
      for (const k of groups) if ((k.parent || null) === gid) t = Math.max(t, newest(k.id, seen));
      latest.set(gid, t);
      return t;
    };
    const byOrder = (a, b) => (a.order || 0) - (b.order || 0);
    const sortBy = recentFirst() ? (a, b) => (newest(b.id) - newest(a.id)) || byOrder(a, b) : byOrder;
    const subs = (pid) => groups.filter((g) => (g.parent || null) === pid && show.has(g.id) && (!secFilter || !g.section || g.section === secFilter)).sort(sortBy);
    const hit = (c) => !q || `${c.title || ''} ${c.raw_title || ''} ${c.source || ''} ${c.neon_name || ''} ${c.archived ? 'archived' : ''}`.toLowerCase().includes(q);
    const count = new Map(), hits = new Map(), archCount = new Map();
    const tally = (g) => {
      const own = inGroup.get(g.id) || [];
      let n = own.length, h = own.filter(hit).length, ac = archInGroup.get(g.id) || 0;
      for (const k of subs(g.id)) { const [a, b, c] = tally(k); n += a; h += b; ac += c; }
      if (q && g.name.toLowerCase().includes(q)) h = Math.max(h, 1);
      count.set(g.id, n); hits.set(g.id, h); archCount.set(g.id, ac);
      return [n, h, ac];
    };
    const tops = subs(null);
    tops.forEach(tally);

    if (sel.size && !isPhone) nav.appendChild(selBar());
    const pickMode = !!KCH.phoneSelect?.active;
    const drawPickMark = (on) => `<span class="m-chk${on ? ' on' : ''}" aria-hidden="true"></span>`;
    let drawnGroups = 0;
    if (tops.length) {
      const box = el('div', 'groups');
      const drawGroup = (g, depth, into) => {
        if (q && !hits.get(g.id)) return;
        // Hide groups whose chats were all archived (keep genuinely fresh empty groups with 0 archived chats)
        if (!q && (count.get(g.id) || 0) === 0 && (archCount.get(g.id) || 0) > 0) return;
        drawnGroups++;
        const nameHit = q && g.name.toLowerCase().includes(q);
        const open = q ? true : !g.collapsed;
        const gPicked = pickMode && depth === 0 && KCH.phoneSelect.groups.has(g.id);
        const wrap = el('div', 'grp' + (open ? ' open' : '') + (gPicked ? ' m-picked' : ''));
        wrap.dataset.gid = g.id;
        const head = el('div', 'grp-head');
        head.style.setProperty('--d', depth);
        head.draggable = !isPhone;
        head.tabIndex = 0;
        const secTag = (q && g.section && secNames.get(g.section)) ? `<span class="m-stag">${esc(secNames.get(g.section))}</span>` : '';
        head.innerHTML = (pickMode && depth === 0 ? drawPickMark(gPicked) : '') +
          `<span class="chev">${SVG.chev}</span><span class="ico">${SVG.folder}</span><span class="t"></span>` +
          secTag + `<span class="n">${count.get(g.id) || ''}</span><span class="acts"><button data-a="new" title="New chat in this group">${SVG.plus}</button>` +
          `<button data-a="menu" title="Group options">${SVG.more}</button></span>`;
        head.querySelector('.t').textContent = g.name;
        head.title = g.name;
        let lpFired = false;
        head.addEventListener('click', (e) => {
          if (lpFired) { lpFired = false; return; }
          if (pickMode && depth === 0) { e.stopPropagation(); KCH.phoneSelect.toggleGroup(g.id); return; }
          const a = e.target.closest('[data-a]')?.dataset.a;
          if (a === 'new') return KCH.newChat({ group: g.id });
          if (a === 'menu') return isPhone && KCH.onPhoneLongPress ? KCH.onPhoneLongPress({ group: g }) : groupMenu(g, e);
          if (!q) collapse(g, !g.collapsed);
        });
        if (isPhone && depth === 0 && KCH.attachLongPress) {
          KCH.attachLongPress(head, () => { lpFired = true; KCH.onPhoneLongPress?.({ group: g }); });
        }
        head.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (!q) collapse(g, !g.collapsed); } });
        head.addEventListener('contextmenu', (e) => { e.preventDefault(); if (isPhone && KCH.onPhoneLongPress) KCH.onPhoneLongPress({ group: g }); else groupMenu(g, e); });
        if (!isPhone) groupDrag(head, wrap, g);
        wrap.appendChild(head);
        if (open) {
          const body = el('div', 'grp-body');
          for (const k of subs(g.id)) drawGroup(k, depth + 1, body);
          tree(inGroup.get(g.id) || [], depth + 1, body, { grouped: true, q: nameHit ? '' : q, isPhone, pickMode, secNames });
          if (!body.children.length) body.appendChild(el('div', 'grp-empty', q ? '' : 'Empty. Drag chats here, or + for a new one.'));
          wrap.appendChild(body);
        }
        into.appendChild(wrap);
      };
      for (const g of tops) drawGroup(g, 0, box);
      if (box.children.length) nav.appendChild(box);
    }
    const lz = el('div', 'loose');
    if (!isPhone) looseDrop(lz);
    tree(loose, 0, lz, { grouped: false, q, isPhone, pickMode, secNames });
    if (!lz.children.length) {
      const emptyMsg = q
        ? (nav.querySelector('.chat-item') ? '' : 'No chats match')
        : (drawnGroups ? '' : (secFilter ? `No active chats in ${secNames.get(secFilter) || 'this section'}` : 'No chats in this project yet'));
      if (emptyMsg) lz.appendChild(el('div', 'day', emptyMsg));
    }
    nav.appendChild(lz);

    // Closed "Archived (N)" fold at the bottom of each section (and on desktop when archived chats exist)
    if (!q && archivedList.length) {
      const archKey = isPhone ? ('sec:' + (secFilter || 'all')) : ('desk:' + (S.allChats ? 'all' : (S.project || '')));
      const fold = el('details', 'arch-fold');
      fold.innerHTML = `<summary><span class="chev">${SVG.chev}</span><span>Archived</span><span class="arch-cnt">${archivedList.length}</span></summary><div class="arch-body"></div>`;
      const body = fold.querySelector('.arch-body');
      let built = false;
      const buildArchived = () => {
        if (built) return;
        built = true;
        const sorted = archivedList.slice().sort((a, b) => (b.updated || b.created || 0) - (a.updated || a.created || 0));
        for (const c of sorted) {
          const row = el('div', 'arch-row');
          const b = item(c, 0, 0, false, { grouped: true, q: '', isPhone, pickMode: false, secNames, inArchive: true });
          const res = el('button', 'arch-restore', 'Restore');
          res.type = 'button';
          res.title = 'Unarchive chat';
          res.onclick = (ev) => { ev.stopPropagation(); KCH.archiveChats?.([c.id], [], false); };
          row.append(b, res);
          body.appendChild(row);
        }
      };
      if (openArchFolds.has(archKey)) { fold.open = true; buildArchived(); }
      fold.addEventListener('toggle', () => {
        if (fold.open) { openArchFolds.add(archKey); buildArchived(); }
        else openArchFolds.delete(archKey);
      });
      nav.appendChild(fold);
    }
    nav.scrollTop = keep;
    KCH.onListPainted?.();
  }

  /* chats as a tree: a chat that came from another sits under it (oldest child first); roots newest first */
  function tree(list, depth, into, opt) {
    const ids = new Set(list.map((c) => c.id));
    const kids = new Map(), roots = [];
    for (const c of list) {
      if (c.parent && c.parent !== c.id && ids.has(c.parent)) (kids.get(c.parent) || kids.set(c.parent, []).get(c.parent)).push(c);
      else roots.push(c);
    }
    const q = opt.q;
    const memo = new Map();
    const hitDeep = (c, seen = new Set()) => {
      if (memo.has(c.id)) return memo.get(c.id);
      if (seen.has(c.id)) return false;
      seen.add(c.id);
      const v = !q || `${c.title || ''} ${c.raw_title || ''} ${c.source || ''} ${c.neon_name || ''} ${c.neon_can || ''} ${(c.neon_owns || []).join(' ')} ${c.archived ? 'archived' : ''}`.toLowerCase().includes(q)
        || (kids.get(c.id) || []).some((k) => hitDeep(k, seen));
      memo.set(c.id, v);
      return v;
    };
    const drawn = new Set();
    let lastDay = null;
    const draw = (c, d) => {
      if (drawn.has(c.id) || !hitDeep(c)) return;
      drawn.add(c.id);
      const ks = (kids.get(c.id) || []).slice().sort((a, b) => (a.created || 0) - (b.created || 0));
      const open = q ? true : !LS.get('fold:' + c.id, false);
      into.appendChild(item(c, d, ks.length, open, opt));
      if (open) for (const k of ks) draw(k, d + 1);
    };
    const all = roots.concat(list.filter((c) => !roots.includes(c)));      // a loop of links still shows every chat
    for (const c of all) {
      if (drawn.has(c.id) || !hitDeep(c)) continue;
      if (!opt.grouped) {
        const dl = c.neon ? 'Neon workers' : KCH.dayLabel(c.updated || c.created);
        if (dl !== lastDay) { into.appendChild(el('div', 'day', esc(dl))); lastDay = dl; }
      }
      draw(c, depth);
    }
  }

  function item(c, depth, nKids, open, opt) {
    const grouped = !!opt.grouped, isPhone = !!opt.isPhone, pickMode = !!opt.pickMode && !grouped;
    const picked = pickMode && KCH.phoneSelect?.chats.has(c.id);
    const b = el('button', 'chat-item' + (c.id === S.chat ? ' on' : '') + (sel.has(c.id) || picked ? ' sel' : '') + (depth ? ' nested' : '') + (c.neon ? ' neon' : ''));
    b.style.setProperty('--d', depth);
    b.dataset.id = c.id;
    b.draggable = !isPhone;
    order.push(c.id);
    const busy = S.busy.has(c.id);
    let lead = pickMode ? `<span class="m-chk${picked ? ' on' : ''}" aria-hidden="true"></span>` : '';
    if (nKids) lead += `<span class="kids${open ? ' open' : ''}" data-kids title="${open ? 'Hide' : 'Show'} the ${nKids} chat${nKids === 1 ? '' : 's'} that came from this one">${SVG.chev}</span>`;
    else if (c.parent && c.branch) lead += `<span class="br">${SVG.branch}</span>`;
    const ts = c.updated || c.created;
    const shortSrc = (s) => ({ 'Claude Code': 'Claude', 'Claude.ai': 'Claude', 'Codex': 'Codex', 'ChatGPT': 'ChatGPT' }[s] || s);
    const secTag = (opt.q && !grouped && c.section && opt.secNames?.get(c.section))
      ? `<span class="m-stag">${esc(opt.secNames.get(c.section))}</span>` : '';
    const standTag = (c.neon && c.neon_standing) ? '<span class="w src stand-tag">standing</span>' : '';
    const neonTag = c.neon ? `<span class="w src neon-tag">${busy ? 'running' : 'Neon'}</span>` : '';
    const groupTag = (c.mode === 'group') ? '<span class="w src group-tag">Group</span>' : '';
    const archTag = (c.archived && !opt.inArchive) ? '<span class="w src arch-tag">archived</span>' : '';
    // a working chat says how long its running step has run, or how long since its last step (nothing under a minute)
    const agoTxt = busy ? (KCH.actAgo?.(c.id) || '') : '';
    const bgTxt = busy ? '' : (KCH.bgLabel?.(c.id) || '');
    const live = busy ? (agoTxt ? `<span class="ago" title="${esc(KCH.actLabel?.(c.id) || '')}">${agoTxt}</span>` : '') + '<span class="live"></span>'
      : (bgTxt ? `<span class="ago bgw" title="${esc('Running in the background: ' + (KCH.actLabel?.(c.id) || ''))}">${esc(bgTxt)}</span>` : '');
    let right;
    if (c.neon) {
      right = archTag + standTag + neonTag + live;
    } else if (busy) {
      right = archTag + groupTag + live;
    } else if (c.mode === 'group') {
      right = archTag + groupTag + `<span class="w">${grouped ? shortWhen(ts) : new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>`;
    } else if (c.source && (isPhone || !grouped)) {
      right = archTag + `<span class="w src" title="Imported from ${esc(c.source)}">${esc(isPhone ? shortSrc(c.source) : c.source)}</span>`;
    } else {
      right = archTag + `<span class="w">${grouped ? shortWhen(ts) : new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>`;
    }
    if (c.neon && c.neon_can) {
      b.classList.add('has-can');
      b.innerHTML = `<div class="m-crow">${lead}<span class="t">${esc(c.title || 'New chat')}</span>${secTag}${right}</div>` +
        `<div class="m-can">${esc(c.neon_can)}</div>`;
    } else {
      b.innerHTML = lead + `<span class="t">${esc(c.title || 'New chat')}</span>` + (nKids && !open ? `<span class="nk">${nKids}</span>` : '') + secTag + right;
    }
    const bt = branchText(c);
    b.title = (c.title || '') + (bt ? `\n${bt}` : '');
    let lpFired = false;
    b.addEventListener('click', (e) => {
      if (lpFired) { lpFired = false; return; }
      if (e.target.closest('[data-kids]')) { e.stopPropagation(); LS.set('fold:' + c.id, open); paint(); return; }
      if (KCH.phoneSelect?.active) {
        e.stopPropagation();
        if (c.group) KCH.phoneSelect.toggleGroup(c.group);
        else KCH.phoneSelect.toggleChat(c.id);
        return;
      }
      if (e.ctrlKey || e.metaKey) { if (sel.has(c.id)) sel.delete(c.id); else sel.add(c.id); lastPick = c.id; paint(); return; }
      if (e.shiftKey && lastPick) {
        const a = order.indexOf(lastPick), z = order.indexOf(c.id);
        if (a >= 0 && z >= 0) for (const id of order.slice(Math.min(a, z), Math.max(a, z) + 1)) sel.add(id);
        paint();
        return;
      }
      lastPick = c.id;
      if (sel.size) { sel.clear(); }
      KCH.openChat(c.id);
    });
    if (isPhone && KCH.attachLongPress) {
      KCH.attachLongPress(b, () => { lpFired = true; KCH.onPhoneLongPress?.({ chat: c }); });
    }
    b.addEventListener('contextmenu', (e) => { e.preventDefault(); if (isPhone && KCH.onPhoneLongPress) KCH.onPhoneLongPress({ chat: c }); else chatMenu(c, e); });
    if (!isPhone) {
      b.addEventListener('dragstart', (e) => {
        const ids = sel.has(c.id) ? [...sel] : [c.id];
        dragging = { chats: ids };
        e.dataTransfer.setData('text/kch-chats', JSON.stringify(ids));
        e.dataTransfer.effectAllowed = 'move';
        document.body.classList.add('dragging-chats');
      });
      b.addEventListener('dragend', endDrag);
    }
    return b;
  }

  function selBar() {
    const bar = el('div', 'selbar');
    bar.innerHTML = `<span>${sel.size} selected</span><button class="link" data-a="move">Move to group</button><button class="link" data-a="clear">Clear</button>`;
    bar.querySelector('[data-a=clear]').onclick = () => { sel.clear(); paint(); };
    bar.querySelector('[data-a=move]').onclick = (e) => moveMenu([...sel], e);
    return bar;
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && sel.size && !$('dialog').open) { sel.clear(); paint(); } });

  /* ---------------------------------------------------------------- drag and drop */
  function endDrag() {
    dragging = null;
    document.body.classList.remove('dragging-chats');
    document.querySelectorAll('.drop-in,.drop-before,.drop-after,.drop-out').forEach((x) => x.classList.remove('drop-in', 'drop-before', 'drop-after', 'drop-out'));
  }
  const carries = (e, t) => [...(e.dataTransfer?.types || [])].includes(t);

  function groupDrag(head, wrap, g) {
    head.addEventListener('dragstart', (e) => {
      dragging = { group: g.id };
      e.dataTransfer.setData('text/kch-group', g.id);
      e.dataTransfer.effectAllowed = 'move';
      e.stopPropagation();
    });
    head.addEventListener('dragend', endDrag);
    const zone = (e) => {
      const r = head.getBoundingClientRect(), y = (e.clientY - r.top) / r.height;
      return y < 0.28 ? 'before' : y > 0.72 ? 'after' : 'in';
    };
    wrap.addEventListener('dragover', (e) => {
      const chats = carries(e, 'text/kch-chats'), grp = carries(e, 'text/kch-group');
      if (!chats && !grp) return;
      e.preventDefault();
      e.stopPropagation();
      document.querySelectorAll('.drop-in,.drop-before,.drop-after,.drop-out').forEach((x) => { if (x !== wrap) x.classList.remove('drop-in', 'drop-before', 'drop-after', 'drop-out'); });
      const z = grp ? (head.contains(e.target) ? zone(e) : 'in') : 'in';
      if (grp && dragging?.group === g.id) return;
      wrap.classList.toggle('drop-in', z === 'in');
      wrap.classList.toggle('drop-before', z === 'before');
      wrap.classList.toggle('drop-after', z === 'after');
    });
    wrap.addEventListener('dragleave', (e) => { if (!wrap.contains(e.relatedTarget)) wrap.classList.remove('drop-in', 'drop-before', 'drop-after'); });
    wrap.addEventListener('drop', (e) => {
      const raw = e.dataTransfer.getData('text/kch-chats'), gid = e.dataTransfer.getData('text/kch-group');
      if (!raw && !gid) return;
      e.preventDefault();
      e.stopPropagation();
      const z = wrap.classList.contains('drop-before') ? 'before' : wrap.classList.contains('drop-after') ? 'after' : 'in';
      endDrag();
      if (raw) return moveChats(JSON.parse(raw), g.id);
      if (gid === g.id) return;
      if (z === 'in') return updateGroup(gid, { parent: g.id }, `Moved into ${g.name}`);
      if (z === 'before') return updateGroup(gid, { before: g.id });
      const sib = (S.groups || []).filter((x) => (x.parent || null) === (g.parent || null) && x.id !== gid).sort((a, b) => (a.order || 0) - (b.order || 0));
      const next = sib[sib.findIndex((x) => x.id === g.id) + 1];
      return updateGroup(gid, next ? { before: next.id } : { parent: g.parent || null, before: null });
    });
  }

  function looseDrop(lz) {
    lz.addEventListener('dragover', (e) => {
      if (!carries(e, 'text/kch-chats') && !carries(e, 'text/kch-group')) return;
      e.preventDefault();
      lz.classList.add('drop-out');
    });
    lz.addEventListener('dragleave', (e) => { if (!lz.contains(e.relatedTarget)) lz.classList.remove('drop-out'); });
    lz.addEventListener('drop', (e) => {
      const raw = e.dataTransfer.getData('text/kch-chats'), gid = e.dataTransfer.getData('text/kch-group');
      if (!raw && !gid) return;
      e.preventDefault();
      endDrag();
      if (raw) return moveChats(JSON.parse(raw), null);
      return updateGroup(gid, { parent: null, before: null }, 'Moved to the top level');
    });
  }
  // the empty part of the list under everything also takes chats out of their group
  $('chatList').addEventListener('dragover', (e) => {
    if (e.target !== $('chatList') || !carries(e, 'text/kch-chats')) return;
    e.preventDefault();
  });
  $('chatList').addEventListener('drop', (e) => {
    if (e.target !== $('chatList')) return;
    const raw = e.dataTransfer.getData('text/kch-chats');
    if (!raw) return;
    e.preventDefault();
    endDrag();
    moveChats(JSON.parse(raw), null);
  });

  /* ---------------------------------------------------------------- changes */
  async function moveChats(ids, gid) {
    ids = [...new Set(ids)];
    // a chat that others came from takes them along, unless they were picked too
    const kids = (id) => S.chats.filter((c) => c.parent === id);
    const all = new Set(ids);
    const walk = (id) => { for (const k of kids(id)) if (!all.has(k.id)) { const c = S.chats.find((x) => x.id === id); if (k.group === c?.group) { all.add(k.id); walk(k.id); } } };
    ids.forEach(walk);
    const list = [...all].filter((id) => (S.chats.find((c) => c.id === id)?.group || null) !== (gid || null));
    if (!list.length) return;
    for (const c of S.chats) if (list.includes(c.id)) c.group = gid || null;
    sel.clear();
    if (gid) { const g = S.groups.find((x) => x.id === gid); if (g?.collapsed) { g.collapsed = false; api('/api/groups/update', { id: gid, collapsed: false }).catch(() => {}); } }
    paint();
    try {
      await api('/api/groups/move', { chats: list, group: gid || null });
      const g = S.groups.find((x) => x.id === gid);
      toast(`${list.length} chat${list.length === 1 ? '' : 's'} moved ${g ? 'to ' + g.name : 'out of the group'}`, 1800);
      if (S.chat && list.includes(S.chat)) KCH.paintSub?.();
    } catch (e) { toast(e.message); await KCH.loadChats(); }
  }

  async function collapse(g, on) {
    g.collapsed = on;
    paint();
    try { await api('/api/groups/update', { id: g.id, collapsed: on }); } catch (e) { toast(e.message); }
  }

  async function updateGroup(id, change, note) {
    try {
      const r = await api('/api/groups/update', { id, ...change });
      S.groups = r.groups;
      paint();
      if (note) toast(note, 1600);
    } catch (e) { toast(e.message); }
  }

  async function newGroup(opts = {}) {
    const name = window.prompt(opts.parent ? 'Name of the new subgroup' : 'Name of the new group', '');
    if (!name || !name.trim()) return null;
    try {
      const r = await api('/api/groups/create', { name: name.trim(), parent: opts.parent || null, project: S.project, chats: opts.chats || [] });
      S.groups = r.groups;
      if (opts.chats?.length) for (const c of S.chats) if (opts.chats.includes(c.id)) c.group = r.group.id;
      sel.clear();
      paint();
      return r.group;
    } catch (e) { toast(e.message); return null; }
  }
  KCH.newGroup = newGroup;

  /* ---------------------------------------------------------------- menus */
  function menuAt(e, nodes) {
    const x = e.clientX ?? e.target.getBoundingClientRect().left, y = e.clientY ?? e.target.getBoundingClientRect().bottom;
    KCH.popAt({ getBoundingClientRect: () => ({ left: x, right: x, top: y - 6, bottom: y - 6 }) }, nodes);
    const pop = $('popover'), r = pop.getBoundingClientRect();
    if (r.bottom > window.innerHeight - 8) pop.style.top = Math.max(8, window.innerHeight - r.height - 8) + 'px';
    if (r.right > window.innerWidth - 8) pop.style.left = Math.max(8, window.innerWidth - r.width - 8) + 'px';
  }
  function mi(label, fn, cls = '') {
    const b = el('button', 'item ' + cls);
    b.innerHTML = label;
    b.onclick = () => { KCH.closePop(); fn(); };
    return b;
  }
  function groupList(pick, opts = {}) {
    // every group, indented under its parent, for "Move to group"
    const out = [];
    const walk = (pid, d) => {
      for (const g of (S.groups || []).filter((x) => (x.parent || null) === pid).sort((a, b) => (a.order || 0) - (b.order || 0))) {
        if (opts.skip?.has(g.id)) continue;
        out.push(mi(`<span class="gi" style="--d:${d}">${SVG.folder}</span><span>${esc(g.name)}</span>`, () => pick(g), g.id === opts.current ? 'on' : ''));
        walk(g.id, d + 1);
      }
    };
    walk(null, 0);
    return out;
  }

  function moveMenu(ids, e) {
    const cur = new Set(ids.map((id) => S.chats.find((c) => c.id === id)?.group || null));
    const nodes = [el('div', 'lbl', ids.length > 1 ? `Move ${ids.length} chats to` : 'Move to group')];
    nodes.push(...groupList((g) => moveChats(ids, g.id), { current: cur.size === 1 ? [...cur][0] : null }));
    if ([...cur].some(Boolean)) nodes.push(mi('No group', () => moveChats(ids, null)));
    nodes.push(el('div', 'sep'), mi('New group…', () => newGroup({ chats: ids })));
    menuAt(e, nodes);
  }

  function chatMenu(c, e) {
    const ids = sel.has(c.id) ? [...sel] : [c.id];
    if (ids.length > 1) return moveMenu(ids, e);
    if (c.neon) {
      const nodes = [
        mi('Open', () => KCH.openChat(c.id)),
        mi('Fork', () => KCH.forkChat(c.id)),
        mi('Move to section…', () => KCH.openMoveSheet?.({ chat: c })),
        el('div', 'sep'),
        mi(c.archived ? 'Unarchive' : 'Archive', () => KCH.archiveChats?.([c.id], [], !c.archived)),
      ];
      return menuAt(e, nodes);
    }
    const nodes = [mi('Open', () => KCH.openChat(c.id)), mi('Fork', () => KCH.forkChat(c.id)), mi('Rename…', () => rename(c)),
      mi('Move to section…', () => KCH.openMoveSheet?.({ chat: c }))];
    if (c.parent && S.chats.some((x) => x.id === c.parent)) nodes.push(mi('Open the chat it came from', () => KCH.openChat(c.parent)));
    if (LIVE_SRC.includes(c.source)) nodes.push(mi('Update this chat', () => window.KCI?.update('chat', c.id)));
    nodes.push(el('div', 'sep'), el('div', 'lbl', 'Move to group'));
    nodes.push(...groupList((g) => moveChats([c.id], g.id), { current: c.group }));
    if (c.group) nodes.push(mi('No group', () => moveChats([c.id], null)));
    nodes.push(mi('New group…', () => newGroup({ chats: [c.id] })));
    nodes.push(el('div', 'sep'), mi(c.archived ? 'Unarchive' : 'Archive', () => KCH.archiveChats?.([c.id], [], !c.archived)));
    nodes.push(mi('Delete…', () => KCH.deleteChat(c.id), 'danger-item'));
    menuAt(e, nodes);
  }

  async function rename(c) {
    const name = window.prompt('Chat name', c.title || '');
    if (!name) return;
    await api(`/api/chats/${c.id}/meta`, { title: name });
    c.title = name;
    if (c.id === S.chat) $('chatTitle').textContent = name;
    paint();
  }

  function groupMenu(g, e) {
    const below = new Set([g.id]);
    const walk = (id) => (S.groups || []).filter((x) => x.parent === id).forEach((x) => { below.add(x.id); walk(x.id); });
    walk(g.id);
    // a group with chats imported from Claude Code or Codex sessions: bring them up to date (new messages, new forks)
    const srcs = new Set((S.chats || []).filter((c) => below.has(c.group) && LIVE_SRC.includes(c.source)).map((c) => c.source));
    if (/^(claude-code|codex):/.test(g.key || '')) srcs.add(g.key.startsWith('codex') ? 'Codex' : 'Claude Code');
    const nodes = [
      ...(srcs.size ? [mi(srcs.size === 1 ? `Update from ${esc([...srcs][0])}` : 'Update imported chats', () => window.KCI?.update('group', g.id)), el('div', 'sep')] : []),
      mi('New chat in this group', () => KCH.newChat({ group: g.id })),
      mi('New subgroup…', () => newGroup({ parent: g.id })),
      mi('Move to section…', () => KCH.openMoveSheet?.({ group: g })),
      mi('Rename…', async () => {
        const name = window.prompt('Group name', g.name);
        if (name && name.trim() && name.trim() !== g.name) updateGroup(g.id, { name: name.trim() });
      }),
      mi(g.collapsed ? 'Expand' : 'Collapse', () => collapse(g, !g.collapsed)),
      mi(recentFirst() ? 'Sort folders: my own order' : 'Sort folders: latest message first', () => {
        LS.set('groupSort', recentFirst() ? 'manual' : 'recent');
        paint();
      }),
      el('div', 'sep'), el('div', 'lbl', 'Put it inside'),
      ...groupList((t) => updateGroup(g.id, { parent: t.id }, `Moved into ${t.name}`), { skip: below }),
    ];
    if (g.parent) nodes.push(mi('Top level', () => updateGroup(g.id, { parent: null, before: null })));
    nodes.push(el('div', 'sep'), mi('Archive all in this group', () => KCH.archiveChats?.([], [g.id], true)));
    nodes.push(mi('Delete group…', async () => {
      const n = (S.chats || []).filter((c) => below.has(c.group)).length;
      if (!window.confirm(`Delete the group "${g.name}"? Its chats are kept: they move ${g.parent ? 'up to the group around it' : 'out of the group'}.` + (n ? ` (${n} here)` : ''))) return;
      try {
        const r = await api('/api/groups/delete', { id: g.id });
        S.groups = r.groups;
        await KCH.loadChats();
        toast(r.moved ? `Group deleted, ${r.moved} chat${r.moved === 1 ? '' : 's'} kept` : 'Group deleted', 2000);
      } catch (err) { toast(err.message); }
    }, 'danger-item'));
    menuAt(e, nodes);
  }

  /* the + by "Chats": a new group */
  $('newGroupBtn')?.addEventListener('click', () => newGroup());

  window.KCL = { paint };
})();
