'use strict';
/* Computer use (harness/computer.py): the question a model asks before it may see the screen or use the mouse and
   keyboard, the "controlling your computer" banner with its Stop, Esc to take control back, and the switches in
   Settings > Tools. Events arrive through app.js (computer_ask, computer_ask_done, computer_session). */
(() => {
  const { S, $, esc, el, api, toast } = window.KCH;
  const asks = new Map();          // id -> ask
  const sessions = new Map();      // chat id -> {model, title}
  let shownChat = null;
  const baseTitle = document.title;

  function onEvent(ev) {
    if (ev.type === 'computer_ask') { asks.set(ev.id, ev); paint(); }
    else if (ev.type === 'computer_ask_done') { asks.delete(ev.id); paint(); }
    else if (ev.type === 'computer_session') {
      if (ev.active) sessions.set(ev.chat, { model: ev.model, title: ev.title });
      else {
        sessions.delete(ev.chat);
        if (ev.why === 'esc') toast('Esc pressed: the model no longer controls your computer');
      }
      paint();
    }
  }

  async function load() {
    try {
      const st = await api('/api/computer/state');
      asks.clear(); sessions.clear();
      for (const a of st.asks || []) asks.set(a.id, a);
      for (const s of st.sessions || []) sessions.set(s.chat, s);
      paint();
    } catch { /* the app is restarting */ }
  }

  /* ---- the question */
  const CHOICE = { once: 'Allow once', chat: 'Allow for this chat', allow: 'Allow', deny: 'Deny' };
  const HINT = {
    once: 'For this request only: it asks again next time',
    chat: 'Until you press Stop or Esc, for this chat',
    allow: 'Screenshots in this chat',
    deny: 'It gets told no and carries on without it',
  };
  function card(a) {
    const control = a.kind === 'control';
    const d = el('div', 'cu-ask' + (control ? ' control' : ''));
    d.dataset.id = a.id;
    d.innerHTML = `<div class="cu-ic">${control ? ICON.mouse : ICON.eye}</div><div class="cu-tx"><b>${esc(a.model)} ${control ? 'wants to control your mouse and keyboard' : 'wants to see your screen'}</b>` +
      `<span>${control ? `First step: ${esc(a.detail || '')}. While it works it can click and type in any app. Esc or Stop takes control back at once.`
        : 'Screenshots can show anything that is open, including private messages. Asked once per chat.'}</span></div>` +
      `<div class="cu-btns">${a.choices.map((c) => `<button data-c="${c}" title="${esc(HINT[c] || '')}" class="${c === 'deny' ? 'ghost' : c === 'once' || c === 'allow' ? 'primary' : 'ghost'}">${CHOICE[c] || c}</button>`).join('')}</div>`;
    d.querySelectorAll('[data-c]').forEach((b) => {
      b.onclick = async () => {
        d.querySelectorAll('button').forEach((x) => { x.disabled = true; });
        try { await api('/api/computer/answer', { id: a.id, choice: b.dataset.c }); } catch (e) { toast(e.message); }
        asks.delete(a.id); paint();
      };
    });
    return d;
  }

  function paintAsks() {
    let box = $('cuAsks');
    if (!box) {
      box = el('div', '');
      box.id = 'cuAsks';
      $('composer').before(box);
    }
    const mine = [...asks.values()].filter((a) => a.chat === S.chat);
    const ids = mine.map((a) => a.id).join(',');
    if (box.dataset.ids !== ids) {
      box.innerHTML = '';
      for (const a of mine) box.appendChild(card(a));
      box.dataset.ids = ids;
      if (mine.length) requestAnimationFrame(() => { const c = $('conv'); c.scrollTop = c.scrollHeight; });
    }
    box.hidden = !mine.length;
    // a question from another chat: say so, with a way there
    const other = [...asks.values()].find((a) => a.chat !== S.chat);
    let bar = $('cuOther');
    if (!other) { if (bar) bar.hidden = true; return; }
    if (!bar) {
      bar = el('div', '');
      bar.id = 'cuOther';
      document.body.appendChild(bar);
    }
    bar.hidden = false;
    bar.innerHTML = `<span>${esc(other.model)} in "${esc(other.title)}" ${other.kind === 'control' ? 'wants to control your mouse and keyboard' : 'wants to see your screen'}.</span><button class="primary">Open the chat</button>`;
    bar.querySelector('button').onclick = () => KCH.openChat?.(other.chat);
  }

  /* ---- the banner while a model has the mouse and keyboard */
  function paintBanner() {
    let b = $('cuBanner');
    const list = [...sessions.entries()];
    if (!list.length) {
      if (b) b.hidden = true;
      document.body.classList.remove('cu-on');
      document.title = baseTitle;
      return;
    }
    if (!b) {
      b = el('div', '');
      b.id = 'cuBanner';
      b.setAttribute('role', 'alert');
      document.body.prepend(b);
    }
    const [cid, s] = list[0];
    const where = cid === S.chat ? '' : ` in "${esc(s.title || 'another chat')}"`;
    b.innerHTML = `<span class="cu-dot"></span><span class="cu-msg"><b>${esc(s.model)} is controlling your computer</b>${where}` +
      `${list.length > 1 ? ` (and ${list.length - 1} more)` : ''}. Press Esc or Stop to take it back.</span>` +
      `<button class="cu-stop">Stop</button>`;
    b.querySelector('.cu-stop').onclick = stopAll;
    b.hidden = false;
    document.body.classList.add('cu-on');
    document.title = `Controlling your computer · ${baseTitle}`;
  }

  async function stopAll() {
    try { await api('/api/computer/stop', {}); } catch (e) { toast(e.message); }
  }

  function paint() { paintAsks(); paintBanner(); }

  // Esc anywhere in the window takes control back (app.js's own Esc also stops the open chat's turn)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sessions.size) { e.preventDefault(); stopAll(); }
  }, true);
  // the open chat changed: show its own question
  setInterval(() => { if (S.chat !== shownChat) { shownChat = S.chat; paintAsks(); } }, 300);

  const ICON = {
    mouse: '<svg viewBox="0 0 16 16"><rect x="4.5" y="1.8" width="7" height="12.4" rx="3.5"/><path d="M8 1.8v4"/></svg>',
    eye: '<svg viewBox="0 0 16 16"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>',
  };

  /* ---- Settings > Tools */
  async function settings(body) {
    let st;
    try { st = await api('/api/computer/state'); } catch { return; }
    const s = st.settings;
    const sec = el('div', 'cu-set');
    const row = el('div', 'imp', `<button class="switch ${s.enabled ? 'on' : ''}" ${st.support.ok ? '' : 'disabled'}></button><div class="txt"><b>Computer use (the Computer tool)</b>` +
      `<span>${st.support.ok ? `Every model can see the screen and, once you allow it, use the mouse and keyboard. New chats only. Esc or Stop takes control back. This computer: ${esc(st.support.label)}.`
        : esc(st.support.label)}</span></div>`);
    row.querySelector('.switch').onclick = async (e) => {
      const on = !e.currentTarget.classList.contains('on');
      e.currentTarget.classList.toggle('on', on);
      await api('/api/computer/settings', { enabled: on });
    };
    sec.appendChild(row);
    const mode = el('div', 'imp', `<div class="txt"><b>When a model wants the mouse and keyboard</b><span>Screenshots alone are asked once per chat either way.</span></div>` +
      `<select><option value="chat">Ask: allow once or for the whole chat</option><option value="ask">Always ask (allow once only)</option></select>`);
    const sel = mode.querySelector('select');
    sel.value = s.mode;
    sel.onchange = async () => { await api('/api/computer/settings', { mode: sel.value }); toast('Saved'); };
    sec.appendChild(mode);
    body.appendChild(sec);
  }

  window.KCC = { onEvent, settings, load };
  load();
})();
