'use strict';
/* Meetings front end. Four corners, the same shape all the time, and every line between them can be dragged:
   col1, top left: meetings, or the live transcript     col2, top right: the open document, or the live summary
   col4, bottom left: files and search, with a viewer   col3, bottom right: the assistant */

const $ = (id) => document.getElementById(id);
const SECTIONS = [
  ['asked', 'Asked of you'], ['points', 'Key points'], ['decisions', 'Decisions'], ['actions', 'Actions'],
  ['deadlines', 'Deadlines'], ['problems', 'Problems'], ['info', 'Extra info'], ['qa', 'You asked'],
];
const ENGINE_NAMES = { opus: 'Claude Opus 5.5', deepseek: 'DeepSeek' };
const engineName = (engine, label) => label || ENGINE_NAMES[engine] || 'Assistant';
const ICONS = {
  file: '<svg viewBox="0 0 16 16"><path d="M4 1.5h5L12.5 5v9.5h-8.5z"/><path d="M9 1.5V5h3.5"/></svg>',
  state: '<svg viewBox="0 0 16 16"><path d="M2.5 13.5h11M4.5 11V7M8 11V3.5M11.5 11V5.5"/></svg>',
  back: '<svg viewBox="0 0 16 16"><path d="M10 3L5 8l5 5"/></svg>',
  close: '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  plus: '<svg viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg>',
  folder: '<svg viewBox="0 0 16 16"><path d="M1.5 4h4.5l1.5 1.5h7v8h-13z"/></svg>',
  screen: '<svg viewBox="0 0 16 16"><rect x="1.5" y="2.5" width="13" height="9" rx="1"/><path d="M5.5 14h5M8 11.5V14"/></svg>',
};
/* What can be imported as a recorded meeting (the server reads anything ffmpeg can; this is just for dropped files). */
const RECORDING_EXT = ['mp4', 'm4v', 'mov', 'mkv', 'webm', 'avi', 'wmv', '3gp', 'mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac', 'opus', 'wma'];
const isRecording = (name) => RECORDING_EXT.includes((name.split('.').pop() || '').toLowerCase());

const S = {
  projects: [], project: null, signedIn: true, root: '',
  engine: 'opus', engineLabel: '', choice: 'opus', choices: [], status: { state: 'idle', text: '' }, banner: null,
  timeline: [], live: null, lines: [], people: {}, named: new Set(), knownPeople: [], preview: {},
  now: '', notes: [], fresh: new Set(), lastNotes: 0,
  meetings: [], files: { state: [], files: [], meetings: [] },
  selected: null, viewer: null, search: { q: '', results: null, busy: false }, helper: null, openMeetings: new Set(),
  summaries: {}, summaryEngine: {}, source: null, bubble: {},
};

/* ---- small helpers ---- */
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  for (const kid of kids.flat()) if (kid != null) node.append(kid);
  return node;
}
async function getJSON(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Something went wrong');
  return data;
}
async function post(url, body) {
  const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-KC-App': '1' }, body: JSON.stringify(body || {}) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { toast(data.error || 'That did not work'); throw new Error(data.error); }
  return data;
}
const act = (url, body = {}) => post(url, { project: S.project, ...body });
const q = (params) => new URLSearchParams(params).toString();
function toast(text) {
  const node = el('div', { class: 'error-toast' }, text);
  document.body.append(node);
  setTimeout(() => node.remove(), 4000);
}
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') { node.setAttribute('target', '_blank'); node.setAttribute('rel', 'noopener noreferrer'); }
});
const markdown = (text) => DOMPurify.sanitize(marked.parse(text || '', { breaks: false, gfm: true }));
/* The assistant marks the word that matters in a note with **double stars**; it shows in the accent colour. */
const rich = (text) => String(text || '').split(/\*\*(.+?)\*\*/g).map((part, i) => (i % 2 ? el('b', { class: 'key' }, part) : part.replace(/\*\*/g, '')));
const hhmm = (ts) => new Date(ts * 1000).toTimeString().slice(0, 5);
function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
function ago(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ago`;
}
function nearBottom(node) { return node.scrollHeight - node.scrollTop - node.clientHeight < 70; }
function niceWhen(when) {
  if (!when) return '';
  const d = new Date(when.replace(' ', 'T'));
  if (isNaN(d)) return when;
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) + ', ' + when.slice(11);
}
function highlight(root, query) {
  const terms = (query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return;
  const pattern = new RegExp(terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  let first = null;
  for (const node of nodes) {
    const text = node.nodeValue;
    pattern.lastIndex = 0;
    if (!pattern.test(text)) continue;
    pattern.lastIndex = 0;
    const out = document.createDocumentFragment();
    let at = 0, match;
    while ((match = pattern.exec(text))) {
      out.append(text.slice(at, match.index));
      const mark = el('mark', {}, match[0]);
      if (!first) { first = mark; mark.className = 'first'; }
      out.append(mark);
      at = match.index + match[0].length;
    }
    out.append(text.slice(at));
    node.replaceWith(out);
  }
  if (first) first.scrollIntoView({ block: 'center' });
}

/* ---- the four corners: every line drags anywhere, and the sizes are remembered, one set for meetings and one for reading afterwards ---- */
const LAYOUT_DEFAULT = { live: { x: 44, yl: 58, yr: 58 }, review: { x: 32, yl: 62, yr: 62 } };
const layout = { live: { ...LAYOUT_DEFAULT.live }, review: { ...LAYOUT_DEFAULT.review } };
try {
  const saved = JSON.parse(localStorage.getItem('kc.corners') || '{}');
  for (const mode of Object.keys(layout)) Object.assign(layout[mode], saved[mode]);
} catch { /* the defaults are fine */ }
function layoutMode() { return S.live ? 'live' : 'review'; }
function applyLayout() {
  const sizes = layout[layoutMode()];
  const root = document.documentElement.style;
  root.setProperty('--x', sizes.x + '%'); root.setProperty('--yl', sizes.yl + '%'); root.setProperty('--yr', sizes.yr + '%');
}
function saveLayout() {
  try { localStorage.setItem('kc.corners', JSON.stringify(layout)); } catch { /* not remembered */ }
}
$('grid').addEventListener('pointerdown', (start) => {
  const handle = start.target.closest?.('[data-drag]');
  if (!handle || start.button !== 0) return;
  start.preventDefault();
  handle.setPointerCapture(start.pointerId);
  handle.classList.add('drag');
  document.body.classList.add('resizing');
  const box = $('grid').getBoundingClientRect();
  const sizes = layout[layoutMode()];
  let parts = handle.dataset.drag.split(' ');
  // Where the lines make a clean cross, its middle moves all four corners at once.
  if (handle.classList.contains('node') && Math.abs(sizes.yl - sizes.yr) / 100 * box.height < 2) parts = ['x', 'yl', 'yr'];
  const clamp = (value, low) => Math.max(low, Math.min(100 - low, value));
  const minX = 150 / box.width * 100, minY = 40 / box.height * 100;   // a corner can shrink down to just its title
  const frames = document.querySelectorAll('iframe, embed');
  frames.forEach((f) => { f.style.pointerEvents = 'none'; });
  const move = (event) => {
    if (parts.includes('x')) sizes.x = clamp((event.clientX - box.left) / box.width * 100, minX);
    const y = clamp((event.clientY - box.top) / box.height * 100, minY);
    for (const [line, other] of [['yl', 'yr'], ['yr', 'yl']]) {
      if (!parts.includes(line)) continue;
      // Near the line on the other side, snap to it, so a clean cross is easy to get back.
      sizes[line] = !parts.includes(other) && Math.abs(y - sizes[other]) / 100 * box.height < 8 ? sizes[other] : y;
    }
    applyLayout();
  };
  const up = () => {
    handle.classList.remove('drag');
    document.body.classList.remove('resizing');
    frames.forEach((f) => { f.style.pointerEvents = ''; });
    handle.removeEventListener('pointermove', move);
    handle.removeEventListener('pointerup', up);
    handle.removeEventListener('pointercancel', up);
    saveLayout();
  };
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up);
  handle.addEventListener('pointercancel', up);
});
$('grid').addEventListener('dblclick', (event) => {   // double-clicking a line puts it back where it started
  const handle = event.target.closest?.('[data-drag]');
  if (!handle) return;
  const mode = layoutMode();
  let parts = handle.dataset.drag.split(' ');
  if (handle.classList.contains('node')) parts = ['x', 'yl', 'yr'];
  parts.forEach((part) => { layout[mode][part] = LAYOUT_DEFAULT[mode][part]; });
  applyLayout();
  saveLayout();
});

/* ---- top bar ---- */
$('themeBtn').addEventListener('click', () => window.kcToggleTheme());
function renderTop() {
  const live = S.live;
  applyLayout();
  document.body.classList.toggle('listening', !!(live && live.ready && !live.paused && !live.ending));
  $('projectName').textContent = S.project || 'Choose a project';
  $('projectBtn').disabled = !!live;
  $('projectBtn').title = live ? 'End the meeting to change project' : 'Choose a project';
  $('liveBar').hidden = !live;
  $('meetingTitle').hidden = !live;
  if (live) $('meetingTitle').textContent = live.title === 'Meeting' ? 'Untitled meeting' : live.title;
  $('startBtn').hidden = !!live || !S.project;
  $('importBtn').hidden = !!live || !S.project;
  if (live) {
    $('liveBar').classList.toggle('paused', !!live.paused);
    $('pauseBtn').textContent = live.paused ? 'Resume' : 'Pause';
    $('pauseBtn').disabled = !live.ready;
    $('endBtn').textContent = live.ending ? 'Finishing' : 'End meeting';
    $('endBtn').disabled = !!live.ending;
  }
  renderModelPick();
}
// Which model answers: the one picked in Flock (when it is connected), Claude Code's Opus, or DeepSeek.
function renderModelPick() {
  const pick = $('modelPick');
  const choices = S.choices || [];
  pick.hidden = choices.length < 2;          // with one model there is nothing to switch
  const key = choices.map((c) => c.id + c.label).join('|');
  if (pick.dataset.key !== key) { pick.replaceChildren(...choices.map((c) => el('option', { value: c.id }, c.label))); pick.dataset.key = key; }
  pick.value = S.choice;
  pick.title = `Answering: ${engineName(S.engine, S.engineLabel)}`;
}
function setEngine(ev) {
  if (ev.engine) S.engine = ev.engine;
  if (ev.label) S.engineLabel = ev.label;
  if (ev.choice) S.choice = ev.choice;
  if (ev.choices) S.choices = ev.choices;
  if (ev.deepseek != null) S.deepseek = ev.deepseek;
  if (ev.harness != null) S.harness = ev.harness;
}
setInterval(() => {
  if (S.live && S.live.ready && !S.live.paused && !S.offline) $('timer').textContent = clock(Date.now() / 1000 - S.live.started);
  renderStatusAge();
  const updated = document.querySelector('.updated[data-ts]');
  if (updated && +updated.dataset.ts) updated.textContent = 'Updated ' + ago(+updated.dataset.ts);
}, 1000);

function renderBanner() {
  const box = $('banner');
  let banner = S.banner;
  if (!banner && !S.signedIn && S.project && S.engine === 'opus') {
    banner = { text: S.claude ? "Claude Code isn't signed in on this computer. Run this in a terminal: claude auth login"
      : 'The assistant needs Claude Code, which is not installed on this computer. Install it from https://claude.com/claude-code, then run: claude auth login', actions: [{ id: 'check_signin', label: 'I have signed in' }, ...(S.harness ? [{ id: 'use_harness', label: 'Use the Flock model' }] : []), ...(S.deepseek ? [{ id: 'use_deepseek', label: 'Use DeepSeek for now' }] : [])] };
  }
  if (S.offline) banner = { text: 'Meetings is not answering. It carries on by itself as soon as the app is back.', actions: [], offline: true };
  box.hidden = !banner;
  box.replaceChildren();
  if (!banner) return;
  box.append(el('span', { class: 'msg' }, banner.text));
  banner.actions.forEach((action, i) => box.append(el('button', { class: i ? 'alt' : '', onclick: () => doAction(action.id) }, action.label)));
  if (!banner.actions.length && !banner.offline) box.append(el('button', { class: 'alt', onclick: () => doAction('dismiss') }, 'Dismiss'));
}
async function doAction(id) {
  if (id === 'check_signin') {
    const data = await getJSON('/api/projects?fresh=1');
    S.signedIn = data.signed_in;
    if (!S.signedIn) toast('Still signed out. Run: claude auth login');
    renderBanner();
    return;
  }
  await act('/api/action', { action: id });
}

/* ---- projects ---- */
function openPopover(anchor, build) {
  const pop = $('popover');
  pop.replaceChildren(...build());
  pop.hidden = false;
  const box = anchor.getBoundingClientRect();
  pop.style.top = box.bottom + 6 + 'px';
  const width = pop.offsetWidth;
  pop.style.left = Math.max(8, Math.min(box.left, window.innerWidth - width - 8)) + 'px';
  const close = (event) => {
    if (event.type === 'keydown' ? event.key === 'Escape' : !pop.contains(event.target) && !anchor.contains(event.target)) {
      pop.hidden = true;
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', close);
    }
  };
  setTimeout(() => { document.addEventListener('pointerdown', close); document.addEventListener('keydown', close); });
  return () => { pop.hidden = true; document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', close); };
}
$('projectBtn').addEventListener('click', async () => {
  const data = await getJSON('/api/projects');
  S.projects = data.projects;
  let close;
  close = openPopover($('projectBtn'), () => [
    el('div', { class: 'plist' }, S.projects.map((p) => el('button', { class: p.name === S.project ? 'on' : '', onclick: () => { close(); selectProject(p.name); } },
      p.name, el('span', { class: 's' }, `${p.meetings} meeting${p.meetings === 1 ? '' : 's'}${p.description ? ' · ' + p.description : ''}`)))),
    el('button', { class: 'ghost', style: 'width:100%', onclick: () => { close(); newProjectDialog(); } }, 'New project'),
  ]);
});
function newProjectDialog() {
  const dialog = $('dialog');
  const name = el('input', { maxlength: 60, placeholder: 'For example: Hospital lab pilot', required: true });
  const description = el('textarea', { placeholder: 'A sentence or two, so the assistant knows the context. You can add to this later.' });
  const submit = async (event) => {
    event.preventDefault();
    const result = await post('/api/projects', { name: name.value, description: description.value });
    dialog.close();
    await selectProject(result.name);
  };
  dialog.replaceChildren(el('form', { method: 'dialog', onsubmit: submit },
    el('h3', {}, 'New project'),
    el('p', { class: 'hint' }, 'A project has its own assistant, files, meetings and state. Its assistant remembers every meeting in it.'),
    el('label', { class: 'field' }, 'Name', name),
    el('label', { class: 'field' }, 'What is it about? (optional)', description),
    el('button', { class: 'primary', type: 'submit' }, 'Create project')));
  dialog.showModal();
  name.focus();
}
async function selectProject(name) {
  if (S.source) S.source.close();
  S.project = name;
  try { localStorage.setItem('kc.project', name); } catch { /* not remembered */ }
  S.viewer = null; S.search = { q: '', results: null, busy: false }; $('search').value = ''; S.selected = null;
  document.body.classList.remove('noproject');
  await loadSnapshot();
  connect();
}
async function loadSnapshot() {
  const snap = await getJSON('/api/snapshot?' + q({ project: S.project }));
  setEngine(snap); S.status = snap.status; S.banner = snap.banner; S.meetings = snap.meetings; S.files = snap.files;
  S.helper = snap.helper; S.knownPeople = snap.people || []; S.usage = snap.usage || {};
  S.lastNotes = snap.status.last_notes || 0;
  S.timeline = [...snap.chat.map((e) => ({ ...e, type: 'chat' })), ...snap.feed.map((e) => ({ ...e, type: 'feed' }))].sort((a, b) => a.ts - b.ts);
  setLive(snap.live);
  if (!S.selected && S.meetings.length) S.selected = { type: 'meeting', id: S.meetings[0].id, tab: 'summary' };
  if (!S.selected) S.selected = { type: 'state', tab: 'status' };
  renderAll();
}
function setLive(live) {
  S.live = live;
  if (live) {
    S.lines = live.lines || []; S.people = live.people || {}; S.named = new Set(live.named || []);
    S.now = live.now || ''; S.notes = live.notes || []; S.preview = {};
  }
}
function connect() {
  const source = new EventSource('/api/events?' + q({ project: S.project }));
  S.source = source;
  let opened = false;
  source.onopen = () => {
    if (opened || S.offline) loadSnapshot().catch(() => {});
    opened = true;
    if (S.offline) { S.offline = false; renderBanner(); renderTop(); }
  };
  source.onmessage = (message) => { try { onEvent(JSON.parse(message.data)); } catch (error) { console.error(error); } };
  // The app stopped or restarted. The browser keeps trying by itself; say so, rather than show a meeting that looks alive.
  source.onerror = () => {
    if (!S.offline) { S.offline = true; renderBanner(); renderTop(); }
    if (source.readyState === EventSource.CLOSED && S.source === source) setTimeout(() => { if (S.source === source) connect(); }, 3000);
  };
}

// Flock says which model is picked there (in the address, then whenever its picker changes). Projects that follow
// the harness answer with it from their next turn.
function tellModel(model) {
  if (!model || typeof model.id !== 'string' || !model.id) return;
  post('/api/harness/model', { id: model.id, name: typeof model.name === 'string' ? model.name : model.id }).catch(() => {});
}
{
  const params = new URLSearchParams(location.search);
  if (params.get('model')) tellModel({ id: params.get('model'), name: params.get('model_name') || params.get('model') });
}
// Flock asks for another project when the user switches project there while this window stays open.
window.addEventListener('message', async (event) => {
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(event.origin)) return;
  if (event.data && event.data.kcModel) { tellModel(event.data.kcModel); return; }
  const wanted = event.data && event.data.kcProject;
  if (typeof wanted !== 'string' || !wanted) return;
  try { S.projects = (await getJSON('/api/projects')).projects; } catch { return; }
  const found = S.projects.find((p) => p.name.toLowerCase() === wanted.toLowerCase());
  if (found && found.name !== S.project) await selectProject(found.name);
});

/* ---- events from the app ---- */
function onEvent(ev) {
  switch (ev.type) {
    case 'status': S.status = ev; S.engine = ev.engine || S.engine; if (ev.label) S.engineLabel = ev.label; if (ev.last_notes) S.lastNotes = ev.last_notes; renderStatus(); renderTop(); break;
    case 'engine': setEngine(ev); renderTop(); renderStatus(); renderBanner(); break;
    case 'banner': S.banner = ev.banner; renderBanner(); break;
    case 'chat': addTimeline({ ...ev.entry, type: 'chat' }); break;
    case 'feed': addTimeline({ ...ev, type: 'feed' }); break;
    case 'answer_start': startBubble(ev.id, ev.engine, ev.extra, ev.label); break;
    case 'answer_delta': growBubble(ev.id, ev.text); break;
    case 'answer_end': endBubble(ev.id); break;
    case 'lines': onLines(ev); break;
    case 'preview': onPreview(ev); break;
    case 'people': S.people = ev.people; S.named = new Set(ev.named); renderTranscript(); break;
    case 'level': setMeter('meterMic', ev.mic); setMeter('meterSys', ev.system); break;
    case 'notes': S.now = ev.now; S.notes = ev.notes; S.lastNotes = Date.now() / 1000; (ev.changed || []).forEach((id) => S.fresh.add(id)); renderNotes(); break;
    case 'title': if (S.live) { S.live.title = ev.title; renderTop(); if (!S.viewer && !S.search.q) renderCol4(); } break;
    case 'meeting': onMeeting(ev); break;
    case 'meeting_status': if (S.live) { Object.assign(S.live, ev); delete S.live.type; renderTop(); renderCol1(); } break;
    case 'meetings': onMeetings(ev); break;
    case 'state': if (S.selected && S.selected.type === 'state' && !S.live) renderCol2(); refreshViewer('State/'); break;
    case 'summary': onSummary(ev); break;
    case 'import': onImport(ev); break;
    case 'helper': S.helper = ev.helper; renderHelper(); break;
    case 'usage': S.usage = ev; renderStatus(); break;
  }
}
function selectedKey() {
  if (!S.selected || S.selected.type !== 'meeting') return '';
  const meeting = S.meetings.find((m) => m.id === S.selected.id);
  return meeting ? `${meeting.id}|${isWriting(meeting)}|${meeting.files.join()}` : 'gone';
}
function onMeetings(ev) {
  const before = selectedKey();
  S.meetings = ev.meetings; S.files = ev.files;
  if (!S.live) {
    if (S.selected?.type === 'meeting' && !S.meetings.some((m) => m.id === S.selected.id) && S.meetings.length) S.selected.id = S.meetings[0].id;
    renderCol1();
    if (selectedKey() !== before) renderCol2();
  }
  if (!S.viewer && !S.search.q) renderCol4();
}
function onMeeting(ev) {
  const wasLive = !!S.live;
  setLive(ev.live);
  if (ev.live && !wasLive) { S.fresh.clear(); }
  if (!ev.live && wasLive && ev.ended) { S.selected = { type: 'meeting', id: ev.ended, tab: 'summary' }; S.summaries[ev.ended] = 'writing'; }
  if (!ev.live && !ev.ended) S.selected = S.meetings.length ? S.selected : { type: 'state', tab: 'status' };
  renderAll();
}
function isWriting(meeting) {
  const known = S.summaries[meeting.id];
  return known ? known === 'writing' : meeting.summary_status === 'writing';
}
function onSummary(ev) {
  S.summaries[ev.folder] = ev.status;
  if (ev.engine) S.summaryEngine[ev.folder] = engineName(ev.engine, ev.label);
  if (ev.status === 'done') {
    if (S.live && S.live.folder === ev.folder) openViewer(`Meetings/${ev.folder}/summary.md`);
    if (S.selected && S.selected.type === 'meeting' && !S.live) {
      if (S.selected.tab === 'summary') { if (!S.meetings.some((m) => m.id === S.selected.id)) S.selected.id = ev.folder; }
    }
  }
  if (!S.live) { renderCol1(); renderCol2(); }
  else renderCol2Head();
}
function setMeter(id, level) {
  // Speech is quiet in raw numbers, so spread the low end out.
  const pct = Math.min(100, Math.pow(level * 8, 0.6) * 100);
  $(id).style.width = pct + '%';
}

/* ---- column 1: meetings, or the live transcript ---- */
function renderCol1() {
  const col = $('col1');
  if (S.live) return renderLiveTranscriptShell(col);
  const count = el('span', { class: 'count' }, String(S.meetings.length));
  const list = el('div', { class: 'scroll' });
  list.append(el('button', { class: 'rowbtn statebtn' + (S.selected && S.selected.type === 'state' ? ' on' : ''), html: ICONS.state, onclick: () => selectDoc({ type: 'state', tab: 'status' }) },
    el('span', {}, el('div', { class: 't' }, 'Project state'), el('div', { class: 's' }, 'Deadlines, meetings, problems, changes'))));
  if (!S.meetings.length) list.append(el('div', { class: 'empty' }, el('b', {}, 'No meetings yet'), 'Press Start meeting when your call begins, with the meeting audio playing through this PC. Or press Import recording for a meeting you already have a recording of.'));
  for (const meeting of S.meetings) {
    const writing = isWriting(meeting);
    const job = meeting.import;
    const failed = !job && !writing && !meeting.files.includes('summary.md');
    const tag = job ? (job.status === 'failed' ? 'import stopped' : 'importing') : writing ? 'writing summary' : failed ? 'no summary' : '';
    const details = job ? [niceWhen(meeting.when), importLine(job)].filter(Boolean).join(' · ')
      : [niceWhen(meeting.when), meeting.duration_s ? Math.max(1, Math.round(meeting.duration_s / 60)) + ' min' : '', meeting.people.filter((p) => p !== 'Me').join(', ')].filter(Boolean).join(' · ');
    list.append(el('button', { class: 'rowbtn' + (S.selected && S.selected.id === meeting.id ? ' on' : ''), onclick: () => selectDoc({ type: 'meeting', id: meeting.id, tab: 'summary' }) },
      el('div', { class: 't' }, meeting.title, tag ? el('span', { class: 'tag' }, tag) : null),
      el('div', { class: 's' }, details)));
  }
  col.replaceChildren(el('header', { class: 'phead' }, el('h2', {}, 'Meetings', count)), list);
}
function selectDoc(selection) {
  S.selected = selection;
  renderCol1();
  renderCol2();
}
function colourFor(pid) {
  return pid === 'me' ? 'var(--accent-text)' : 'var(--ink)';
}
function renderLiveTranscriptShell(col) {
  const live = S.live;
  const body = el('div', { class: 'scroll', id: 'transcriptScroll' }, el('div', { id: 'lines' }), el('div', { id: 'previewLine', class: 'preview', hidden: true }),
    el('button', { class: 'jump', id: 'jumpBtn', hidden: true, onclick: () => scrollTranscript(true) }, 'Back to latest'));
  const jump = body.querySelector('#jumpBtn');      // its own button: after the meeting this body is gone from the page
  body.addEventListener('scroll', () => { if (nearBottom(body)) jump.hidden = true; });
  col.replaceChildren(el('header', { class: 'phead' }, el('h2', {}, 'Live transcript')), body);
  renderTranscript();
  if (!live.ready && !live.lines?.length) {
    $('lines').before(el('div', { class: 'starting' }, el('span', { class: 'spin' }), live.ending ? 'Finishing the last lines' : live.loading === 'Interrupted' ? 'Not listening' : (live.loading || 'Starting') + '. This takes a few seconds.'));
  } else if (!live.ready && live.loading) {
    body.append(el('div', { class: 'starting' }, el('span', { class: 'spin' }), live.loading));
  } else if (live.ready && !S.lines.length) {
    $('lines').before(el('div', { class: 'empty', id: 'waitingHint' }, el('b', {}, 'Listening'), 'Words appear here a few seconds after someone speaks. Check the You and Them meters above are moving.'));
  }
}
function turnNode(line, withHeader) {
  const turn = el('div', { class: 'turn', 'data-pid': line.pid });
  if (withHeader) {
    const name = S.people[line.pid] || 'Someone';
    const button = el('button', { style: `--c:${colourFor(line.pid)}`, class: line.pid === 'me' || line.pid === 'other' ? 'fixed' : '', title: line.pid === 'me' ? 'This is you' : 'Click to name this person' }, name);
    if (line.pid !== 'me' && line.pid !== 'other') button.addEventListener('click', () => renameSpeaker(button, line.pid));
    turn.append(el('div', { class: 'who' }, button, el('time', {}, clock(line.start))));
  }
  turn.append(el('div', { class: 'say' }));
  return turn;
}
function renameSpeaker(button, pid) {
  const input = el('input', { value: S.people[pid] || '', list: 'peopleList', spellcheck: 'false' });
  const list = el('datalist', { id: 'peopleList' }, S.knownPeople.map((p) => el('option', { value: p })));
  button.replaceWith(input, list);
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (save && value && value !== S.people[pid]) act('/api/meeting/speaker', { pid, name: value });
    else renderTranscript();
  };
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') finish(true); if (event.key === 'Escape') finish(false); });
  input.addEventListener('blur', () => finish(true));
}
let renderedLines = [];
function renderTranscript() {
  const box = $('lines');
  if (!box) return;
  box.replaceChildren();
  renderedLines = [];
  let turn = null, last = null;
  for (const line of S.lines) {
    if (!turn || last !== line.pid) { turn = turnNode(line, true); box.append(turn); last = line.pid; }
    turn.querySelector('.say').append(line.text + ' ');
    renderedLines.push(line.seq);
  }
  scrollTranscript(false, true);
}
function appendLine(line) {
  const box = $('lines');
  if (!box) return;
  $('waitingHint')?.remove();
  const lastTurn = box.lastElementChild;
  let turn = lastTurn;
  if (!lastTurn || lastTurn.dataset.pid !== line.pid) { turn = turnNode(line, true); box.append(turn); }
  turn.querySelector('.say').append(el('span', { class: 'fresh' }, line.text + ' '));
  renderedLines.push(line.seq);
}
function scrollTranscript(force, quiet) {
  const body = $('transcriptScroll');
  if (!body) return;
  if (force || quiet || nearBottom(body)) { body.scrollTop = body.scrollHeight; $('jumpBtn').hidden = true; }
  else $('jumpBtn').hidden = false;
}
function onLines(ev) {
  S.people = ev.people || S.people;
  const body = $('transcriptScroll');
  const stick = body ? nearBottom(body) : true;
  if (ev.all) { S.lines = ev.all; renderTranscript(); return; }
  const lastStart = S.lines.length ? S.lines[S.lines.length - 1].start : -1;
  S.lines.push(...ev.lines);
  const inOrder = ev.lines.every((line, i) => line.start >= (i ? ev.lines[i - 1].start : lastStart));
  if (inOrder) ev.lines.forEach(appendLine);
  else { S.lines.sort((a, b) => a.start - b.start || a.seq - b.seq); renderTranscript(); }
  if (body) { if (stick) body.scrollTop = body.scrollHeight; else $('jumpBtn').hidden = false; }
  S.preview = {};
  renderPreview();
}
function onPreview(ev) {
  if (ev.final || !ev.text) delete S.preview[ev.channel]; else S.preview[ev.channel] = ev.text;
  renderPreview();
}
function renderPreview() {
  const box = $('previewLine');
  if (!box) return;
  const text = Object.values(S.preview).join('   ');
  box.hidden = !text;
  box.textContent = text;
  const body = $('transcriptScroll');
  if (body && text && nearBottom(body)) body.scrollTop = body.scrollHeight;
}

/* ---- column 2: the open document, or the live summary ---- */
function renderCol2Head() {
  if (!S.live) return;
  const head = document.querySelector('#col2 .phead');
  if (!head) return;
  const writing = S.summaries[S.live.folder] === 'writing';
  const updated = el('span', { class: 'updated', 'data-ts': String(S.lastNotes || 0) }, S.lastNotes ? 'Updated ' + ago(S.lastNotes) : '');
  const button = el('button', { class: 'ghost', disabled: writing || !S.lines.length, onclick: () => act('/api/meeting/summary') }, writing ? 'Writing summary' : 'Proper summary');
  head.replaceChildren(el('h2', {}, 'Live summary'), updated, button);
}
$('meetingTitle').addEventListener('click', () => {
  const button = $('meetingTitle');
  const input = el('input', { class: 'mtitle-edit', value: S.live.title === 'Meeting' ? '' : S.live.title, placeholder: 'Meeting title', maxlength: 70 });
  button.hidden = true;
  button.after(input);
  input.focus();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    input.remove();
    button.hidden = false;
    if (save && input.value.trim()) act('/api/meeting/title', { title: input.value.trim() });
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); });
  input.addEventListener('blur', () => finish(true));
});
function renderCol2() {
  const col = $('col2');
  if (S.live) {
    col.replaceChildren(el('header', { class: 'phead' }), el('div', { class: 'scroll', id: 'notes' }));
    renderCol2Head();
    renderNotes();
    return;
  }
  const selected = S.selected;
  if (!selected) { col.replaceChildren(); return; }
  const tabs = el('div', { class: 'tabs' });
  const head = el('header', { class: 'phead' }, tabs);
  const body = el('div', { class: 'scroll' });
  col.replaceChildren(head, body);
  if (selected.type === 'state') {
    [['status', 'Project state'], ['changelog', 'Changelog']].forEach(([id, label]) => tabs.append(el('button', { class: selected.tab === id ? 'on' : '', onclick: () => selectDoc({ ...selected, tab: id }) }, label)));
    loadDoc(body, selected.tab === 'status' ? 'State/status.md' : 'State/changelog.md');
    return;
  }
  const meeting = S.meetings.find((m) => m.id === selected.id);
  if (!meeting) { body.append(el('div', { class: 'empty' }, 'That meeting is no longer here.')); return; }
  [['summary', 'Summary'], ['live', meeting.recording ? 'Notes' : 'Live notes'], ['transcript', 'Transcript']].forEach(([id, label]) => tabs.append(el('button', { class: selected.tab === id ? 'on' : '', onclick: () => selectDoc({ ...selected, tab: id }) }, label)));
  const writing = isWriting(meeting);
  head.append(el('div', { class: 'grow' }));
  if (meeting.recording) head.append(el('button', { class: 'ghost', title: 'Play the recording in the corner below', onclick: () => openViewer(meeting.recording) }, 'Recording'));
  if (meeting.import && selected.tab === 'summary') { body.append(importPanel(meeting)); return; }
  if (meeting.import && !meeting.files.includes(selected.tab === 'live' ? 'live-summary.md' : 'transcript.md')) {
    body.append(el('div', { class: 'empty' }, el('b', {}, 'Not ready yet'), 'This appears once the recording has been read. The Summary tab shows how far it has got.'));
    return;
  }
  if (selected.tab === 'summary' && !writing) {
    head.append(el('button', { class: 'ghost', onclick: () => act('/api/meeting/summary', { folder: meeting.id }) }, meeting.files.includes('summary.md') ? 'Rewrite' : 'Write summary'));
  }
  if (selected.tab === 'summary') {
    if (writing) {
      const who = `${S.summaryEngine[meeting.id] || 'The model'} is`;
      body.append(el('div', { class: 'writing' }, el('span', { class: 'spin' }), `${who} writing the proper summary. This can take a few minutes, and you can keep using the app.`));
      return;
    }
    if (!meeting.files.includes('summary.md')) {
      body.append(el('div', { class: 'empty' }, el('b', {}, 'No summary yet'), meeting.summary_status === 'failed' ? 'The last try failed. Press Write summary to try again.' : 'Press Write summary to make one.'));
      return;
    }
    loadDoc(body, `Meetings/${meeting.id}/summary.md`);
  } else if (selected.tab === 'live') loadDoc(body, `Meetings/${meeting.id}/live-summary.md`);
  else loadDoc(body, `Meetings/${meeting.id}/transcript.md`);
}
async function loadDoc(body, path) {
  try {
    const data = await getJSON('/api/view?' + q({ project: S.project, path }));
    body.replaceChildren(renderContent(data));
  } catch (error) {
    body.replaceChildren(el('div', { class: 'empty' }, error.message));
  }
}
const toSeconds = (time) => time.split(':').reduce((total, part) => total * 60 + Number(part), 0);
/* A finished meeting's transcript. Names can be corrected with a click, and with a recording each time jumps the video there. */
function storedTranscript(text, path) {
  const folder = ((path || '').match(/^Meetings\/([^/]+)\//) || [])[1];
  const meeting = S.meetings.find((m) => m.id === folder);
  const editable = !!meeting && !(S.live && S.live.folder === folder);
  const recording = meeting && meeting.recording;
  const out = el('div', { id: 'storedLines', style: 'padding:8px 0 24px' });
  let turn = null, last = null;
  const stamp = (time) => (recording ? el('button', { class: 'stamp', title: 'Play the recording from here', onclick: () => openViewer(recording, null, toSeconds(time)) }, time) : el('time', {}, time));
  text.split('\n').forEach((raw) => {
    const match = raw.match(/^\[([\d:]+)\] ([^:]+): (.*)$/);
    if (!match) {
      if (raw.startsWith('# ')) out.append(el('div', { style: 'padding:6px 12px 0;font-weight:600;color:var(--ink)' }, raw.slice(2)));
      else if (raw.trim()) out.append(el('div', { style: 'padding:0 12px 6px;color:var(--faint);font-size:12.5px' }, raw));
      return;
    }
    const [, time, who, said] = match;
    if (who === 'On screen') {
      out.append(el('div', { class: 'screenline' }, el('span', { class: 'ic', html: ICONS.screen }),
        el('div', {}, el('div', { class: 'who' }, el('span', { class: 'lab' }, 'On screen'), stamp(time)), el('div', { class: 'say' }, said))));
      turn = null; last = null;
      return;
    }
    if (!turn || last !== who) {
      const name = el('button', { class: editable ? '' : 'fixed', style: `--c:${who === 'Me' ? 'var(--accent-text)' : 'var(--ink)'}`, title: editable ? 'Click to correct this name' : '' }, who);
      if (editable) name.addEventListener('click', () => renameStored(name, folder, who));
      turn = el('div', { class: 'turn' }, el('div', { class: 'who' }, name, stamp(time)), el('div', { class: 'say' }));
      out.append(turn);
      last = who;
    }
    turn.querySelector('.say').append(said + ' ');
  });
  return out;
}
function renameStored(button, folder, old) {
  const input = el('input', { value: old, list: 'peopleList', spellcheck: 'false' });
  const list = el('datalist', { id: 'peopleList' }, S.knownPeople.map((p) => el('option', { value: p })));
  button.replaceWith(input, list);
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (save && value && value !== old) {
      try {
        await act('/api/meeting/rename-speaker', { folder, old, new: value });
        toast(`Every "${old}" line is now ${value}. Press Rewrite on the summary to use the new name there too.`);
      } catch { /* the error is already shown */ }
    }
    if (!S.live && S.selected?.type === 'meeting' && S.selected.id === folder) renderCol2();
    refreshViewer(`Meetings/${folder}/`);
  };
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') finish(true); if (event.key === 'Escape') finish(false); });
  input.addEventListener('blur', () => finish(true));
}
function renderContent(data, query, at) {
  let node;
  const url = '/api/file?' + q({ project: S.project, path: data.path });
  if (data.name === 'transcript.md') node = storedTranscript(data.text || '', data.path);
  else if (data.kind === 'markdown') node = el('div', { class: 'doc md', html: markdown(data.text) });
  else if (data.kind === 'text') node = el('div', { class: 'plain' }, data.text, data.cut ? '\n\n[This preview stops here because the file is very large.]' : '');
  else if (data.kind === 'image') node = el('img', { src: url, alt: data.name });
  else if (data.kind === 'pdf') node = el('embed', { src: url, type: 'application/pdf' });
  else if (data.kind === 'frame') node = el('iframe', { src: url, sandbox: '' });
  else if (data.kind === 'video' || data.kind === 'audio') {
    node = el(data.kind, { src: url, controls: true, preload: 'metadata' });
    if (at != null) node.addEventListener('loadedmetadata', () => { node.currentTime = at; }, { once: true });
  }
  else if (data.kind === 'table') {
    node = el('div', {}, data.sheets.map((sheet) => el('div', {}, data.sheets.length > 1 ? el('div', { class: 'sheetname' }, sheet.name) : null,
      el('table', { class: 'sheet' }, el('tbody', {}, sheet.rows.map((row) => el('tr', {}, row.map((cell) => el('td', { title: cell }, cell)))))))));
  } else node = el('div', { class: 'empty' }, el('b', {}, data.name), data.note || 'This type of file cannot be previewed here.', el('br'), el('button', { class: 'link', onclick: () => act('/api/reveal', { path: data.path }) }, 'Show it in its folder'));
  if (query) setTimeout(() => highlight(node, query));
  return node;
}

function renderNotes() {
  const box = $('notes');
  if (!box) return;
  const stick = box.scrollTop < 40;
  box.replaceChildren();
  if (S.now) box.append(el('section', { class: 'nsec now' }, el('h3', {}, 'Right now'), el('p', { class: S.fresh.has('now') ? 'fresh' : '' }, rich(S.now))));
  let any = false;
  const cols = el('div', { class: 'cols' });   // flows into side-by-side columns when the corner is wide
  box.append(cols);
  for (const [id, heading] of SECTIONS) {
    const items = S.notes.filter((n) => n.s === id);
    if (!items.length) continue;
    any = true;
    cols.append(el('section', { class: 'nsec sec-' + id }, el('h3', {}, heading),
      items.map((item) => el('p', { class: S.fresh.has(item.id) ? 'fresh' : '' }, item.q ? el('span', { class: 'q' }, item.q + ' ') : null, rich(item.t)))));
  }
  if (!any && !S.now) box.append(el('div', { class: 'empty' }, el('b', {}, 'The summary builds here as people talk'), 'The assistant updates it about every 25 seconds. Glance here whenever you lose the thread.'));
  S.fresh.clear();
  if (stick) box.scrollTop = 0;
  const updated = document.querySelector('#col2 .updated');
  if (updated) { updated.dataset.ts = String(S.lastNotes || 0); updated.textContent = S.lastNotes ? 'Updated ' + ago(S.lastNotes) : ''; }
}

/* ---- column 3: the assistant ---- */
function renderStatus() {
  const line = $('statusLine');
  line.className = 'state-' + (S.status.state || 'idle');
  const name = engineName(S.engine, S.status.label || S.engineLabel);
  $('statusText').textContent = S.status.text || `${name} is ready`;
  $('stopBtn').hidden = !S.status.busy;
  const used = S.usage || {};
  line.title = used.five_hour != null ? `Claude usage so far: ${Math.round(used.five_hour * 100)}% of the 5 hour limit, ${Math.round((used.seven_day || 0) * 100)}% of the weekly limit` : '';
  renderStatusAge();
}
function renderStatusAge() {
  $('statusAge').textContent = S.live && S.lastNotes ? 'notes ' + ago(S.lastNotes) : '';
}
function renderHelper() {
  $('helperStrip').hidden = !S.helper;
  if (S.helper) $('helperText').textContent = `Helper: ${S.helper.status}${S.helper.queued ? ` (${S.helper.queued} waiting)` : ''}`;
}
function timelineNode(item) {
  if (item.type === 'feed') return el('div', { class: 'feed ' + item.kind }, el('time', {}, hhmm(item.ts)), el('span', {}, item.text));
  if (item.role === 'user') return el('div', { class: 'msg user' }, item.text);
  if (item.role === 'assistant') return botNode(item.engine, markdown(item.text), false, item.extra, item.label);
  if (item.role === 'nudge') return el('div', { class: 'msg nudge' }, rich(item.text));
  if (item.role === 'system' && /^Meeting (started|ended)/.test(item.text)) return el('div', { class: 'day' }, `Meeting ${item.text.startsWith('Meeting started') ? 'started' : 'ended'} at ${hhmm(item.ts)}`);
  return null;
}
function botNode(engine, html, typing, extra, label) {
  const name = engineName(engine, label) + (extra ? ', extra info' : '');
  return el('div', { class: 'msg bot' + (extra ? ' extra' : '') }, el('div', { class: 'from' }, name), el('div', { class: 'md' + (typing ? ' typing' : ''), html }));
}
function renderChat() {
  const box = $('chat');
  box.replaceChildren();
  S.timeline.slice(-300).forEach((item) => { const node = timelineNode(item); if (node) box.append(node); });
  if (!box.children.length) box.append(el('div', { class: 'empty', id: 'chatHint' }, el('b', {}, 'This is your project assistant'), 'It remembers every meeting in this project. Ask it anything, or tell it to change the project state.'));
  box.scrollTop = box.scrollHeight;
}
function addTimeline(item) {
  if (item.type === 'chat' && item.role === 'assistant' && item.bubble && S.bubble[item.bubble]) {
    S.bubble[item.bubble].final = item;
    S.timeline.push(item);
    return;
  }
  S.timeline.push(item);
  const box = $('chat');
  const stick = nearBottom(box);
  const node = timelineNode(item);
  if (node) { $('chatHint')?.remove(); node.classList.add('arrive'); box.append(node); }
  if (stick || (item.role === 'user')) box.scrollTop = box.scrollHeight;
}
function startBubble(id, engine, extra, label) {
  const box = $('chat');
  $('chatHint')?.remove();
  const node = botNode(engine, '', true, extra, label);
  node.classList.add('arrive');
  box.append(node);
  S.bubble[id] = { node, text: '', engine };
  box.scrollTop = box.scrollHeight;
}
let bubbleTimer = null;
function growBubble(id, text) {
  const bubble = S.bubble[id];
  if (!bubble) return;
  bubble.text += text;
  if (bubbleTimer) return;
  bubbleTimer = setTimeout(() => {
    bubbleTimer = null;
    const box = $('chat');
    const stick = nearBottom(box);
    bubble.node.querySelector('.md').innerHTML = markdown(bubble.text);
    if (stick) box.scrollTop = box.scrollHeight;
  }, 70);
}
function endBubble(id) {
  const bubble = S.bubble[id];
  if (!bubble) return;
  clearTimeout(bubbleTimer); bubbleTimer = null;
  const box = $('chat');
  const stick = nearBottom(box);
  const final = bubble.final;
  if (final) {
    const md = bubble.node.querySelector('.md');
    md.classList.remove('typing');
    md.innerHTML = markdown(final.text);
  } else bubble.node.remove();
  delete S.bubble[id];
  if (stick) box.scrollTop = box.scrollHeight;
}
function renderChips() {
  const chips = $('chips');
  const list = S.live ? ['Catch me up', 'What should I say?'] : (S.meetings.length ? ['Where do things stand?'] : []);
  chips.replaceChildren(...list.map((text) => el('button', { onclick: () => send(text) }, text)));
}
function send(text) {
  const value = (text ?? $('input').value).trim();
  if (!value || !S.project) return;
  if (text == null) { $('input').value = ''; growInput(); }
  act('/api/message', { text: value }).catch(() => { if (text == null) $('input').value = value; });
}
function growInput() {
  const input = $('input');
  input.style.height = 'auto';
  input.style.height = Math.min(160, input.scrollHeight) + 'px';
}
$('input').addEventListener('input', growInput);
$('input').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } });
$('sendBtn').addEventListener('click', () => send());
$('stopBtn').addEventListener('click', () => act('/api/stop'));
$('helperStop').addEventListener('click', () => act('/api/helper/stop'));
$('modelPick').addEventListener('change', (event) => {
  const choice = event.target.value;
  if (choice && choice !== S.choice) act('/api/engine', { engine: choice }).catch(() => renderModelPick());
});

/* ---- column 4: files, search and the viewer ---- */
function renderCol4() {
  const col = $('col4');
  if (S.viewer) return renderViewer(col);
  if (S.search.q) return renderResults(col);
  const files = S.files;
  // Side by side when the corner is wide, stacked when it is narrow.
  const groups = el('div', { class: 'filegroups' });
  const list = el('div', { class: 'scroll' }, groups);
  const group = (label) => { const node = el('div', {}, el('div', { class: 'sublabel' }, label)); groups.append(node); return node; };
  const stateGroup = group('Project state');
  files.state.forEach((f) => stateGroup.append(fileItem(f.path, f.name, ICONS.file)));
  const filesGroup = group('Project files');
  if (!files.files.length) filesGroup.append(el('div', { class: 'empty', style: 'padding-top:2px' }, 'Drag files anywhere onto this window to add them.'));
  let folder = null;
  files.files.forEach((f) => {
    if (f.folder && f.folder !== folder) filesGroup.append(el('div', { class: 'folderlab' }, f.folder));
    folder = f.folder;
    filesGroup.append(fileItem(f.path, f.name, ICONS.file));
  });
  if (files.meetings.length) {
    const meetingsGroup = group('Meetings');
    files.meetings.forEach((m) => {
      const open = S.openMeetings.has(m.id);
      meetingsGroup.append(el('button', { class: 'mtoggle', onclick: () => { open ? S.openMeetings.delete(m.id) : S.openMeetings.add(m.id); renderCol4(); } },
        el('div', { class: 't' }, S.live && S.live.folder === m.id && S.live.title !== 'Meeting' ? S.live.title : m.title), el('div', { class: 's' }, niceWhen(m.when))));
      if (open) m.files.forEach((f) => meetingsGroup.append(fileItem(f.path, f.name, ICONS.file, 'sub')));
    });
  }
  const picker = el('input', { type: 'file', multiple: true, hidden: true, onchange: (event) => uploadFiles(event.target.files) });
  col.replaceChildren(el('header', { class: 'phead' }, el('h2', { class: 'grow' }, 'Files'),
    el('button', { class: 'iconbtn', title: 'Add files', html: ICONS.plus, onclick: () => picker.click() }),
    el('button', { class: 'iconbtn', title: 'Open the project folder', html: ICONS.folder, onclick: () => act('/api/reveal') }), picker), list);
}
function fileItem(path, name, icon, extra = '') {
  return el('button', { class: 'fileitem ' + extra, title: name, html: icon, onclick: () => openViewer(path) }, el('span', {}, name));
}
async function openViewer(path, query, at) {
  const playing = document.querySelector('#viewerBody video, #viewerBody audio');
  if (at != null && playing && S.viewer && S.viewer.path === path && S.viewer.data) { playing.currentTime = at; S.viewer.at = at; return; }
  S.viewer = { path, query, at, data: null };
  renderCol4();
  try {
    const data = await getJSON('/api/view?' + q({ project: S.project, path }));
    if (S.viewer && S.viewer.path === path) { S.viewer.data = data; renderCol4(); }
  } catch (error) {
    if (S.viewer) { S.viewer.data = { name: path.split('/').pop(), path, kind: 'other', note: error.message }; renderCol4(); }
  }
}
function refreshViewer(prefix) {
  if (S.viewer && S.viewer.path.startsWith(prefix)) openViewer(S.viewer.path, S.viewer.query);
}
function renderViewer(col) {
  const viewer = S.viewer;
  const title = viewer.data ? viewer.data.name : viewer.path.split('/').pop();
  const nice = { 'summary.md': 'Summary', 'live-summary.md': 'Live notes', 'transcript.md': 'Transcript', 'status.md': 'Project state', 'changelog.md': 'Changelog' }[title] || title;
  const body = el('div', { id: 'viewerBody' });
  if (viewer.data) body.append(renderContent(viewer.data, viewer.query, viewer.at));
  else body.append(el('div', { class: 'starting' }, el('span', { class: 'spin' }), 'Opening'));
  col.replaceChildren(el('header', { class: 'phead' },
    el('button', { class: 'iconbtn', title: S.search.q ? 'Back to results' : 'Back to files', html: ICONS.back, onclick: () => { S.viewer = null; renderCol4(); } }),
    el('h2', { class: 'grow', title }, nice),
    el('button', { class: 'iconbtn', title: 'Show in its folder', html: ICONS.folder, onclick: () => act('/api/reveal', { path: viewer.path }) })),
  el('div', { id: 'viewer' }, body));
}
function renderResults(col) {
  const list = el('div', { class: 'scroll' });
  const results = S.search.results;
  if (results && !results.length) list.append(el('div', { class: 'empty' }, el('b', {}, 'Nothing found'), `No meeting, note or file contains "${S.search.q}".`));
  (results || []).forEach((r) => {
    list.append(el('button', { class: 'result', onclick: () => openViewer(r.path, S.search.q) },
      el('div', { class: 't' }, r.title), el('div', { class: 's' }, [r.when ? niceWhen(r.when) : '', r.count ? `${r.count} matching line${r.count === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ')),
      r.snippets.slice(0, 2).map((s) => { const n = el('div', { class: 'sn' }, s.text); setTimeout(() => highlightNoScroll(n, S.search.q)); return n; })));
  });
  col.replaceChildren(el('header', { class: 'phead' }, el('h2', { class: 'grow' }, results ? `Results${results.length ? ' (' + results.length + ')' : ''}` : 'Searching'),
    el('button', { class: 'iconbtn', title: 'Clear search', html: ICONS.close, onclick: clearSearch })), list);
}
function highlightNoScroll(node, query) {
  const keep = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = () => {};
  highlight(node, query);
  Element.prototype.scrollIntoView = keep;
}
function clearSearch() {
  S.search = { q: '', results: null };
  $('search').value = '';
  renderCol4();
}
let searchTimer = null;
$('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  const value = $('search').value.trim();
  if (!value) { clearSearch(); return; }
  searchTimer = setTimeout(async () => {
    S.search = { q: value, results: S.search.results };
    S.viewer = null;
    renderCol4();
    const data = await getJSON('/api/search?' + q({ project: S.project, q: value }));
    if (S.search.q === value) { S.search.results = data.results; renderCol4(); }
  }, 180);
});
$('search').addEventListener('keydown', (event) => { if (event.key === 'Escape') clearSearch(); });
document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('search').focus(); $('search').select(); }
  if (event.key === 'Escape' && S.viewer && document.activeElement?.tagName !== 'INPUT' && $('popover').hidden && !$('dialog').open) { S.viewer = null; renderCol4(); }
});

async function uploadFiles(files) {
  for (const file of files) {
    const response = await fetch('/api/upload?' + q({ project: S.project, name: file.name }), { method: 'POST', headers: { 'X-KC-App': '1', 'Content-Type': 'application/octet-stream' }, body: file });
    if (!response.ok) toast((await response.json().catch(() => ({}))).error || `Could not add ${file.name}`);
  }
}
let dragDepth = 0;
window.addEventListener('dragenter', (event) => { if (S.project && event.dataTransfer?.types.includes('Files')) { dragDepth += 1; $('dropHint').hidden = false; } });
window.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('dropHint').hidden = true; });
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  $('dropHint').hidden = true;
  if (!S.project || !event.dataTransfer?.files.length) return;
  const files = [...event.dataTransfer.files];
  // A recording dropped in becomes a meeting to import (outside a live meeting); everything else is a project file.
  const recording = !S.live ? files.find((f) => isRecording(f.name)) : null;
  const others = files.filter((f) => f !== recording);
  if (others.length) uploadFiles(others);
  if (recording) importDialog(recording);
});

/* ---- importing a recorded meeting ---- */
const IMPORT_STEPS = [['copy', 'Copy the recording into the project'], ['words', 'Transcribe it with Whisper'], ['voices', 'Tell the voices apart'],
  ['pictures', 'Pick out what was on screen'], ['reading', 'Claude reads the meeting and names the speakers'], ['summary', 'Write the proper summary']];
function sizeText(bytes) {
  return bytes > 1e9 ? (bytes / 1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(bytes / 1e6)) + ' MB';
}
function importProgress(job) {
  if (job.done == null || !job.total) return null;
  const pct = Math.max(0, Math.min(100, job.done / job.total * 100));
  if (job.step === 'words') return { pct, text: `${clock(job.done)} of ${clock(job.total)}` };
  if (job.step === 'copy') return { pct, text: `${sizeText(job.done)} of ${sizeText(job.total)}` };
  return { pct, text: Math.round(pct) + '%' };
}
function importLine(job) {
  if (job.status === 'failed') return 'stopped, open it to see why';
  if (job.status === 'waiting' || job.status === 'queued') return 'waiting its turn';
  const step = IMPORT_STEPS.find(([id]) => id === (job.step === 'audio' ? 'words' : job.step));
  const progress = importProgress(job);
  return (step ? step[1].replace(' with Whisper', '').replace(' and names the speakers', '') : 'working') + (progress ? ' ' + Math.round(progress.pct) + '%' : '');
}
function importPanel(meeting) {
  const job = meeting.import;
  const steps = IMPORT_STEPS.filter(([id]) => id !== 'copy' || job.from);
  const current = job.step === 'audio' ? 'words' : job.step;
  const at = Math.max(0, steps.findIndex(([id]) => id === current));
  const list = el('ol', { class: 'isteps' });
  steps.forEach(([id, label], i) => {
    const row = el('li', { class: i < at ? 'done' : i === at ? (job.status === 'failed' ? 'cur failed' : 'cur') : '' }, el('span', { class: 'step-dot' }), el('div', {}, el('div', { class: 'l' }, label)));
    if (i === at && job.status !== 'failed') {
      const box = row.lastChild;
      if (job.text && job.text !== label) box.append(el('div', { class: 's' }, job.text));
      const progress = importProgress(job);
      if (progress) box.append(el('div', { class: 'bar' }, el('i', { style: `width:${progress.pct}%` })), el('div', { class: 's' }, progress.text));
    }
    list.append(row);
  });
  const panel = el('div', { class: 'importing', id: 'importPanel', 'data-folder': meeting.id },
    el('h3', {}, job.status === 'failed' ? 'The import stopped' : job.status === 'waiting' || job.status === 'queued' ? 'Waiting to import' : 'Importing the recording'),
    el('p', { class: 'hint' }, job.status === 'failed' ? job.error || 'Something went wrong.' : 'You can keep using the app. The summary appears here when it is done.'), list);
  const buttons = el('div', { class: 'row' });
  if (job.status === 'failed') buttons.append(el('button', { class: 'primary', onclick: () => act('/api/import/retry', { folder: meeting.id }) }, 'Try again'));
  const cancel = el('button', { class: 'ghost' }, 'Cancel import');
  let armed = null;
  cancel.addEventListener('click', () => {
    if (!armed) { cancel.textContent = 'Click again to cancel'; armed = setTimeout(() => { armed = null; cancel.textContent = 'Cancel import'; }, 3000); return; }
    clearTimeout(armed);
    act('/api/import/cancel', { folder: meeting.id });
  });
  buttons.append(cancel);
  panel.append(buttons);
  return panel;
}
function onImport(ev) {
  const meeting = S.meetings.find((m) => m.id === ev.folder);
  if (!meeting) return;
  meeting.import = ev.import && ev.import.status !== 'done' ? ev.import : null;
  if (S.live) return;
  renderCol1();
  const panel = $('importPanel');
  if (panel && panel.dataset.folder === ev.folder) {
    if (meeting.import) panel.replaceWith(importPanel(meeting));
    else renderCol2();
  }
}
function uploadRecording(file, fields, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/import?' + q({ project: S.project, name: file.name, ...fields }));
    xhr.setRequestHeader('X-KC-App', '1');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total * 100); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 200 && data.folder) resolve(data.folder);
      else reject(new Error(data.error || 'The recording could not be sent to the app.'));
    };
    xhr.onerror = () => reject(new Error('The recording could not be sent to the app.'));
    xhr.send(file);
  });
}
function importDialog(dropped) {
  const picker = el('input', { type: 'file', accept: 'video/*,audio/*,.mkv,.m4a,.wma', hidden: true });
  const chosen = el('span', { class: 'chosen' }, 'No file chosen');
  const pick = el('button', { class: 'ghost', type: 'button', onclick: () => picker.click() }, 'Choose');
  const title = el('input', { placeholder: 'You can leave this blank', maxlength: 70 });
  const when = el('input', { type: 'datetime-local' });
  const people = el('input', { placeholder: 'For example: Me, Priya Shah, Tom from Illumina', maxlength: 400 });
  const bar = el('div', { class: 'bar', hidden: true }, el('i', { style: 'width:0%' }));
  const go = el('button', { class: 'primary', type: 'submit', disabled: true }, 'Import');
  let file = null;
  const setFile = (f) => { file = f || null; chosen.textContent = file ? `${file.name}, ${sizeText(file.size)}` : 'No file chosen'; go.disabled = !file; };
  picker.addEventListener('change', () => setFile(picker.files[0]));
  const submit = async (event) => {
    event.preventDefault();
    if (!file) return;
    go.disabled = true;
    go.textContent = 'Sending it to the app';
    bar.hidden = false;
    try {
      const folder = await uploadRecording(file, { title: title.value.trim(), when: when.value, people: people.value.trim(), modified: file.lastModified },
        (pct) => { bar.firstChild.style.width = pct + '%'; });
      close();
      S.selected = { type: 'meeting', id: folder, tab: 'summary' };
      renderCol1();
      renderCol2();
    } catch (error) {
      toast(error.message);
      go.disabled = false;
      go.textContent = 'Import';
      bar.hidden = true;
    }
  };
  const close = openPopover($('importBtn'), () => [el('form', { onsubmit: submit }, el('h3', {}, 'Import a recorded meeting'),
    el('div', { class: 'field' }, 'Recording (video or audio)', el('div', { class: 'pickrow' }, pick, chosen), picker),
    el('label', { class: 'field' }, 'Meeting title', title),
    el('label', { class: 'field' }, 'When it happened (blank: the date saved in the file)', when),
    el('label', { class: 'field' }, 'Who was in it, with commas (optional, helps name the voices)', people),
    el('p', { class: 'hint' }, 'Whisper transcribes it on your graphics card, the voices are told apart, and Claude works out who is who from what they say and what is on screen. Then it gets a proper summary and updates the project, like a live meeting. An hour of recording takes about 5 minutes, and a live meeting always goes first.'),
    bar, go)]);
  setFile(dropped);
}
$('importBtn').addEventListener('click', () => importDialog(null));

/* ---- starting and ending a meeting ---- */
$('startBtn').addEventListener('click', () => {
  const title = el('input', { placeholder: 'You can leave this blank', maxlength: 70 });
  const mic = el('select', {}, el('option', { value: '' }, 'Loading microphones...'));
  const speaker = el('p', { class: 'hint' }, 'It also listens to whatever your PC is playing, so the meeting audio needs to come through this PC. Headphones help it tell you apart from everyone else.');
  let devices = { mics: [] };
  const go = async (event) => {
    event.preventDefault();
    const chosen = devices.mics.find((m) => m.name === mic.value);
    close();
    try { localStorage.setItem('kc.mic', mic.value); } catch { /* not remembered */ }
    await act('/api/meeting/start', { title: title.value, mic: chosen || null });
  };
  const close = openPopover($('startBtn'), () => [el('form', { onsubmit: go }, el('h3', {}, 'Start listening'),
    el('label', { class: 'field' }, 'Meeting title', title), el('label', { class: 'field' }, 'Your microphone', mic), speaker,
    el('button', { class: 'primary', type: 'submit' }, 'Start'))]);
  title.focus();
  getJSON('/api/devices').then((data) => {
    devices = data;
    let saved = '';
    try { saved = localStorage.getItem('kc.mic') || ''; } catch { /* no memory */ }
    mic.replaceChildren(el('option', { value: '' }, `Default microphone${data.default_mic ? ' (' + data.default_mic + ')' : ''}`), ...data.mics.map((m) => el('option', { value: m.name }, m.name)));
    if (data.mics.some((m) => m.name === saved)) mic.value = saved;
    if (data.speaker) speaker.textContent = `It also listens to what your PC is playing through ${data.speaker}, so the meeting audio needs to come out of that. Headphones help it tell you apart from everyone else.`;
  }).catch(() => mic.replaceChildren(el('option', { value: '' }, 'Default microphone')));
});
$('pauseBtn').addEventListener('click', () => act('/api/meeting/pause', { paused: !S.live.paused }));
let endTimer = null;
$('endBtn').addEventListener('click', () => {
  const button = $('endBtn');
  if (!endTimer) {
    button.textContent = 'Click again to end';
    endTimer = setTimeout(() => { endTimer = null; renderTop(); }, 3000);
    return;
  }
  clearTimeout(endTimer); endTimer = null;
  act('/api/meeting/end');
});

/* ---- everything ---- */
function renderAll() {
  renderTop(); renderBanner(); renderCol1(); renderCol2(); renderCol4(); renderChat(); renderChips(); renderStatus(); renderHelper();
}
async function boot() {
  const data = await getJSON('/api/projects');
  S.projects = data.projects; S.signedIn = data.signed_in; S.root = data.root; S.claude = data.claude !== false;
  let saved = '';
  try { saved = localStorage.getItem('kc.project') || ''; } catch { /* no memory */ }
  const asked = new URLSearchParams(location.search).get('project');      // Flock opens the project it is on
  const start = S.projects.find((p) => p.name === asked) || S.projects.find((p) => p.name === saved) || S.projects[0];
  if (start) { await selectProject(start.name); return; }
  document.body.classList.add('noproject');
  renderTop();
  $('col2').replaceChildren(el('div', { class: 'welcome' }, document.querySelector('.brand .mark').cloneNode(true), el('h1', {}, 'Welcome to Meetings'),
    'Create a project for each thing you work on. Each one has its own assistant, files, meetings and state.',
    el('br'), el('button', { class: 'primary', onclick: newProjectDialog }, 'Create your first project')));
}
applyLayout();
boot().catch((error) => toast(error.message));
setInterval(async () => { try { const data = await getJSON('/api/projects'); if (data.signed_in !== S.signedIn) { S.signedIn = data.signed_in; renderBanner(); } } catch { /* the app is restarting */ } }, 60000);
