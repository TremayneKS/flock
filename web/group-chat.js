'use strict';
/* Group Chat mode UI:
   - "Group" button in the sidebar to create a multi-model room (any configured model).
   - Ultra-compact single-line room header strip (~34px default) with a 1-tap collapsible drawer
     holding the pinned goal, budget/sub-agent metadata, and a single horizontal scrollable row
     of clickable chips (clearly separating the dedicated Shared Worker sub-agent from Council members).
   - Structured Board Post cards in the message feed with private trace link. */
(() => {
  const { S, $, esc, el } = window.KCH;
  const api = (...a) => KCH.api(...a);
  const toast = (...a) => KCH.toast(...a);

  const ICO = {
    group: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="5.2" cy="5.2" r="2.2"/><circle cx="11.2" cy="5.8" r="1.9"/><path d="M1.8 13c.2-2.2 1.6-3.4 3.4-3.4s3.2 1.2 3.4 3.4"/><path d="M9.4 12.8c.2-1.6 1.2-2.6 2.5-2.6 1.2 0 2.1.9 2.3 2.6"/></svg>',
    baton: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h10M9.5 4.5L13 8l-3.5 3.5"/></svg>',
    pause: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="3.2" width="2.8" height="9.6" rx=".7"/><rect x="9.2" y="3.2" width="2.8" height="9.6" rx=".7"/></svg>',
    play: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 3.2L12.5 8l-8 4.8z"/></svg>',
    stop: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1.4"/></svg>',
    gear: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2.2"/><path d="M8 2v1.6M8 12.4V14M2 8h1.6M12.4 8H14M3.8 3.8l1.1 1.1M11.1 11.1l1.1 1.1M12.2 3.8l-1.1 1.1M4.9 11.1l-1.1 1.1"/></svg>',
    chev: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 6l3.5 4 3.5-4"/></svg>',
    up: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 10l3.5-4 3.5 4"/></svg>',
    down: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 6l3.5 4 3.5-4"/></svg>',
    trace: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M3 8h7M3 11.5h9"/></svg>',
  };

  const MODE_LABELS = {
    off: { short: 'Relay', full: 'Off (Relay)', desc: 'Sub-agents locked off. Models take sequential turns in the workspace.' },
    shared_worker: { short: '1 Worker', full: '1 Shared Worker', desc: 'Council models talk and review freely while a separate Shared Worker runs tasks asynchronously in the background.' },
    open: { short: 'Open (3)', full: 'Open (max 3)', desc: 'Models take turns and may spawn up to 3 active sub-agents across the room.' },
  };

  const STATE_LABELS = {
    idle: ['Ready', 'idle'],
    running: ['Live', 'run'],
    paused: ['Paused', 'warn'],
    need_user: ['Needs you', 'need'],
    done: ['Done', 'ok'],
    stopped: ['Stopped', 'idle'],
  };

  let liveAct = null;     // { member, chat_id, name, role, what, elapsed_s, remaining_s, at }
  let lastNudgeAt = 0;    // second tap on Pass Baton within 12s forces immediate handoff
  const drawerOpen = {};  // room_id -> bool (collapsed by default!)

  function fmtTime(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`;
  }

  function shortName(name) {
    const s = String(name || '').trim();
    if (!s) return 'Model';
    // Keep names crisp in compact horizontal chips ("ChatGPT (GPT-6.2)" -> "GPT-6.2", "Claude Sonnet 5.5" -> "Claude Sonnet 5.5")
    const paren = s.match(/\(([^)]{2,24})\)/);
    if (paren && /^chatgpt\b/i.test(s)) return paren[1].trim();
    return s.length > 18 ? s.slice(0, 17).trimEnd() + '…' : s;
  }

  function modelFamily(epId, name) {
    const id = String(epId || '').trim();
    const ep = id ? (S.cfg?.endpoints || []).find((e) => e.id === id) : null;
    const txt = [id, name, ep?.name, ep?.model].filter(Boolean).join(' ');
    const kind = String(ep?.kind || '').toLowerCase();
    // Order matters: check Kimi before DeepSeek so multi-provider slugs map cleanly,
    // and check explicit model families before generic "openai" compatible endpoints.
    if (/\bkimi\b|moonshot|k3\.1|\bk2\b/i.test(txt)) return 'kimi';
    if (/deepseek|\borca\b/i.test(txt)) return 'deepseek';
    if (/claude|\bopus\b|\bsonnet\b|\bhaiku\b|\bfable\b/i.test(txt) || kind === 'claude-code') return 'claude';
    if (/gemini|\bneon\b|\bcarbon\b|\bhydrogen\b/i.test(txt) || kind === 'agy') return 'gemini';
    if (/openai|chatgpt|\bgpt\b|codex|\bsol\b|\bastra\b|\bo[134](?:-|\b)/i.test(txt) || kind === 'codex') return 'openai';
    return '';
  }

  function famClass(epId, name) {
    const f = modelFamily(epId, name);
    return f ? ` fam-${f}` : '';
  }

  /* ---------------------------------------------------------------- Sidebar "Group" button */
  function mountSidebarBtn() {
    if ($('newGroupChat')) return;
    const btn = el('button', 'ghost wide grp-new-btn');
    btn.id = 'newGroupChat';
    btn.type = 'button';
    btn.title = 'Start a multi-model Group Chat room';
    btn.innerHTML = `${ICO.group}<span>Group</span>`;
    btn.onclick = () => openGroupModal(null);
    const anchor = $('newAssistant') || $('newChat');
    if (anchor) anchor.after(btn);
  }
  mountSidebarBtn();

  /* ---------------------------------------------------------------- Compact Group Room Header Bar */
  const groupBar = el('div', 'grp-bar');
  groupBar.id = 'groupBar';
  groupBar.hidden = true;
  const headAnchor = $('subStrip') || $('chatHead');
  headAnchor?.after(groupBar);

  function isGroupMeta(meta) {
    return !!(meta && meta.mode === 'group' && meta.group_chat);
  }

  function summaryHtml(g) {
    const modeInfo = MODE_LABELS[g.subagent_mode] || MODE_LABELS.shared_worker;
    const budgetMin = Math.max(1, Math.round((g.turn_budget_s || 600) / 60));
    const wm = g.worker_member;
    const wBusy = !!(g.worker_busy && wm);
    const wElap = wBusy && g.worker_started ? fmtTime(Math.max(0, Date.now() / 1000 - g.worker_started)) : '';
    if (g.busy && g.council_waiting && wBusy) {
      const wCls = famClass(wm.endpoint, wm.name);
      const wAct = g.worker_what ? ` · ${g.worker_what}` : '';
      return `Waiting · <span class="grp-sum-who${wCls}">${esc(`W:${shortName(wm.name)}`)}</span>${esc(` ${wElap}${wAct}`)}`;
    }
    if (g.busy && g.active_started) {
      const elap = Math.max(0, Date.now() / 1000 - g.active_started);
      const rawWho = g.active_name || g.active_endpoint || 'Model';
      const who = shortName(rawWho);
      const fCls = famClass(g.active_endpoint, rawWho);
      const wTag = wBusy
        ? ` · <span class="grp-sum-who${famClass(wm.endpoint, wm.name)}">${esc(`W:${wElap || 'run'}`)}</span>`
        : '';
      const act = (liveAct?.what && liveAct.role !== 'worker') ? ` · ${liveAct.what}` : '';
      return `<span class="grp-sum-who${fCls}">${esc(who)}</span>${esc(` · ${fmtTime(elap)}/${budgetMin}m`)}${wTag}${esc(act)}`;
    }
    if (g.state === 'need_user' && g.state_reason) {
      return esc(g.state_reason);
    }
    return esc(`${modeInfo.short} · R${g.round || 0}/${g.max_rounds || 12}`);
  }

  function paintGroupBar() {
    const meta = S.meta;
    if (!isGroupMeta(meta)) {
      groupBar.hidden = true;
      const inp = $('input');
      if (inp && inp.dataset.grpPh) {
        inp.placeholder = inp.dataset.grpPh;
        delete inp.dataset.grpPh;
      }
      return;
    }
    const g = meta.group_chat;
    const expanded = !!drawerOpen[S.chat];
    groupBar.hidden = false;
    groupBar.classList.toggle('open', expanded);

    const inp = $('input');
    if (inp) {
      if (!inp.dataset.grpPh) inp.dataset.grpPh = inp.placeholder;
      inp.placeholder = g.busy ? 'Steer the group...' : 'Message the group...';
    }

    const isSignoff = (g.signoff_pending || []).length > 0 && g.busy;
    const [stLbl, stCls] = isSignoff
      ? ['Sign-off', 'ok']
      : (g.busy && g.council_waiting ? ['Waiting', 'warn'] : (STATE_LABELS[g.state] || ['Ready', 'idle']));
    const modeInfo = MODE_LABELS[g.subagent_mode] || MODE_LABELS.shared_worker;
    const budgetMin = Math.max(1, Math.round((g.turn_budget_s || 600) / 60));
    const nCouncil = (g.members || []).length;
    const hasWorker = g.subagent_mode === 'shared_worker' && g.worker_member;
    const toggleLbl = hasWorker ? `${nCouncil}+W` : `${nCouncil}`;
    const subChip = g.subagent_mode === 'off'
      ? 'Sub-agents: Off'
      : (g.subagent_mode === 'shared_worker'
        ? `Worker slot: ${g.active_subagents || 0}/1`
        : `Sub-agents: ${g.active_subagents || 0}/${g.subagent_limit || 3}`);

    groupBar.innerHTML =
      `<div class="grp-strip">` +
        `<span class="grp-st ${stCls}"><i class="grp-dot"></i>${esc(stLbl)}</span>` +
        `<button class="grp-sum" type="button" data-gact="toggle" title="Expand or collapse room details">${summaryHtml(g)}</button>` +
        `<div class="grp-acts">` +
          (g.busy
            ? `<button class="grp-btn" data-gact="baton" title="Wrap-up nudge (tap again within 12s to force pass)">${ICO.baton}<span class="lbl">Pass</span></button>` +
              `<button class="grp-btn" data-gact="pause" title="Pause after current turn">${ICO.pause}<span class="lbl">Pause</span></button>` +
              `<button class="grp-btn danger" data-gact="stop" title="Stop room">${ICO.stop}</button>`
            : (g.goal ? `<button class="grp-btn primary" data-gact="resume" title="Resume room">${ICO.play}<span class="lbl">Resume</span></button>` : '')) +
          `<button class="grp-btn icon" data-gact="settings" title="Room settings">${ICO.gear}</button>` +
          `<button class="grp-btn grp-tog${expanded ? ' open' : ''}" data-gact="toggle" aria-expanded="${expanded}" title="Show or hide models and goal"><span>${toggleLbl}</span>${ICO.chev}</button>` +
        `</div>` +
      `</div>` +
      `<div class="grp-drawer"${expanded ? '' : ' hidden'}>` +
        (g.goal ? `<div class="grp-goal"><b>Goal:</b> <span>${esc(g.goal)}</span></div>` : '') +
        `<div class="grp-dmeta">${esc(modeInfo.full)} · ${budgetMin}m/turn · R${g.round || 0}/${g.max_rounds || 12} · ${esc(subChip)}${(!g.busy && g.state_reason) ? ' · ' + esc(g.state_reason) : ''}</div>` +
        `<div class="grp-members"></div>` +
      `</div>`;

    if (expanded) {
      const mRow = groupBar.querySelector('.grp-members');
      if (hasWorker) {
        const w = g.worker_member;
        const wChip = el('button', 'grp-mchip worker' + famClass(w.endpoint, w.name) + (w.active ? ' active' : ''));
        wChip.type = 'button';
        wChip.innerHTML = `<i class="dot"></i><span class="mr">Worker</span><span class="mn">${esc(shortName(w.name))}</span>` +
          (w.turns ? `<span class="mt">${w.turns}</span>` : '');
        wChip.title = `Dedicated Shared Worker sub-agent: ${w.name} (separate persistent session). Tap to inspect trace.`;
        wChip.onclick = () => KCH.openChat?.(w.chat_id);
        mRow.appendChild(wChip);
        mRow.appendChild(el('span', 'grp-mdiv'));
      }
      for (const m of (g.members || [])) {
        const chip = el('button', 'grp-mchip' + famClass(m.endpoint, m.name) + (m.active ? ' active' : ''));
        chip.type = 'button';
        const roleTag = m.role === 'panel' ? 'Council' : '';
        chip.innerHTML = `<i class="dot"></i><span class="mn">${esc(shortName(m.name))}</span>` +
          (roleTag ? `<span class="mr">${roleTag}</span>` : '') +
          (m.turns ? `<span class="mt">${m.turns}</span>` : '');
        chip.title = `${m.name} (${roleTag || 'Relay'}). Tap to inspect private session and tool trace.`;
        chip.onclick = () => KCH.openChat?.(m.chat_id);
        mRow.appendChild(chip);
      }
    }

    groupBar.querySelectorAll('[data-gact]').forEach((b) => {
      b.onclick = () => runGroupAction(b.dataset.gact);
    });
  }

  async function runGroupAction(act) {
    if (!S.chat || !isGroupMeta(S.meta)) return;
    if (act === 'toggle') {
      drawerOpen[S.chat] = !drawerOpen[S.chat];
      paintGroupBar();
      return;
    }
    if (act === 'settings') {
      openGroupModal(S.meta.group_chat);
      return;
    }
    try {
      if (act === 'baton') {
        const now = Date.now();
        const force = now - lastNudgeAt < 12000;
        lastNudgeAt = force ? 0 : now;
        await api(`/api/chats/${encodeURIComponent(S.chat)}/group/action`, { action: 'pass_baton', force });
        toast(force ? 'Passing baton immediately...' : 'Wrap-up nudge sent (tap Pass again within 12s to force)', 2200);
      } else if (act === 'pause') {
        await api(`/api/chats/${encodeURIComponent(S.chat)}/group/action`, { action: 'pause' });
        toast('Pausing cleanly after this turn');
      } else if (act === 'resume') {
        const r = await api(`/api/chats/${encodeURIComponent(S.chat)}/group/action`, { action: 'resume' });
        if (r.state && S.meta) { S.meta.group_chat = r.state; S.busy.add(S.chat); paintGroupBar(); }
      } else if (act === 'stop') {
        await api(`/api/chats/${encodeURIComponent(S.chat)}/stop`, {});
        toast('Group room stopped');
      }
    } catch (e) {
      toast(e.message || 'Action failed');
    }
  }

  // Live 1-second timer tick on the single-line summary strip while active
  setInterval(() => {
    if (!groupBar.hidden && S.meta?.group_chat?.busy) {
      const sum = groupBar.querySelector('.grp-sum');
      if (sum) sum.innerHTML = summaryHtml(S.meta.group_chat);
    }
  }, 1000);

  /* ---------------------------------------------------------------- Live + persisted thinking & tool calls for Group turns */
  const liveTurns = {};   // member_chat_id -> { el, steps: {}, seenIds: Set, info }

  function stripHiddenTags(text) {
    return String(text || '')
      .replace(/^\s*\[WORKER\s*:\s*(?:run|steer|stop)\s*\].*$/gim, '')
      .replace(/\[ROOM\s*:\s*(?:done|need_user|need user|continue|wait)\s*\]/gi, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function appendTraceItems(turn, trace) {
    if (!Array.isArray(trace) || !trace.length || !KCH.flowNode) return;
    trace.forEach((p, idx) => {
      if (p.kind === 'thinking' && p.text && KCH.thinkBlock) {
        KCH.flowNode(turn, KCH.thinkBlock(p.text, false));
      } else if (p.kind === 'tool' && KCH.toolRow) {
        const rawArgs = String(p.arguments || '');
        const isJson = /^\s*[{[]/.test(rawArgs);
        const row = KCH.toolRow({
          id: p.id || `gtr-${idx}`,
          name: p.name || 'tool',
          arguments: isJson ? rawArgs : '',
          raw: isJson ? undefined : rawArgs,
        });
        if (!isJson && rawArgs) {
          const argEl = row.querySelector('.arg');
          if (argEl) argEl.textContent = rawArgs.split('\n')[0].slice(0, 160);
        }
        if (p.output !== undefined && p.output !== null) {
          row._res = { content: p.output, ok: p.ok !== false, ui: p.diff ? { diff: p.diff } : undefined };
          const st = row.querySelector('.st');
          if (st) st.className = 'st ' + (p.ok === false ? 'bad' : 'ok');
          if (p.ok === false) row.classList.add('failed');
        } else {
          const st = row.querySelector('.st');
          if (st) st.className = 'st run';
        }
        KCH.flowNode(turn, row);
      }
    });
  }

  function clearLiveTurn(memberChatId) {
    const lt = liveTurns[memberChatId];
    if (!lt) return;
    for (const L of Object.values(lt.steps || {})) {
      if (L.t) cancelAnimationFrame(L.t);
    }
    lt.el?.remove();
    delete liveTurns[memberChatId];
  }

  function clearAllLiveTurns() {
    for (const cid of Object.keys(liveTurns)) clearLiveTurn(cid);
  }

  function ensureLiveTurn(info) {
    const mcid = info.member_chat || info.member_endpoint || 'live';
    if (liveTurns[mcid]?.el?.isConnected) return liveTurns[mcid];
    const box = $('msgs');
    if (!box) return null;
    box.querySelector('.empty-chat')?.remove();

    const isWorker = info.member_role === 'worker';
    const roleTag = isWorker ? 'Worker' : '';
    const fCls = famClass(info.member_endpoint, info.member_name);
    const turn = el('div', 'turn grp-turn grp-live');
    turn.dataset.mcid = mcid;
    turn.innerHTML =
      `<div class="grp-by">` +
        `<span class="m-by${fCls}${isWorker ? ' worker' : ''}">${esc(info.member_name || info.member_endpoint || 'Model')}${roleTag ? ` <em class="grp-by-role">${roleTag}</em>` : ''}</span>` +
        `<span class="grp-live-tag">Live</span>` +
        (info.member_chat
          ? `<button class="link grp-trace-btn" type="button" title="Inspect private session and tool calls">${ICO.trace}<span>Trace</span></button>`
          : '') +
      `</div>`;
    const tb = turn.querySelector('.grp-trace-btn');
    if (tb && info.member_chat) tb.onclick = () => KCH.openChat?.(info.member_chat);

    const w = box.querySelector('.working');
    if (w) box.insertBefore(turn, w);
    else box.appendChild(turn);

    const lt = { el: turn, steps: {}, seenIds: new Set(), info };
    liveTurns[mcid] = lt;
    return lt;
  }

  function liveMemberStep(lt, stepId) {
    const k = stepId || '_';
    if (lt.steps[k]) return lt.steps[k];
    return (lt.steps[k] = { text: '', reasoning: '', think: null, prose: null, t: 0 });
  }

  function paintMemberLive(lt, L) {
    if (L.t) return;
    L.t = requestAnimationFrame(() => {
      L.t = 0;
      if (!lt.el.isConnected) return;
      if (L.reasoning && KCH.thinkBlock && KCH.flowNode && KCH.setThought) {
        if (!L.think) { L.think = KCH.thinkBlock('', true); KCH.flowNode(lt.el, L.think); }
        KCH.setThought(L.think, L.reasoning);
        const b = L.think.querySelector('.body');
        if (b) b.scrollTop = b.scrollHeight;
        const g = L.think.closest('.acts');
        if (g) KCH.paintGroup?.(g);
      }
      const cleanTxt = stripHiddenTags(L.text);
      if (cleanTxt && KCH.flowNode) {
        if (!L.prose) { L.prose = el('div', 'prose'); KCH.flowNode(lt.el, L.prose); }
        const html = KCH.md ? KCH.md(cleanTxt, { streaming: true }) : esc(cleanTxt);
        if (window.KCVis?.patchLive) window.KCVis.patchLive(L.prose, html);
        else L.prose.innerHTML = html;
        L.prose.lastElementChild?.classList.add('cursor');
      }
      KCH.scrollDown?.();
    });
  }

  function onGroupStream(ev) {
    const e = ev.ev || {};
    const lt = ensureLiveTurn(ev);
    if (!lt) return;
    const who = ev.member_name || ev.member_endpoint || 'Model';
    switch (e.type) {
      case 'step':
        liveMemberStep(lt, e.id);
        break;
      case 'reasoning': {
        const L = liveMemberStep(lt, e.id);
        L.reasoning += e.text || '';
        KCH.showWorking?.(true, `${who}: Thinking`);
        paintMemberLive(lt, L);
        break;
      }
      case 'delta': {
        const L = liveMemberStep(lt, e.id);
        L.text += e.text || '';
        paintMemberLive(lt, L);
        break;
      }
      case 'tool_draft':
        KCH.showWorking?.(true, `${who}: Writing ${e.name || 'a tool call'}`);
        break;
      case 'step_cancelled': {
        const L = lt.steps[e.id];
        if (L) {
          if (L.t) cancelAnimationFrame(L.t);
          KCH.unflow?.(L.think);
          KCH.unflow?.(L.prose);
          delete lt.steps[e.id];
        }
        break;
      }
      case 'assistant': {
        const msg = e.message || {};
        const L = lt.steps[e.temp];
        if (L) {
          if (L.t) cancelAnimationFrame(L.t);
          KCH.unflow?.(L.think);
          KCH.unflow?.(L.prose);
          delete lt.steps[e.temp];
        }
        if (msg.id) {
          if (lt.seenIds.has(msg.id)) break;
          lt.seenIds.add(msg.id);
        }
        if (msg.reasoning_content && KCH.thinkBlock && KCH.flowNode) {
          KCH.flowNode(lt.el, KCH.thinkBlock(msg.reasoning_content, false));
        }
        const cleanBody = stripHiddenTags(msg.content || '');
        if (cleanBody && KCH.flowNode) {
          const p = el('div', 'prose');
          p.innerHTML = KCH.md ? KCH.md(cleanBody) : esc(cleanBody);
          window.KCVis?.mountVisuals?.(p);
          KCH.flowNode(lt.el, p);
        }
        for (const c of (msg.tool_calls || [])) {
          if (KCH.toolRow && KCH.flowNode) {
            const row = KCH.toolRow(c);
            const st = row.querySelector('.st');
            if (st) st.className = 'st run';
            KCH.flowNode(lt.el, row);
          }
        }
        if ((msg.tool_calls || []).length) {
          KCH.showWorking?.(true, `${who}: Running ${msg.tool_calls.map((c) => c.name).join(', ')}`);
        }
        KCH.scrollDown?.();
        break;
      }
      case 'tool_start': {
        const row = KCH.findRow?.(lt.el, e.call?.id);
        if (row) {
          const st = row.querySelector('.st');
          if (st) st.className = 'st run';
          const g = row.closest('.acts');
          if (g) KCH.paintGroup?.(g);
        }
        KCH.showWorking?.(true, `${who}: Running ${e.call?.name || 'tool'}`);
        break;
      }
      case 'message':
        if (e.message?.role === 'tool') {
          KCH.finishTool?.(e.message, lt.el);
          KCH.scrollDown?.();
        }
        break;
      case 'waiting':
      case 'retry':
        KCH.showWorking?.(true, `${who}: ${e.text || 'Waiting'}`);
        break;
      default:
        break;
    }
  }

  /* ---------------------------------------------------------------- Normal chat message renderer for Group replies */
  function renderGroupPost(m) {
    // Replace any active live-streaming turn container for this member cleanly
    if (m.member_chat_id) clearLiveTurn(m.member_chat_id);

    const isWorker = m.member_role === 'worker';
    const roleTag = isWorker ? 'Worker' : '';
    const fCls = famClass(m.member_endpoint, m.member_name);
    const metaBits = [];
    if (m.secs != null) metaBits.push(fmtTime(m.secs));
    if (m.tool_count) metaBits.push(`${m.tool_count} tool${m.tool_count === 1 ? '' : 's'}`);

    // Render as a normal assistant turn in the chat feed (with native expandable thinking & tool calls inline!)
    const turn = el('div', 'turn grp-turn');
    turn.dataset.mid = m.id || '';
    turn.innerHTML =
      `<div class="grp-by">` +
        `<span class="m-by${fCls}${isWorker ? ' worker' : ''}">${esc(m.member_name || m.member_endpoint || 'Model')}${roleTag ? ` <em class="grp-by-role">${roleTag}</em>` : ''}</span>` +
        (metaBits.length ? `<span class="grp-by-meta">${esc(metaBits.join(' · '))}</span>` : '') +
        (m.member_chat_id
          ? `<button class="link grp-trace-btn" type="button" title="Inspect private session and tool calls">${ICO.trace}<span>Trace</span></button>`
          : '') +
      `</div>`;

    // Inline native foldable thinking & tool calls right above the prose (identical to normal chats!)
    if (Array.isArray(m.trace) && m.trace.length) {
      appendTraceItems(turn, m.trace);
    }

    const prose = el('div', 'prose');
    prose.innerHTML = window.KCH.md ? KCH.md(m.content || '') : esc(m.content || '');
    turn.appendChild(prose);

    if (m.files_changed?.length) {
      const fDiv = el('div', 'grp-files', m.files_changed.slice(0, 6).map((f) => `<code>${esc(String(f).split(/[\\/]/).pop())}</code>`).join(' '));
      turn.appendChild(fDiv);
    }

    const tb = turn.querySelector('.grp-trace-btn');
    if (tb && m.member_chat_id) {
      tb.onclick = () => KCH.openChat?.(m.member_chat_id);
    }
    window.KCVis?.mountVisuals?.(turn);
    return turn;
  }

  /* ---------------------------------------------------------------- Create / Edit Group Room modal */
  function openGroupModal(existing) {
    const eps = (S.cfg?.endpoints || []).slice();
    if (eps.length < 2) {
      toast('Add at least 2 model connections in Settings first');
      return;
    }
    const isEdit = !!existing;
    let selected = isEdit
      ? (existing.members || []).map((m) => m.endpoint).filter((id) => eps.some((e) => e.id === id))
      : eps.slice(0, Math.min(3, eps.length)).map((e) => e.id);
    if (selected.length < 2) selected = eps.slice(0, 2).map((e) => e.id);

    let subMode = existing?.subagent_mode || 'shared_worker';
    let workerId = (existing?.worker && eps.some((e) => e.id === existing.worker)) ? existing.worker : (selected[0] || eps[0].id);
    let budgetMin = Math.max(1, Math.round((existing?.turn_budget_s || 600) / 60));
    let maxRounds = existing?.max_rounds || 12;
    let roomTitle = isEdit ? (S.meta?.title || '') : '';

    const build = (body) => {
      body.classList.add('grp-modal-body');

      if (!isEdit) {
        const tGrp = el('div', 'grp-field');
        tGrp.innerHTML = '<label>Room name (optional)</label><input type="text" class="m-sub-inp" id="grpTitleInp" placeholder="Auto-named from your first goal">';
        const ti = tGrp.querySelector('input');
        ti.value = roomTitle;
        ti.oninput = () => { roomTitle = ti.value; };
        body.appendChild(tGrp);
      }

      // Mode 3-way switch
      const mGrp = el('div', 'grp-field');
      mGrp.innerHTML = '<label>Sub-agents & Coordination Mode</label><div class="grp-modes"></div><div class="grp-mode-desc"></div>';
      const modesEl = mGrp.querySelector('.grp-modes');
      const descEl = mGrp.querySelector('.grp-mode-desc');

      // Shared Worker sub-agent picker: lists ALL configured endpoints (separate from council!)
      const workerField = el('div', 'grp-field');
      workerField.innerHTML =
        '<label>Shared Worker sub-agent <small>(separate from council)</small></label>' +
        '<select class="m-sub-sel" id="grpWorkerSel"></select>' +
        '<small class="grp-hint">Runs in its own dedicated persistent sub-agent session, separate from Council models even when using the same model.</small>';
      const workerSel = workerField.querySelector('select');
      for (const ep of eps) {
        const opt = document.createElement('option');
        opt.value = ep.id;
        opt.textContent = ep.name || ep.id;
        if (ep.id === workerId) opt.selected = true;
        workerSel.appendChild(opt);
      }
      workerSel.onchange = () => { workerId = workerSel.value; };

      const pGrp = el('div', 'grp-field');
      pGrp.innerHTML = '<label id="grpPickLbl">Council models <small>(pick 2+; order sets turn order)</small></label><div class="grp-ep-list"></div>';
      const epList = pGrp.querySelector('.grp-ep-list');
      const pickLbl = pGrp.querySelector('#grpPickLbl');

      const paintModes = () => {
        modesEl.innerHTML = '';
        for (const id of ['off', 'shared_worker', 'open']) {
          const b = el('button', 'grp-mode-btn' + (subMode === id ? ' on' : ''), MODE_LABELS[id].full);
          b.type = 'button';
          b.onclick = () => { subMode = id; paintModes(); };
          modesEl.appendChild(b);
        }
        descEl.textContent = MODE_LABELS[subMode].desc;
        workerField.hidden = subMode !== 'shared_worker';
        pickLbl.innerHTML = subMode === 'shared_worker'
          ? 'Council models <small>(all selected stay on the Council; order sets review order)</small>'
          : 'Relay models <small>(pick 2+; order sets turn order)</small>';
      };

      // Turn budget slider + presets
      const bGrp = el('div', 'grp-field');
      bGrp.innerHTML = `<label>Soft turn budget per model: <b id="grpBudgetVal">${budgetMin} min</b></label>` +
        `<input type="range" id="grpBudgetRange" min="2" max="30" step="1" value="${budgetMin}">` +
        `<div class="grp-presets"></div>` +
        `<small class="grp-hint">Early finish yields immediately. At 80% of budget the active model gets a gentle wrap-up nudge.</small>`;
      const range = bGrp.querySelector('#grpBudgetRange');
      const bVal = bGrp.querySelector('#grpBudgetVal');
      const syncBudget = (v) => {
        budgetMin = Math.max(1, Math.min(60, Number(v) || 10));
        range.value = Math.min(30, Math.max(2, budgetMin));
        bVal.textContent = `${budgetMin} min`;
        bGrp.querySelectorAll('.grp-pre').forEach((p) => p.classList.toggle('on', Number(p.dataset.m) === budgetMin));
      };
      range.oninput = () => syncBudget(range.value);
      const preRow = bGrp.querySelector('.grp-presets');
      for (const mins of [2, 5, 10, 15, 20, 30]) {
        const pb = el('button', 'grp-pre' + (mins === budgetMin ? ' on' : ''), `${mins}m`);
        pb.type = 'button';
        pb.dataset.m = mins;
        pb.onclick = () => syncBudget(mins);
        preRow.appendChild(pb);
      }

      // Max rounds
      const rGrp = el('div', 'grp-field grp-inline');
      rGrp.innerHTML = `<label>Max rounds before auto-pause</label><input type="number" class="m-sub-inp grp-num" min="2" max="50" value="${maxRounds}">`;
      rGrp.querySelector('input').oninput = (e) => { maxRounds = Math.max(2, Math.min(50, Number(e.target.value) || 12)); };

      const paintEndpoints = () => {
        epList.innerHTML = '';
        const ordered = [
          ...selected.map((id) => eps.find((e) => e.id === id)).filter(Boolean),
          ...eps.filter((e) => !selected.includes(e.id)),
        ];
        ordered.forEach((ep) => {
          const idx = selected.indexOf(ep.id);
          const on = idx >= 0;
          const st = (S.statuses || {})[ep.id];
          const dotCls = st ? (st.ok ? 'ok' : 'bad') : '';
          const row = el('div', 'grp-ep-row' + (on ? ' on' : ''));
          const fCls = famClass(ep.id, ep.name).trim();
          row.innerHTML =
            `<label class="grp-ep-chk"><input type="checkbox"${on ? ' checked' : ''}>` +
            `<i class="ep-dot ${dotCls}"></i>` +
            `<span class="ep-txt"><b${fCls ? ` class="${fCls}"` : ''}>${esc(ep.name || ep.id)}</b><small>${esc(ep.model || ep.kind || '')}</small></span></label>` +
            (on ? `<span class="grp-ord"><span class="ord-n">#${idx + 1}</span>` +
              `<button type="button" class="ord-b" data-dir="-1" ${idx === 0 ? 'disabled' : ''}>${ICO.up}</button>` +
              `<button type="button" class="ord-b" data-dir="1" ${idx === selected.length - 1 ? 'disabled' : ''}>${ICO.down}</button></span>` : '');
          row.querySelector('input').onchange = (ev) => {
            if (ev.target.checked) {
              if (!selected.includes(ep.id)) selected.push(ep.id);
            } else {
              selected = selected.filter((x) => x !== ep.id);
            }
            paintEndpoints();
            syncSubmit();
          };
          row.querySelectorAll('.ord-b').forEach((ob) => {
            ob.onclick = () => {
              const d = Number(ob.dataset.dir);
              const ni = idx + d;
              if (ni < 0 || ni >= selected.length) return;
              [selected[idx], selected[ni]] = [selected[ni], selected[idx]];
              paintEndpoints();
            };
          });
          epList.appendChild(row);
        });
      };

      const foot = el('div', 'grp-foot');
      const submitBtn = el('button', 'primary grp-go', isEdit ? 'Save room settings' : 'Create Group Chat');
      submitBtn.type = 'button';
      const syncSubmit = () => {
        submitBtn.disabled = selected.length < 2;
        submitBtn.textContent = selected.length < 2
          ? 'Pick at least 2 models'
          : (isEdit ? `Save (${selected.length} models)` : `Create Group Chat (${selected.length} models)`);
      };
      submitBtn.onclick = async () => {
        if (selected.length < 2) return;
        submitBtn.disabled = true;
        try {
          if (isEdit) {
            const r = await api(`/api/chats/${encodeURIComponent(S.chat)}/group/action`, {
              action: 'config',
              members: selected,
              subagent_mode: subMode,
              worker: workerId,
              turn_budget_s: budgetMin * 60,
              max_rounds: maxRounds,
            });
            if (r.group && S.meta) S.meta.group_chat = r.group;
            KCH.closeSheet?.();
            paintGroupBar();
            toast('Group room settings updated');
          } else {
            const sec = KCH.activeSection?.() || null;
            const r = await api('/api/chats', {
              project: S.project,
              mode: 'group',
              title: roomTitle.trim(),
              section: sec,
              group_chat: {
                members: selected,
                subagent_mode: subMode,
                worker: workerId,
                turn_budget_s: budgetMin * 60,
                max_rounds: maxRounds,
              },
            });
            KCH.closeSheet?.();
            await KCH.loadChats?.();
            await KCH.openChat?.(r.id);
            if (KCH.NARROW?.matches) KCH.setSide?.(false);
            $('input')?.focus();
          }
        } catch (e) {
          toast(e.message || 'Could not save Group Chat');
          syncSubmit();
        }
      };
      foot.appendChild(submitBtn);

      body.append(mGrp, workerField, pGrp, bGrp, rGrp, foot);
      paintModes();
      paintEndpoints();
      syncSubmit();
    };

    if (KCH.openSheet) {
      KCH.openSheet(isEdit ? 'Group Room Settings' : 'New Group Chat', build);
    }
  }

  /* ---------------------------------------------------------------- SSE & openChat hooks */
  function onEvent(ev) {
    if (ev.type === 'group_stream' && ev.chat && ev.chat === S.chat) {
      onGroupStream(ev);
      return true;
    }
    if (ev.type === 'group_state' && ev.chat && ev.chat === S.chat && S.meta) {
      S.meta.group_chat = ev.group;
      if (ev.group?.busy) {
        S.busy.add(S.chat);
        const who = ev.group.active_name || ev.group.active_endpoint || 'Working';
        KCH.showWorking?.(true, who);
      } else {
        S.busy.delete(S.chat);
        KCH.showWorking?.(false);
        if (!ev.group?.worker_busy) clearAllLiveTurns();
      }
      paintGroupBar();
      KCH.paintList?.();
      return true;
    }
    if (ev.type === 'group_activity' && ev.chat && ev.chat === S.chat) {
      liveAct = { ...ev, at: Date.now() };
      const sum = groupBar.querySelector('.grp-sum');
      if (sum && S.meta?.group_chat) sum.innerHTML = summaryHtml(S.meta.group_chat);
      if (S.meta?.group_chat?.busy) {
        KCH.showWorking?.(true, `${ev.name || ev.member}: ${ev.what || 'Working'}`);
      }
      return true;
    }
    return false;
  }

  function onOpenChat(_id, meta, groupLive) {
    liveAct = null;
    lastNudgeAt = 0;
    clearAllLiveTurns();
    paintGroupBar();
    document.body.classList.toggle('in-group-chat', isGroupMeta(meta));
    if (isGroupMeta(meta) && Array.isArray(groupLive) && groupLive.length) {
      for (const snap of groupLive) {
        const lt = ensureLiveTurn(snap);
        if (!lt) continue;
        if (Array.isArray(snap.trace) && snap.trace.length) {
          appendTraceItems(lt.el, snap.trace);
        }
        const cleanTxt = stripHiddenTags(snap.text || '');
        if (cleanTxt && KCH.flowNode) {
          const p = el('div', 'prose');
          p.innerHTML = KCH.md ? KCH.md(cleanTxt) : esc(cleanTxt);
          window.KCVis?.mountVisuals?.(p);
          KCH.flowNode(lt.el, p);
        }
      }
      KCH.scrollDown?.();
    }
  }

  function emptyStateEl(meta) {
    const g = meta?.group_chat || {};
    const namesHtml = (g.members || [])
      .map((m) => `<span class="grp-ename${famClass(m.endpoint, m.name)}">${esc(m.name)}</span>`)
      .join(' · ');
    const modeInfo = MODE_LABELS[g.subagent_mode] || MODE_LABELS.shared_worker;
    const budgetMin = Math.max(1, Math.round((g.turn_budget_s || 600) / 60));
    const workerLine = (g.subagent_mode === 'shared_worker' && g.worker_member)
      ? `<div class="grp-empty-sub"><b>Shared Worker:</b> <span class="grp-ename${famClass(g.worker_member.endpoint, g.worker_member.name)}">${esc(g.worker_member.name)}</span> (separate session)</div>`
      : '';
    const d = el('div', 'empty-chat grp-empty',
      `<h2>Group Chat</h2>` +
      `<div><b>${g.subagent_mode === 'shared_worker' ? 'Council: ' : ''}</b>${namesHtml}</div>` +
      workerLine +
      `<div class="grp-empty-sub">${esc(modeInfo.full)} · ${budgetMin} min soft turn budget</div>` +
      `<div class="grp-empty-sub">Send a message below to start.</div>`);
    return d;
  }

  window.KCG = {
    openGroupModal,
    paintGroupBar,
    renderGroupPost,
    onEvent,
    onOpenChat,
    emptyStateEl,
    modelFamily,
  };
})();
