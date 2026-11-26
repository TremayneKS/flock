'use strict';
/* Live dictation in the composer. The mic streams 16 kHz audio to /api/dictation (Whisper on this computer) and the words
   appear at the cursor as they are heard: settled words in normal text, words that may still change in a lighter colour
   (an overlay that mirrors the textarea). Stopping finishes the last words at once; the accurate model's polish then
   replaces the dictated text if the user has not changed it. Another engine, off by default: the browser's own speech
   recognition (Chrome sends the audio to Google). */
(() => {
  const { $, api, toast } = KCH;
  const input = $('input');
  const btn = $('micBtn');
  const hint = $('dictState');
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

  const D = { active: false, stopping: false, kind: '', sid: '', seq: 0, queue: [], inflight: null, rev: -1,
    ctx: null, stream: null, node: null, rec: null, base: '', polishing: false, sendAfter: false, finished: null };
  // the dictated text in the composer: where it starts, the exact string placed there, and words given up to the user's edits
  const span = { start: 0, shown: '', tentAt: -1, tentLen: 0, skipWords: 0, detached: false };

  function engine() {
    const f = KCH.S.cfg?.features || {};
    const want = f.dictation_engine || '';
    if (want === 'off') return '';
    if (want === 'browser') return SR ? 'browser' : '';
    return f.dictation ? 'whisper' : '';
  }

  /* ---------------------------------------------------------------- the composer */
  const ghost = document.createElement('div');
  ghost.id = 'dictGhost';
  ghost.setAttribute('aria-hidden', 'true');
  input.parentElement.insertBefore(ghost, input.nextSibling);

  function beginSpan() {
    const v = input.value;
    const s = input.selectionStart ?? v.length, e = input.selectionEnd ?? v.length;
    input.value = v.slice(0, s) + v.slice(e);                // dictation replaces a selection, like typing would
    input.setSelectionRange(s, s);
    Object.assign(span, { start: s, shown: '', tentAt: -1, tentLen: 0, skipWords: 0, detached: false });
  }

  function locate() {
    const v = input.value;
    if (v.substr(span.start, span.shown.length) === span.shown) return span.start;
    if (!span.shown) return Math.min(span.start, v.length);
    const a = v.lastIndexOf(span.shown, span.start), b = v.indexOf(span.shown, span.start);   // text typed before it moved it
    if (a < 0) return b;
    if (b < 0) return a;
    return span.start - a <= b - span.start ? a : b;
  }

  // put settled + tentative text in the composer in place of what dictation put there last time
  function place(settled, tentative, final = false) {
    let at = locate();
    if (at < 0) {                    // the user edited inside the dictated words: those are theirs now; carry on at the caret
      span.skipWords += words(span.shown).length - (span.tentLen ? words(input.value.substr(span.tentAt, span.tentLen)).length : 0);
      span.detached = true;
      at = input.selectionStart ?? input.value.length;
      span.shown = '';
    }
    let text = (settled + (settled && tentative ? ' ' : '') + tentative).trim();
    const tentLen0 = tentative.trim().length;
    if (span.skipWords) {
      const w = words(text);
      text = w.slice(span.skipWords).join(' ');
    }
    const v = input.value;
    const before = v.slice(0, at), after = v.slice(at + span.shown.length);
    const lead = text && before && !/\s$/.test(before) ? ' ' : '';
    const trail = text && after && !/^\s/.test(after) ? ' ' : '';
    const shown = lead + text + trail;
    const caretAtEnd = document.activeElement === input && input.selectionStart === input.selectionEnd &&
      input.selectionStart === at + span.shown.length;
    const caretAfter = input.selectionStart > at + span.shown.length ? input.selectionStart - span.shown.length : null;
    input.value = before + shown + after;
    span.start = at;
    span.shown = shown;
    span.tentLen = final ? 0 : Math.min(tentLen0, text.length);
    span.tentAt = at + lead.length + text.length - span.tentLen;
    if (caretAtEnd || final) input.setSelectionRange(at + lead.length + text.length, at + lead.length + text.length);
    else if (caretAfter != null) input.setSelectionRange(caretAfter + shown.length, caretAfter + shown.length);
    KCH.autoSize();
    paintGhost();
  }
  const words = (s) => String(s || '').split(/\s+/).filter(Boolean);

  // the overlay: the same text, invisible except the words that may still change, drawn lighter over the textarea's own
  function paintGhost() {
    if (!D.active || !span.tentLen || input.value.substr(span.tentAt, span.tentLen) === '') { ghost.hidden = true; return; }
    const cs = getComputedStyle(input);
    for (const k of ['font', 'letterSpacing', 'wordSpacing', 'lineHeight', 'paddingTop', 'paddingRight', 'paddingBottom',
      'paddingLeft', 'textIndent', 'tabSize', 'textTransform']) ghost.style[k] = cs[k];
    ghost.style.left = input.offsetLeft + 'px';
    ghost.style.top = input.offsetTop + 'px';
    ghost.style.width = input.clientWidth + 'px';
    ghost.style.height = input.clientHeight + 'px';
    const v = input.value, a = span.tentAt, b = a + span.tentLen;
    ghost.replaceChildren(document.createTextNode(v.slice(0, a)), Object.assign(document.createElement('mark'), { textContent: v.slice(a, b) }),
      document.createTextNode(v.slice(b) + '​'));
    ghost.hidden = false;
    ghost.scrollTop = input.scrollTop;
  }
  input.addEventListener('scroll', () => { ghost.scrollTop = input.scrollTop; });
  input.addEventListener('input', () => { if (D.active) paintGhost(); });
  window.addEventListener('resize', () => { if (D.active) paintGhost(); });

  function say(text, kind = '') {
    hint.textContent = text;
    hint.className = kind;
    hint.hidden = !text;
  }
  function paintBtn() {
    btn.classList.toggle('rec', D.active && !D.stopping);
    btn.classList.toggle('load', D.active && D.loading);
    btn.title = D.active ? 'Stop dictation (Ctrl+Shift+Space)' : title();
  }
  function title() {
    return engine() === 'browser' ? "Speak (the browser's speech recognition: your audio goes to Google's servers) · Ctrl+Shift+Space"
      : 'Speak (Whisper on this computer) · Ctrl+Shift+Space';
  }

  /* ---------------------------------------------------------------- Whisper on this computer */
  function micError(e) {
    const n = e && e.name;
    if (n === 'NotAllowedError' || n === 'SecurityError') return 'Microphone access is blocked. Allow it for this app in the browser\'s site settings, then try again.';
    if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'No microphone found';
    if (n === 'NotReadableError' || n === 'AbortError') return 'The microphone is busy in another app';
    return 'Microphone: ' + ((e && e.message) || e);
  }

  async function startWhisper() {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) { toast(micError(e), 6000); return false; }
    let st;
    try { st = await api('/api/dictation/start', { chat: KCH.S.chat || '' }); } catch (e) { stream.getTracks().forEach((t) => t.stop()); toast(e.message, 6000); return false; }
    let ctx;
    try {
      try { ctx = new AudioContext({ sampleRate: 16000 }); } catch { ctx = new AudioContext(); }
      await ctx.audioWorklet.addModule('/static/dictation-worklet.js');
      const src = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, 'kch-pcm', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      const mute = ctx.createGain();
      mute.gain.value = 0;                       // keeps the node running; nothing is ever heard
      src.connect(node); node.connect(mute); mute.connect(ctx.destination);
      node.port.onmessage = (ev) => {
        if (!D.active) return;
        if (ev.data.pcm) D.queue.push(new Uint8Array(ev.data.pcm));
        if (ev.data.flushed) D.flushed?.();
        btn.style.setProperty('--lvl', Math.min(1, (ev.data.peak || 0) * 3).toFixed(2));
        if (D.queue.length >= 2 && !D.inflight) pump();
      };
      Object.assign(D, { ctx, node, stream });
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      try { ctx?.close(); } catch { /* closed */ }
      api(`/api/dictation/${st.id}/cancel`, {}).catch(() => {});
      toast('Could not start the audio: ' + e.message, 6000);
      return false;
    }
    Object.assign(D, { sid: st.id, seq: 0, queue: [], rev: -1 });
    stream.getAudioTracks()[0]?.addEventListener('ended', () => { if (D.active && !D.stopping) { toast('The microphone was disconnected'); stop(); } });
    apply(st);
    return true;
  }

  function pump() {
    if (D.inflight || !D.queue.length || !D.sid) return D.inflight;
    const parts = D.queue.splice(0);
    const blob = new Blob(parts, { type: 'application/octet-stream' });
    const seq = D.seq++;
    const sid = D.sid;
    D.inflight = api(`/api/dictation/${sid}/audio?seq=${seq}`, blob)
      .then((st) => { if (sid === D.sid) apply(st); })
      .catch((e) => {
        if (sid !== D.sid) return;
        if (e.status === 400 || e.status === 404) { toast('Dictation ended: ' + e.message, 6000); stop(); }
        else D.queue.unshift(...parts);         // the app was briefly busy: send it again with the next block
      })
      .finally(() => { D.inflight = null; if (D.queue.length >= 2 || (D.stopping && D.queue.length)) pump(); });
    return D.inflight;
  }

  function apply(st) {
    if (st.rev === D.rev && !['done', 'error'].includes(st.phase)) { paintPhase(st); return; }
    D.rev = st.rev;
    const fin = ['finishing', 'polishing', 'done', 'error'].includes(st.phase) && D.stopping;
    if (fin) place(((st.settled || '') + ' ' + (st.tentative || '')).trim(), '', true);
    else place(st.settled || '', st.tentative || '');
    paintPhase(st);
  }

  function paintPhase(st) {
    D.loading = st.phase === 'loading';
    paintBtn();
    if (st.phase === 'loading') say(st.message || 'Loading speech model... keep talking, nothing is lost', 'wait');
    else if (st.phase === 'live' && !D.stopping) say(st.message || 'Listening. Enter sends, Ctrl+Shift+Space stops', 'live');
    else if (st.phase === 'finishing') say('Finishing...', 'wait');
    else if (st.phase === 'polishing') say('Polishing the text...', 'wait');
    else if (st.phase === 'error') say('', '');
    if (st.phase === 'error' && st.message && !D.errShown) { D.errShown = true; toast(st.message, 8000); if (!D.stopping) stop(); }
  }

  async function stopWhisper() {
    if (D.node) {                                // the last part-block of audio, so the last word is not cut
      await new Promise((r) => { D.flushed = r; D.node.port.postMessage('flush'); setTimeout(r, 400); });
      D.flushed = null;
    }
    try { D.stream?.getTracks().forEach((t) => t.stop()); } catch { /* gone */ }
    try { await D.ctx?.close(); } catch { /* closed */ }
    Object.assign(D, { node: null, ctx: null, stream: null });
    const sid = D.sid;
    if (!sid) return;
    for (let i = 0; i < 20 && (D.inflight || D.queue.length); i++) { await (D.inflight || pump()); }
    let st;
    try { st = await api(`/api/dictation/${sid}/stop`, {}); } catch (e) { toast(e.message); return; }
    apply(st);
    D.finished = st;
    if (st.phase === 'polishing') polish(sid, st);
  }

  // the accurate pass: replace the dictated words with it if they are still exactly as placed (and not sent)
  async function polish(sid, st) {
    const placed = span.shown, at = span.start;
    D.polishing = true;
    try {
      for (let i = 0; i < 40 && !['done', 'error'].includes(st.phase); i++) st = await api(`/api/dictation/${sid}/poll?rev=${st.rev}`, {});
    } catch { st = null; }
    D.polishing = false;
    say('', '');
    if (!st || st.phase !== 'done' || span.detached || D.active) return;
    if (input.value.substr(at, placed.length) !== placed || span.start !== at) return;
    const text = (st.settled || '').trim();
    if (!text || text === placed.trim()) return;
    const sel = [input.selectionStart, input.selectionEnd];
    const lead = placed.match(/^\s*/)[0], trail = placed.match(/\s*$/)[0];
    const shown = lead + text + trail;
    input.value = input.value.slice(0, at) + shown + input.value.slice(at + placed.length);
    const d = shown.length - placed.length;
    const fix = (p) => (p >= at + placed.length ? p + d : p > at ? Math.min(p, at + shown.length) : p);
    if (document.activeElement === input) input.setSelectionRange(fix(sel[0]), fix(sel[1]));
    span.shown = shown;
    KCH.autoSize();
  }

  /* ---------------------------------------------------------------- the browser's speech recognition */
  function startBrowser() {
    const rec = new SR();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || 'en-GB';
    D.base = '';
    rec.onresult = (e) => {
      let fin = '', tent = '';
      for (let i = 0; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) fin += t; else tent += t;
      }
      D.lastFin = fin.trim();
      place((D.base + ' ' + fin).trim(), tent.trim());
      say("Listening (browser speech recognition). Ctrl+Shift+Space stops", 'live');
    };
    rec.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return;
      toast(e.error === 'not-allowed' ? 'Microphone access is blocked for this app' : e.error === 'network'
        ? "The browser's speech recognition needs the internet" : 'Speech recognition: ' + e.error, 6000);
      D.recFailed = true;
    };
    rec.onend = () => {
      if (D.active && !D.stopping && !D.recFailed) {             // Chrome ends a session after a silence: carry on
        D.base = (D.base + ' ' + (D.lastFin || '')).trim();
        D.lastFin = '';
        try { rec.start(); return; } catch { /* fall through */ }
      }
      D.recEnded?.();
    };
    try { rec.start(); } catch (e) { toast('Speech recognition: ' + e.message); return false; }
    D.rec = rec;
    D.recFailed = false;
    say("Listening (browser speech recognition). Ctrl+Shift+Space stops", 'live');
    return true;
  }
  async function stopBrowser() {
    const ended = new Promise((r) => { D.recEnded = r; setTimeout(r, 3000); });
    try { D.rec?.stop(); } catch { /* stopped */ }
    await ended;                               // Chrome turns the last words final before it ends
    D.rec = null;
  }

  /* ---------------------------------------------------------------- start / stop */
  async function start() {
    if (D.active) return;
    const kind = engine();
    if (!kind) return toast('Dictation is off (Settings > General)');
    Object.assign(D, { active: true, stopping: false, kind, errShown: false, finished: null, sid: '', loading: kind === 'whisper' });
    beginSpan();
    input.focus();
    paintBtn();
    if (kind === 'whisper') say('Starting the microphone...', 'wait');
    const ok = kind === 'whisper' ? await startWhisper() : startBrowser();
    if (!ok) { Object.assign(D, { active: false, loading: false }); paintBtn(); say('', ''); ghost.hidden = true; }
  }

  async function stop() {
    if (!D.active || D.stopping) return D.stopDone;
    D.stopping = true;
    paintBtn();
    ghost.hidden = true;
    D.stopDone = (async () => {
      try {
        if (D.kind === 'whisper') await stopWhisper(); else await stopBrowser();
      } finally {
        D.active = false;
        D.stopping = false;
        D.loading = false;
        btn.style.removeProperty('--lvl');
        ghost.hidden = true;
        paintBtn();
        if (!D.polishing) say('', '');
        const end = span.start + span.shown.replace(/\s+$/, '').length;
        if (!span.detached && input.value.substr(span.start, span.shown.length) === span.shown) { input.focus(); input.setSelectionRange(end, end); }
        D.sid = '';
      }
    })();
    return D.stopDone;
  }

  btn.onclick = () => (D.active ? stop() : start());
  let warmed = 0;
  btn.addEventListener('pointerenter', () => {          // the model loads while the hand moves to click
    if (engine() !== 'whisper' || D.active || Date.now() - warmed < 5 * 60 * 1000) return;
    warmed = Date.now();
    api('/api/dictation/warm', {}).catch(() => {});
  });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.code === 'Space') {
      if (btn.hidden) return;
      e.preventDefault();
      if (D.active) stop(); else start();
    } else if (e.key === 'Escape' && D.active && !$('dialog').open) {
      e.preventDefault();
      e.stopImmediatePropagation();
      stop();
    }
  }, true);
  window.addEventListener('pagehide', () => {
    if (D.sid) fetch(`/api/dictation/${D.sid}/cancel`, { method: 'POST', keepalive: true, headers: { 'X-KC-App': '1' } }).catch(() => {});
  });

  function onSend() {
    if (D.active && !D.stopping) stop().catch(() => {});
  }

  window.KCD = {
    engine, start, stop, onSend, title,
    active: () => D.active,
    async stopThenSend() {
      await stop();
      KCH.send({ fromDictation: true });
    },
  };
})();
