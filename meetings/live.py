"""One project's live brain: its assistant, its current meeting, its helper and its summaries.

The assistant gets one message at a time. The user's typed messages always go first, and they interrupt a turn that is only
reading the transcript, so an answer starts straight away. New transcript lines wait and go as one batch.

Which model: "same as Flock" (the model picked there, through harness_link; the default when the harness is connected),
Claude Code's Opus, or DeepSeek. Switching loses nothing, because the app keeps the record: chat.jsonl and the meeting files are
the truth, and each model has a cursor saying what it has already seen. Whichever model is active is sent what it missed.
"""
import collections
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import weakref

import engines
import harness_link
import importer
import prompts
import settings
import store

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = settings.PYTHON         # the Python with the speech engine's packages
ROLE_FILE = os.path.join(store.APP_DIR, 'role-main.md')
HELPER_ROLE_FILE = os.path.join(store.APP_DIR, 'role-helper.md')
MCP_FILE = os.path.join(store.APP_DIR, 'mcp.json')

LISTEN_EVERY_S = 25         # normal rhythm of notes updates while people talk
LISTEN_SOON_S = 8           # sooner when someone has just asked something
LISTEN_MIN_WORDS = 12       # don't bother the model for a word or two, unless a minute has passed
NUDGE_GAP_S = 60            # a "heads up" is only useful if it is rare; questions still land under "Asked of you"
OPUS_IDLE_CLOSE_S = 900     # the session is resumed on the next message
COMPACT_AFTER_TOKENS = 60000
SECTIONS = [('asked', 'Asked of you'), ('points', 'Key points'), ('decisions', 'Decisions'), ('actions', 'Actions'),
            ('deadlines', 'Deadlines'), ('problems', 'Problems'), ('info', 'Extra info'), ('qa', 'You asked')]
HELPER_QUEUE_MAX = 3
CHOICES = ('harness', 'opus', 'deepseek')
UNAVAILABLE_RETRY_S = 60    # a model that was off is tried again this often, quietly, for the notes
STARTING_RETRY_S = 20
HELPER_STOP_WORDS = set('''the and for with that this from what have about into your their there which would could should will
been were they them then than when where who whom how why does did not are was can its our out all any some more most just also
only very much many such each other over under after before while because said says please check find look meeting
project files file brief assistant user asked typed'''.split())
SECTION_IDS = {s for s, _ in SECTIONS}
ALL_SESSIONS = weakref.WeakSet()


def capture_running():
    """Is any project's live meeting transcribing right now? Then Whisper has the GPU, and imports wait."""
    for session in list(ALL_SESSIONS):
        capture = session.capture
        if capture is not None and capture.proc.poll() is None:
            return True
    return False


def prepare_app_files():
    store.ensure_root()
    store.atomic_write(ROLE_FILE, prompts.ROLE)
    store.atomic_write(HELPER_ROLE_FILE, prompts.helper_role())
    # Computer use (screenshots, mouse, keyboard) is written for Windows; elsewhere the helper has no computer tools.
    config = {'mcpServers': {'computer': {'type': 'stdio', 'command': VENV_PYTHON,
                                          'args': ['-X', 'utf8', os.path.join(HERE, 'computer_mcp.py')]}}} if settings.WINDOWS else {'mcpServers': {}}
    store.atomic_write(MCP_FILE, json.dumps(config, indent=1))


def clock(seconds):
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes:02d}:{secs:02d}'


def helper_label(run):
    """What the user sees the helper doing: their own words, or the short label the assistant sent with its brief."""
    if run['asked']:
        return re.sub(r'\s+', ' ', run['asked']).strip()[:110]
    label = run.get('label')
    label = re.sub(r'\s+', ' ', label).strip().rstrip('.') if isinstance(label, str) else ''
    return label[:80] or 'something from the meeting'


def first_sentence(text, limit=220):
    text = re.sub(r'\s+', ' ', re.sub(r'[*_`#>]', '', text or '')).strip()
    match = re.match(r'(.{20,}?[.!?])(\s|$)', text)
    piece = match[1] if match else text
    return piece[:limit]


def parse_json_reply(text):
    match = re.search(r'\{.*\}', text or '', re.S)
    if not match:
        return {}
    try:
        value = json.loads(match[0])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def split_ops(text):
    """A reply's visible answer, and the hidden <ops> block the app applies."""
    match = re.search(r'<ops>(.*?)(</ops>|$)', text or '', re.S)
    if not match:
        return (text or '').strip(), {}
    return text[:match.start()].strip(), parse_json_reply(match[1])


def visible_part(text):
    """What can be shown while streaming: never the <ops> block, or the start of its tag."""
    at = text.find('<ops>')
    if at >= 0:
        return text[:at]
    for size in range(min(4, len(text)), 0, -1):
        if '<ops>'.startswith(text[-size:]):
            return text[:-size]
    return text


class LiveMeeting:
    """The meeting in progress: transcript lines, speakers and rough notes. Saved to its folder as it goes."""

    def __init__(self, project, folder, title, when):
        self.project, self.folder, self.title, self.when = project, folder, title, when
        self.started = time.time()
        self.lines = []
        self.seq = 0
        self.people = {'me': 'Me', 'other': 'Others'}
        self.named = set()
        self.now = ''
        self.items = {}
        self.offset = 0.0
        self.dirty = True
        self.ready = False
        self.paused = False
        self.ending = False
        self.urgent = False
        self.ignored = set()
        self.loading = 'Starting'
        self.mic_name = ''
        self.note_seq = 0
        self.named_title = False

    @property
    def dir(self):
        return self.project.meeting_dir(self.folder)

    def label(self, pid):
        if pid not in self.people:
            count = sum(1 for key in self.people if key not in ('me', 'other'))
            self.people[pid] = f'Person {count + 1}'
        return self.people[pid]

    def add_rows(self, rows, removed=()):
        """Returns (new lines, whether older lines changed)."""
        new, changed = [], False
        for rid in removed or ():
            before = len(self.lines)
            self.lines = [line for line in self.lines if line['id'] != str(rid)]
            changed = changed or len(self.lines) != before
        for row in rows or ():
            text = str(row.get('text') or '').strip()
            line_id = str(row.get('id') or row.get('segment_id') or '')
            if not text or line_id in self.ignored:
                continue
            channel = row.get('channel') or ''
            pid = 'me' if channel == 'mic' else str(row.get('speaker_participant_id') or row.get('speaker') or 'other')
            start = float(row.get('start_s') or 0) + self.offset
            end = float(row.get('end_s') or start) + self.offset if row.get('end_s') is not None else start
            existing = next((line for line in self.lines if line['id'] == line_id), None) if line_id else None
            if existing:
                existing.update(text=text, pid=pid, start=start, end=end)
                self.label(pid)
                changed = True
                continue
            if self._is_echo(pid, text, start, end, line_id):
                changed = True if pid != 'me' else changed
                if pid == 'me':
                    continue
            self.label(pid)
            self.seq += 1
            line = {'seq': self.seq, 'id': line_id or f'x{self.seq}', 'pid': pid, 'text': text, 'start': start, 'end': end}
            self.lines.append(line)
            new.append(line)
            if pid != 'me' and text.rstrip().endswith('?'):
                self.urgent = True
        if new or changed:
            self.lines.sort(key=lambda line: (line['start'], line['seq']))
            self.dirty = True
        return new, changed

    def _is_echo(self, pid, text, start, end, line_id):
        """When the user isn't on headphones the mic also picks up the other people: keep their line, drop the copy."""
        if len(text) < 15:
            return False
        for other in reversed(self.lines[-40:]):
            if other['start'] < start - 8:
                break
            if (other['pid'] == 'me') == (pid == 'me') or other['end'] < start - 3 or other['start'] > end + 3:
                continue
            if difflib.SequenceMatcher(None, text.lower(), other['text'].lower()).ratio() > 0.78:
                if pid == 'me':
                    self.ignored.add(line_id)
                else:
                    self.lines.remove(other)
                    self.ignored.add(other['id'])
                return True
        return False

    def line_text(self, line):
        return f'[{clock(line["start"])}] {self.label(line["pid"])}: {line["text"]}'

    def new_since(self, seq):
        return [line for line in self.lines if line['seq'] > seq]

    def upsert_note(self, note_id, section, text, question=None):
        if section not in SECTION_IDS:
            section = 'points'
        existing = self.items.get(note_id)
        if existing:
            existing.update(s=section, t=text, at=time.time())
            if question:
                existing['q'] = question
        else:
            self.note_seq += 1
            self.items[note_id] = {'id': note_id, 's': section, 't': text, 'at': time.time(), 'n': self.note_seq}
            if question:
                self.items[note_id]['q'] = question
        self.dirty = True

    def notes_list(self):
        return sorted(self.items.values(), key=lambda item: item.get('n', 0))

    def notes_markdown(self):
        out = [f'# Rough notes: {self.title}', '', f'{self.when}. Updated {time.strftime("%H:%M")}.', '']
        if self.now:
            out += ['## Right now', '', self.now, '']
        for section, heading in SECTIONS:
            items = [i for i in self.notes_list() if i['s'] == section]
            if items:
                out += [f'## {heading}', '']
                for item in items:
                    out += [f'{item["q"]} {item["t"]}' if item.get('q') else item['t'], '']
        return '\n'.join(out)

    def transcript_markdown(self):
        names = sorted({self.label(line['pid']) for line in self.lines})
        out = [f'# Transcript: {self.title}', '', f'{self.when}. People: {", ".join(names) or "nobody yet"}.', '']
        out += [self.line_text(line) for line in self.lines]
        return '\n'.join(out) + '\n'

    def flush(self):
        """Write the two live files, plus a private copy that lets a crash carry on where it left off."""
        os.makedirs(self.dir, exist_ok=True)
        store.atomic_write(os.path.join(self.dir, 'transcript.md'), self.transcript_markdown())
        store.atomic_write(os.path.join(self.dir, 'live-summary.md'), self.notes_markdown())
        store.atomic_write(os.path.join(self.project.app_dir, 'live.json'), json.dumps({
            'folder': self.folder, 'title': self.title, 'when': self.when, 'started': self.started, 'lines': self.lines,
            'seq': self.seq, 'people': self.people, 'named': sorted(self.named), 'now': self.now, 'items': self.items,
            'note_seq': self.note_seq}, ensure_ascii=False))
        self.dirty = False

    @classmethod
    def restore(cls, project):
        path = os.path.join(project.app_dir, 'live.json')
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if not os.path.isdir(project.meeting_dir(data['folder'])):
                raise ValueError
        except (OSError, ValueError, KeyError):
            return None
        meeting = cls(project, data['folder'], data['title'], data['when'])
        meeting.started = data.get('started') or time.time()
        meeting.lines = data.get('lines') or []
        meeting.seq = data.get('seq') or 0
        meeting.people = data.get('people') or meeting.people
        meeting.named = set(data.get('named') or [])
        meeting.now = data.get('now') or ''
        meeting.items = data.get('items') or {}
        meeting.note_seq = data.get('note_seq') or 0
        meeting.offset = (meeting.lines[-1]['end'] + 2) if meeting.lines else 0.0
        meeting.named_title = meeting.title != 'Meeting'
        return meeting

    def snapshot(self):
        return {'folder': self.folder, 'title': self.title, 'when': self.when, 'started': self.started, 'lines': self.lines,
                'people': self.people, 'named': sorted(self.named), 'now': self.now, 'notes': self.notes_list(),
                'ready': self.ready, 'paused': self.paused, 'ending': self.ending, 'loading': self.loading, 'mic': self.mic_name}


class Capture:
    """The hidden transcription process. It lives only for the meeting."""

    def __init__(self, on_event, title='', mic=None):
        self.on_event = on_event
        script = os.environ.get('KC_CAPTURE_SCRIPT') or os.path.join(HERE, 'capture_host.py')
        cmd = [VENV_PYTHON, '-X', 'utf8', script, '--title', title or '']
        if mic and mic.get('name'):
            cmd += ['--mic-name', mic['name'], '--mic-hostapi', mic.get('hostapi') or '']
        self.log = open(os.path.join(store.APP_DIR, 'capture.log'), 'w', encoding='utf-8', errors='replace')
        self.proc = subprocess.Popen(cmd, cwd=HERE, env=capture_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                     encoding='utf-8', errors='replace', creationflags=settings.NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith('{'):
                try:
                    self.on_event(json.loads(line))
                except ValueError:
                    pass
        self.proc.wait()
        self.on_event({'ev': 'exit', 'code': self.proc.returncode})

    def command(self, name):
        try:
            self.proc.stdin.write(json.dumps({'cmd': name}) + '\n')
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def kill(self):
        try:
            self.proc.kill()
        except OSError:
            pass


def capture_log_tail():
    """The last line the transcription process wrote, usually the reason it stopped."""
    lines = [line.strip() for line in store.read_text(os.path.join(store.APP_DIR, 'capture.log'))[-4000:].splitlines() if line.strip()]
    return lines[-1][:240] if lines else ''


def capture_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CLAUDE', 'ANTHROPIC'))}
    if settings.HF_HOME:
        env['HF_HOME'] = settings.HF_HOME
    os.makedirs(settings.ENGINE_DATA, exist_ok=True)
    env.update(OPENWHISPER_DATA_DIR=settings.ENGINE_DATA, FLOCK_MEETINGS_ENGINE=settings.ENGINE, KC_MEETINGS_ENGINE=settings.ENGINE,  # publish-scan: ok (the engine's own variable)
               HF_HUB_DISABLE_SYMLINKS_WARNING='1', PYTHONUTF8='1',
               QT_QPA_PLATFORM='offscreen')
    return env


_devices = {'at': 0, 'value': None}


def audio_devices():
    """The microphones to choose from, asked of a short-lived process so the app itself never opens audio."""
    if _devices['value'] and time.time() - _devices['at'] < 60:
        return _devices['value']
    script = os.path.join(HERE, 'capture_host.py')
    try:
        out = subprocess.run([VENV_PYTHON, '-X', 'utf8', script, '--devices'], cwd=HERE, env=capture_env(), capture_output=True,
                             text=True, timeout=40, encoding='utf-8', errors='replace',
                             creationflags=settings.NO_WINDOW).stdout
        for line in out.splitlines():
            if line.startswith('{'):
                _devices.update(at=time.time(), value=json.loads(line))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return _devices['value'] or {'mics': [], 'default_mic': '', 'speaker': ''}


class ProjectSession:
    def __init__(self, project):
        self.project = project
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.clients = []
        meta = project.meta()
        self.choice_saved = meta.get('model_choice') if meta.get('model_choice') in CHOICES else None
        self.legacy_engine = meta.get('engine') if meta.get('engine') in ('opus', 'deepseek') else 'opus'
        self.opus = None
        self.deepseek = None
        self.harness_engines = {}
        self.retry_at = 0.0
        self.meeting = LiveMeeting.restore(project)
        self.capture = None
        self.chat = project.read_chat()
        self.feed = collections.deque(maxlen=120)
        self.user_jobs = collections.deque()
        self.system_jobs = collections.deque()
        self.current = None
        self.blocked = None
        self.banner = None
        self.failed_job = None
        self.last_listen = 0.0
        self.listen_not_before = 0.0
        self.last_notes = 0.0
        self.status = {'state': 'idle', 'text': ''}
        self.helper = None
        self.helper_queue = collections.deque()
        self.failed_helper = None
        self.summaries = {}
        self.pending_updates = []
        self.limit_warned = False
        self.last_nudge = 0.0
        self.file_signature = 0
        self.last_activity = time.time()
        self.closed = False
        scratch = os.path.join(project.dir, '.copilot', 'tmp')      # the helper's scratch space, cleared when the app opens the project
        shutil.rmtree(scratch, ignore_errors=True)
        os.makedirs(scratch, exist_ok=True)
        if self.meeting:
            self.meeting.loading = 'Interrupted'
            self.banner = {'kind': 'interrupted', 'text': f'The meeting "{self.meeting.title}" was cut off. Nothing was lost.',
                           'actions': [{'id': 'restart_capture', 'label': 'Keep listening'}, {'id': 'finish_meeting', 'label': 'Finish it'}]}
        for folder, info in (meta.get('meetings') or {}).items():
            if info.get('summary_status') == 'writing':
                project.update_meeting(folder, summary_status='failed')     # the app stopped while it was being written
        ALL_SESSIONS.add(self)
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._tick, daemon=True).start()
        self.importer = importer.Importer(self)

    # ---- talking to the browser ----
    def emit(self, event):
        for client in list(self.clients):
            try:
                client.put_nowait(event)
            except Exception:
                pass

    def set_status(self, state, text=''):
        self.status = {'state': state, 'text': text}
        self.emit({'type': 'status', **self.status_payload()})

    def status_payload(self):
        return {**self.status, 'engine': self.engine_name, 'label': self.label_for(self.engine_name), 'last_notes': self.last_notes,
                'queued': len(self.user_jobs), 'busy': self.current is not None and self.current['kind'] in ('user', 'helper_result')}

    def add_feed(self, text, kind='info'):
        entry = {'text': text, 'kind': kind, 'ts': time.time()}
        self.feed.append(entry)
        self.emit({'type': 'feed', **entry})

    def add_chat(self, role, text, engine=None, **extra):
        """The permanent record: everything either model needs to know is here or in the meeting files."""
        with self.lock:
            entry = {'n': len(self.chat), 'ts': time.time(), 'role': role, 'text': text, 'engine': engine,
                     'meeting': self.meeting.folder if self.meeting else None, **extra}
            self.chat.append(entry)
            self.project.append_chat(entry)
        self.emit({'type': 'chat', 'entry': entry})
        return entry

    def set_banner(self, kind, text, actions=None):
        if not engines.deepseek_available():
            actions = [a for a in actions or [] if 'deepseek' not in a['id']]
        self.banner = {'kind': kind, 'text': text, 'actions': actions or []}
        self.emit({'type': 'banner', 'banner': self.banner})

    def clear_banner(self, kinds=None):
        if self.banner and (kinds is None or self.banner['kind'] in kinds):
            self.banner = None
            self.emit({'type': 'banner', 'banner': None})

    def snapshot(self):
        with self.lock:
            meta = self.project.meta()
            return {'project': {'name': self.project.name, 'description': meta.get('description', ''), 'dir': self.project.dir},
                    **self.engine_payload(), 'status': self.status_payload(), 'chat': self.chat[-300:], 'feed': list(self.feed),
                    'meetings': self.project.meetings(), 'files': self.project.file_tree(),
                    'live': self.meeting.snapshot() if self.meeting else None, 'helper': self.helper_state(),
                    'banner': self.banner, 'usage': self.usage_payload(), 'people': meta.get('people') or [],
                    }

    def usage_payload(self):
        opus = self.opus
        windows = (opus.rate_limit.get('unifiedWindows') or {}) if opus else {}
        return {'turns': opus.totals['turns'] if opus else 0, 'output': opus.totals['output'] if opus else 0,
                'cache_read': opus.totals['cache_read'] if opus else 0, 'context': opus.context_tokens if opus else 0,
                'five_hour': (windows.get('five_hour') or {}).get('utilization'), 'seven_day': (windows.get('seven_day') or {}).get('utilization')}

    # ---- which model ----
    def _opus(self):
        if self.opus is None:
            meta = self.project.meta()
            session_id = meta.get('opus_session')
            if not session_id:
                session_id = str(uuid.uuid4())
                self.project.update_meta(opus_session=session_id, opus_started=False)
            self.opus = engines.OpusEngine(self.project.dir, session_id, bool(meta.get('opus_started')), ROLE_FILE)
        return self.opus

    def _deepseek(self):
        if self.deepseek is None:
            self.deepseek = engines.DeepSeekEngine(os.path.join(self.project.app_dir, 'deepseek.json'), prompts.deepseek_role())
        return self.deepseek

    def _session_tag(self, what=''):
        """A stable, header-safe name for this project's conversation at a provider (keeps it on one cache)."""
        import hashlib
        return f'kc-meetings{what}-' + hashlib.sha256(self.project.name.encode('utf-8')).hexdigest()[:12]

    def _harness(self, model_id):
        engine = self.harness_engines.get(model_id)
        if engine is None:
            safe = re.sub(r'[^A-Za-z0-9_.-]', '_', model_id)[:60]
            engine = engines.HarnessEngine(os.path.join(self.project.app_dir, f'harness-{safe}.json'), model_id,
                                           prompts.harness_role(self.label_for('h:' + model_id)), self._session_tag())
            self.harness_engines[model_id] = engine
        return engine

    def engine(self):
        name = self.engine_name
        if name == 'opus':
            return self._opus()
        if name == 'deepseek':
            return self._deepseek()
        return self._harness(name[2:])

    def choice(self):
        """What the user picked here: 'harness' (follow Flock), 'opus' or 'deepseek'. With no pick, follow the harness
        when it is connected, else the model this project used before."""
        choice = self.choice_saved
        if choice == 'deepseek' and not engines.deepseek_available():
            choice = None
        if choice:
            return choice
        if harness_link.linked():
            return 'harness'
        return 'deepseek' if self.legacy_engine == 'deepseek' and engines.deepseek_available() else 'opus'

    @property
    def engine_name(self):
        """The engine answering now: 'opus', 'deepseek', or 'h:<connection id>' for a model reached through Flock.
        Claude picked in the harness runs on this app's own Claude Code engine, which keeps one session per project."""
        choice = self.choice()
        if choice != 'harness':
            return choice
        model = harness_link.current()
        if harness_link.is_claude(model):
            return 'opus'
        return 'h:' + (model['id'] if model else 'selected')

    def label_for(self, name):
        if name == 'opus':
            return engines.OPUS_LABEL
        if name == 'deepseek':
            return 'DeepSeek'
        if name and name.startswith('h:'):
            model = harness_link.current()
            if model and model['id'] == name[2:]:
                return model['name']
            info = harness_link.models().get(name[2:]) or {}
            return info.get('name') or ("Flock's model" if name == 'h:selected' else name[2:])
        return 'Assistant'

    def engine_payload(self):
        name = self.engine_name
        choices = []
        if harness_link.linked() or self.choice_saved == 'harness':
            model = harness_link.current()
            choices.append({'id': 'harness', 'label': f'Same as Flock ({model["name"]})' if model else 'Same as Flock'})
        choices.append({'id': 'opus', 'label': f'{engines.OPUS_LABEL} (Claude Code)'})
        if engines.deepseek_available():
            choices.append({'id': 'deepseek', 'label': 'DeepSeek'})
        return {'engine': name, 'label': self.label_for(name), 'choice': self.choice(), 'choices': choices,
                'deepseek': engines.deepseek_available(), 'harness': harness_link.linked()}

    def set_engine(self, choice):
        if choice not in CHOICES:
            raise ValueError('Unknown model.')
        if choice == 'deepseek' and not engines.deepseek_available():
            raise ValueError('DeepSeek is not set up on this machine.')
        with self.lock:
            self.choice_saved = choice
            self.project.update_meta(model_choice=choice)
            self._engine_changed(f'Switched to {self.label_for(self.engine_name)}. It picks up everything so far.')

    def _engine_changed(self, text):
        with self.lock:
            self.blocked = None
            self.clear_banner(('refused', 'limit', 'signed_out', 'unavailable', 'trouble'))
            failed, self.failed_job = self.failed_job, None
            if failed:
                self.user_jobs.appendleft(failed)
            self.add_feed(text, 'switch')
            self.emit({'type': 'engine', **self.engine_payload()})
            if self.current is None:
                self._set_resting_status()
            else:
                self.emit({'type': 'status', **self.status_payload()})
            self.wake.notify_all()

    def harness_changed(self, before):
        """Flock connected, or its model picker changed. A project that follows it uses the new model from the next turn."""
        if self.engine_name != before:
            self._engine_changed(f'Flock now uses {self.label_for(self.engine_name)}. The next answer comes from it, '
                                 'and it picks up everything so far.')
        else:
            self.emit({'type': 'engine', **self.engine_payload()})
            if self.current is None:
                self._set_resting_status()

    def fresh_opus(self):
        """A new Opus session that starts from the full record, for when the old one gets stuck on a safety flag."""
        with self.lock:
            if self.opus:
                self.opus.close()
                self.opus = None
            self.project.update_meta(opus_session=str(uuid.uuid4()), opus_started=False)
            cursors = self.project.meta().get('cursors') or {}
            cursors.pop('opus', None)
            self.project.update_meta(cursors=cursors)
        self.add_feed('Started a fresh Opus session from the full project record.', 'switch')
        self.set_engine('opus')

    def _cursor(self, name):
        cursor = (self.project.meta().get('cursors') or {}).get(name) or {}
        return {'chat': cursor.get('chat', 0), 'seq': cursor.get('seq', 0), 'meeting': cursor.get('meeting')}

    def _save_cursor(self, name, cursor):
        with self.project.lock:
            cursors = self.project.meta().get('cursors') or {}
            cursors[name] = cursor
            self.project.update_meta(cursors=cursors)

    # ---- what a model is told ----
    def _needs_handoff(self, engine):
        if engine.name.startswith('h:'):
            if engine.over_budget():
                engine.reset()      # it has read as much as fits: start it again from the project record
                self.add_feed(f'{engine.label} has read as much as fits in its memory, so it starts again from the project record.', 'switch')
            return engine.fresh
        if engine.name == 'deepseek':
            return engine.fresh
        return not self.project.meta().get('opus_started')

    def handoff_pack(self, transcript_chars=60000):
        """Everything so far, built from the files. How a model takes over a project it has never seen."""
        project, meeting, parts = self.project, self.meeting, []
        meta = project.meta()
        parts.append(f'Project: {project.name}. {meta.get("description", "")}'.strip())
        parts.append(f'<project_state>\n{project.read_status()}\n</project_state>')
        changes = store.read_text(project.changelog_path).splitlines()[-30:]
        parts.append('<changelog_recent>\n' + '\n'.join(changes) + '\n</changelog_recent>')
        finished = [m for m in project.meetings() if not (meeting and m['id'] == meeting.folder)]
        if finished:
            parts.append('Past meetings: ' + '; '.join(f'{m["when"]} {m["title"]}' for m in finished[:30]))
        for item in finished[:3]:
            text = store.read_text(os.path.join(project.meeting_dir(item['id']), 'summary.md'), 8000)
            if text:
                parts.append(f'<meeting_summary folder="{item["id"]}">\n{text}\n</meeting_summary>')
        names = [item['path'] for item in project.project_files()[:80]]
        if names:
            parts.append('Project files: ' + ', '.join(names))
        if meeting:
            parts.append(f'A meeting is running now: "{meeting.title}", started {meeting.when}.')
            if meeting.items or meeting.now:
                parts.append('<current_notes>\n' + json.dumps({'now': meeting.now, 'notes': [
                    {'id': i['id'], 's': i['s'], 't': i['t']} for i in meeting.notes_list()]}, ensure_ascii=False) + '\n</current_notes>')
            lines = '\n'.join(meeting.line_text(line) for line in meeting.lines)
            parts.append('<transcript_so_far>\n' + lines[-transcript_chars:] + '\n</transcript_so_far>')
        recent = [entry for entry in self.chat if entry['role'] in ('user', 'assistant', 'system')][-30:]
        if recent:
            parts.append('<recent_chat>\n' + '\n'.join(self._chat_line(entry) for entry in recent) + '\n</recent_chat>')
        return '<handoff>\n' + '\n\n'.join(parts) + '\n</handoff>'

    @staticmethod
    def _chat_line(entry):
        who = {'user': f'{settings.User} asked', 'assistant': (entry.get('label') or entry.get('engine') or 'assistant').capitalize() + ' answered',
               'system': 'Note'}.get(entry['role'], entry['role'])
        return f'- {who}: {entry["text"]}'

    def _build_message(self, engine, job):
        """Returns the text, plus the cursor to save once the model has it."""
        cursor = self._cursor(engine.name)
        meeting = self.meeting
        parts = [time.strftime('(Now: %A %d %B %Y, %H:%M)')]
        chat_end = len(self.chat)
        skip = {job.get('chat_n')} | {queued.get('chat_n') for queued in self.user_jobs}
        seq = cursor['seq'] if meeting and cursor.get('meeting') == meeting.folder else 0
        if self._needs_handoff(engine):
            parts.append(self.handoff_pack(getattr(engine, 'handoff_chars', 60000)))
            seq = meeting.seq if meeting else 0
        else:
            missed = [entry for entry in self.chat[cursor['chat']:] if entry['role'] in ('user', 'assistant', 'system')
                      and entry.get('engine') != engine.name and entry['n'] not in skip]
            if missed:
                parts.append('<updates>\n' + '\n'.join(self._chat_line(entry) for entry in missed) + '\n</updates>')
        if getattr(engine, 'compacted', False) and meeting:
            parts.append('<current_notes>\n' + json.dumps({'now': meeting.now, 'notes': [
                {'id': i['id'], 's': i['s'], 't': i['t']} for i in meeting.notes_list()]}, ensure_ascii=False) + '\n</current_notes>')
            engine.compacted = False
        next_seq = meeting.seq if meeting else 0
        if meeting:
            lines = meeting.new_since(seq)
            if lines:
                body = '\n'.join(meeting.line_text(line) for line in lines).replace('</transcript', '< /transcript')
                parts.append(f'<transcript meeting="{meeting.title}">\n{body}\n</transcript>')
        kind = job['kind']
        if kind == 'user':
            parts.append(f'<user>{job["text"]}</user>')
        elif kind == 'helper_result':
            source = 'user' if job.get('asked') else 'assistant'
            brief = job['question'][:300].replace('"', "'")
            parts.append(f'<helper_result source="{source}" brief="{brief}">\n{job["text"]}\n</helper_result>')
        elif kind == 'meeting_ended':
            info = self.project.meeting_meta(job['folder'])
            title = str(job['title']).replace('"', "'")
            if info.get('source') == 'recording':
                parts.append(f'<meeting_ended title="{title}" folder="{job["folder"]}" imported="yes" date="{info.get("when", "")}">\n'
                             f'{settings.User} imported a recording of this meeting. Its summary:\n\n{job["text"]}\n</meeting_ended>')
            else:
                parts.append(f'<meeting_ended title="{title}" folder="{job["folder"]}">\nThe meeting has ended. Its summary:\n\n'
                             f'{job["text"]}\n</meeting_ended>')
        return '\n\n'.join(parts), {'chat': chat_end, 'seq': next_seq, 'meeting': meeting.folder if meeting else None}

    # ---- the assistant's turns ----
    def submit(self, text):
        text = (text or '').strip()
        if not text:
            return
        with self.lock:
            self.last_activity = time.time()
            entry = self.add_chat('user', text)
            self.user_jobs.append({'kind': 'user', 'text': text, 'chat_n': entry['n']})
            current = self.current
            if current and current['kind'] == 'listen':
                current['engine'].interrupt()       # answers come first; the notes catch up right after
            self.emit({'type': 'status', **self.status_payload()})
            self.wake.notify_all()

    def stop(self):
        with self.lock:
            current = self.current
            if current and current['kind'] in ('user', 'helper_result', 'listen'):
                current['engine'].interrupt()

    def _listen_due(self):
        meeting = self.meeting
        if not meeting or not meeting.ready or meeting.paused or meeting.ending or self.blocked:
            return False
        now = time.time()
        if now < self.listen_not_before:
            return False
        cursor = self._cursor(self.engine_name)
        seq = cursor['seq'] if cursor.get('meeting') == meeting.folder else 0
        pending = meeting.new_since(seq)
        if not pending:
            return False
        since = now - self.last_listen
        words = sum(len(line['text'].split()) for line in pending)
        if meeting.urgent and since >= LISTEN_SOON_S:
            return True
        return since >= LISTEN_EVERY_S and (words >= LISTEN_MIN_WORDS or since >= 60)

    def _next_job(self):
        with self.lock:
            while not self.closed:
                if self.user_jobs:
                    return self.user_jobs.popleft()
                if self.system_jobs and not self.blocked:
                    return self.system_jobs.popleft()
                if self._listen_due():
                    return {'kind': 'listen'}
                self.wake.wait(1.0)
        return None

    def _loop(self):
        while not self.closed:
            job = self._next_job()
            if job is None:
                return
            try:
                self._run(job)
            except Exception as exc:
                self.add_feed(f'Something went wrong: {type(exc).__name__}: {exc}', 'error')
                with open(os.path.join(store.APP_DIR, 'errors.log'), 'a', encoding='utf-8') as f:
                    import traceback
                    f.write(time.strftime('%H:%M:%S ') + traceback.format_exc() + '\n')
                self.current = None
                self.set_status('idle')
                time.sleep(2)

    def _run(self, job):
        kind = job['kind']
        with self.lock:
            engine = self.engine()
            if kind == 'compact':
                text, next_cursor = job['text'], None
            else:
                text, next_cursor = self._build_message(engine, job)
            if self.meeting:
                self.meeting.urgent = False
            if kind == 'listen':
                self.last_listen = time.time()
            self.current = {'kind': kind, 'engine': engine}
        label = engine.label
        if engine.name.startswith('h:'):
            engine.effort = 'none' if kind in ('listen', 'compact') else 'medium'      # notes fast; answers think a little
            engine.wait = 60 if kind == 'user' else 0        # a question waits a minute for a model server that is starting
        bubble = uuid.uuid4().hex[:10]
        answering = kind in ('user', 'helper_result')
        if kind == 'listen':
            self.set_status('reading', f'{label} is reading the latest lines')
        elif kind == 'user':
            self.set_status('thinking', f'{label} is thinking')
            self.emit({'type': 'answer_start', 'id': bubble, 'engine': engine.name, 'label': label})
        elif kind == 'helper_result':
            self.set_status('thinking', f'{label} is reading what the helper found')
            self.emit({'type': 'answer_start', 'id': bubble, 'engine': engine.name, 'label': label, 'extra': not job.get('asked')})
        elif kind == 'meeting_ended':
            self.set_status('thinking', f'{label} is updating the project state')
        elif kind == 'compact':
            self.set_status('thinking', f'{label} is tidying its memory')
        buffer = []
        shown = [0]
        started = time.time()
        first_text = []

        def on_engine(what, value):
            if what == 'delta' and answering:
                if not first_text:
                    first_text.append(round(time.time() - started, 1))
                buffer.append(value)
                visible = visible_part(''.join(buffer))
                if len(visible) > shown[0]:
                    self.emit({'type': 'answer_delta', 'id': bubble, 'text': visible[shown[0]:]})
                    shown[0] = len(visible)
                    if self.status['state'] != 'answering':
                        self.set_status('answering', f'{label} is answering')
            elif what == 'thinking' and answering:
                self.set_status('thinking', f'{label} is thinking')
            elif what == 'status':
                self.add_feed(value)
            elif what == 'wrong_model':
                self.add_feed(f'Warning: this reply came from {value}, not {engines.OPUS_LABEL}.', 'warn')
            elif what == 'rate_limit':
                if value.get('status') == 'allowed_warning' and not self.limit_warned:
                    self.limit_warned = True
                    self.add_feed('You are getting close to your Claude usage limit.', 'warn')

        timeout = {'listen': 150, 'user': 600, 'helper_result': 600, 'meeting_ended': 600, 'compact': 600}[kind]
        result = engine.turn(text, on_engine, timeout)
        self.current = None
        result['first_text_s'] = first_text[0] if first_text else None
        self._log_usage(kind, engine.name, result)
        if engine.name == 'opus' and result.get('kind') not in ('crashed', 'lost_session', 'signed_out'):
            if not self.project.meta().get('opus_started'):
                self.project.update_meta(opus_started=True)
        if result.get('ok'):
            self.blocked = None
            self.clear_banner(('refused', 'limit', 'signed_out', 'trouble', 'unavailable'))
            if next_cursor:
                self._save_cursor(engine.name, next_cursor)
            self._handle_reply(job, engine, result['text'], bubble)
        else:
            self._handle_failure(job, engine, result, bubble, next_cursor)
        self.emit({'type': 'usage', **self.usage_payload()})
        self._set_resting_status()
        with self.lock:
            self.wake.notify_all()

    def _set_resting_status(self):
        meeting = self.meeting
        label = self.label_for(self.engine_name)
        if self.blocked:
            self.set_status('blocked', f'{label} is paused')
        elif meeting and meeting.paused:
            self.set_status('paused', 'Paused. Not listening.')
        elif meeting and meeting.ready and not meeting.ending:
            self.set_status('listening', f'{label} is listening')
        else:
            self.set_status('idle', f'{label} is ready')

    def _log_usage(self, kind, engine_name, result):
        usage = result.get('usage') or {}
        row = {'t': time.strftime('%Y-%m-%dT%H:%M:%S'), 'kind': kind, 'engine': engine_name, 'ok': bool(result.get('ok')),
               'err': result.get('kind'), 'elapsed_s': result.get('elapsed_s'), 'first_text_s': result.get('first_text_s'),
               'input': usage.get('input_tokens') or usage.get('prompt_tokens'),
               'cache_read': usage.get('cache_read_input_tokens'), 'cache_write': usage.get('cache_creation_input_tokens'),
               'output': usage.get('output_tokens') or usage.get('completion_tokens')}
        try:
            with open(os.path.join(self.project.app_dir, 'usage.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps(row) + '\n')
        except OSError:
            pass

    def _handle_reply(self, job, engine, text, bubble):
        kind = job['kind']
        if kind == 'compact':
            self.add_feed('Memory tidied.')
            return
        if kind in ('listen', 'meeting_ended'):
            ops = parse_json_reply(text)
            self.apply_ops(ops, kind)
            if kind == 'listen' and not ops and text.strip() and text.strip() != '{}':
                # Nothing to apply, and the next turn covers the same ground, so this is not worth interrupting the user for.
                with open(os.path.join(store.APP_DIR, 'errors.log'), 'a', encoding='utf-8') as f:
                    f.write(time.strftime('%H:%M:%S ') + 'listen reply was not JSON: ' + text[:300].replace('\n', ' ') + '\n')
            if kind == 'meeting_ended':
                engine_opus = self.opus
                if engine.name == 'opus' and engine_opus and engine_opus.context_tokens > COMPACT_AFTER_TOKENS:
                    self.system_jobs.append({'kind': 'compact', 'text': f'/compact Keep the project facts, people, decisions, deadlines, open problems and anything {settings.user} asked you to remember.'})
            return
        answer, ops = split_ops(text)
        asked = job.get('text') if kind == 'user' else job.get('asked')
        if not answer and kind == 'helper_result':
            # The helper's own idea turned out not to matter: say nothing rather than interrupt the user.
            self.emit({'type': 'answer_end', 'id': bubble, 'entry': None})
            self.apply_ops(ops, kind, asked=asked)
            return
        answer = answer or '(No answer came back.)'
        own_idea = kind == 'helper_result' and not asked
        entry = self.add_chat('assistant', answer, engine=engine.name, label=engine.label, bubble=bubble, **({'extra': True} if own_idea else {}))
        self.emit({'type': 'answer_end', 'id': bubble, 'entry': entry})
        handed_over = bool(ops.get('helper') or ops.get('research'))
        self.apply_ops(ops, kind, asked=asked)
        meeting = self.meeting
        if meeting and not handed_over and (asked or '').strip().lower() != 'catch me up':
            one_line = str(ops.get('qa') or first_sentence(answer))
            note_id = f'{"info" if own_idea else "qa"}{len(self.chat)}'
            with self.lock:
                if own_idea:
                    meeting.upsert_note(note_id, 'info', one_line)
                else:
                    meeting.upsert_note(note_id, 'qa', one_line, question=(asked or '')[:140])
                self.last_notes = time.time()
            self.emit({'type': 'notes', 'now': meeting.now, 'notes': meeting.notes_list(), 'changed': [note_id]})

    def _handle_failure(self, job, engine, result, bubble, next_cursor):
        kind, failure = job['kind'], result.get('kind')
        label = engine.label
        answering = kind in ('user', 'helper_result')
        error = result.get('error') or 'Unknown error'
        if failure == 'interrupted':
            if next_cursor:
                self._save_cursor(engine.name, next_cursor)
            if answering:
                partial = split_ops(result.get('text') or '')[0]
                entry = self.add_chat('assistant', (partial + '\n\n(Stopped)').strip(), engine=engine.name, label=engine.label, bubble=bubble)
                self.emit({'type': 'answer_end', 'id': bubble, 'entry': entry})
            return
        if answering:
            self.emit({'type': 'answer_end', 'id': bubble, 'entry': None})
        if failure == 'lost_session' and engine.name == 'opus':
            self.add_feed('The old Opus session could not be found, so a new one takes over from the project record.', 'warn')
            self.project.update_meta(opus_session=str(uuid.uuid4()), opus_started=False)
            self.opus = None
            if kind != 'listen':
                self.user_jobs.appendleft(job) if kind == 'user' else self.system_jobs.appendleft(job)
            return
        if failure in ('unavailable', 'starting'):
            # The model is off (a GPU that is not running, Flock closed) or still starting. Say so at once, offer the
            # other models, and try again by itself every so often: notes wait, the transcript keeps going.
            self.blocked = failure
            self.retry_at = time.time() + (STARTING_RETRY_S if failure == 'starting' else UNAVAILABLE_RETRY_S)
            if kind == 'user':
                self.failed_job = job
            elif kind != 'listen':
                self.system_jobs.appendleft(job)
            actions = [{'id': 'use_opus', 'label': f'Use {engines.OPUS_LABEL}'}] if engine.name != 'opus' else []
            actions += [{'id': 'use_deepseek', 'label': 'Use DeepSeek'}, {'id': 'retry', 'label': 'Try again'}]
            more = ' Notes wait until it answers; the transcript keeps going.' if self.meeting else ''
            self.set_banner('unavailable', error[:300] + more, actions)
            said = error[:160] if error.startswith(label) else f'{label}: {error[:160]}'
            if not any(entry['text'] == said for entry in list(self.feed)[-3:]):
                self.add_feed(said, 'error')
            return
        if failure == 'refused':
            self.blocked = 'refused'
            self.failed_job = job if kind == 'user' else None
            self.set_banner('refused', f'{label} refused this. Its safety filter blocked the last message, so notes are paused.', [
                {'id': 'use_deepseek', 'label': 'Switch to DeepSeek'}, {'id': 'fresh_opus', 'label': 'Fresh Opus session'}]
                if engine.name == 'opus' else [{'id': 'use_opus', 'label': f'Switch to {engines.OPUS_LABEL}'}])
            self.add_feed(f'{label} refused: {error[:200]}', 'error')
            return
        if failure in ('signed_out', 'missing'):
            self.blocked = 'signed_out'
            self.failed_job = job if kind == 'user' else None
            text, button = (engines.SIGN_IN_HELP, 'I have signed in') if failure == 'signed_out' else (engines.MISSING_HELP, 'Try again')
            others = [{'id': 'use_harness', 'label': 'Use the Flock model'}] if harness_link.linked() and self.choice() != 'harness' else []
            self.set_banner('signed_out', text, [{'id': 'retry', 'label': button}, *others, {'id': 'use_deepseek', 'label': 'Use DeepSeek for now'}])
            return
        if failure == 'limit':
            self.blocked = 'limit'
            self.failed_job = job if kind == 'user' else None
            actions = [{'id': 'use_deepseek', 'label': 'Switch to DeepSeek'}, {'id': 'retry', 'label': 'Try again'}] if engine.name == 'opus' else [{'id': 'use_opus', 'label': f'Switch to {engines.OPUS_LABEL}'}, {'id': 'retry', 'label': 'Try again'}]
            self.set_banner('limit', f'{label} has hit its usage limit. {error[:160]}', actions)
            return
        # Crashes, time-outs and network trouble: retry once for answers; listening just tries again shortly.
        self.add_feed(f'{label} had a problem: {error[:160]}', 'error')
        if kind == 'listen':
            self.listen_not_before = time.time() + 20
        elif job.get('retried'):
            self.failed_job = job if kind == 'user' else None
            self.set_banner('trouble', f'{label} could not answer: {error[:160]}', [{'id': 'retry', 'label': 'Try again'}, {'id': 'use_deepseek', 'label': 'Ask DeepSeek'}])
        else:
            job['retried'] = True
            time.sleep(1)
            (self.user_jobs if kind == 'user' else self.system_jobs).appendleft(job)

    def apply_ops(self, ops, kind, asked=None):
        """Apply what the assistant asked for."""
        if not ops:
            return
        changed = []
        with self.lock:
            meeting = self.meeting
            if meeting:
                if isinstance(ops.get('now'), str) and ops['now'].strip():
                    meeting.now = ops['now'].strip()
                    meeting.dirty = True
                    changed.append('now')
                for note in ops.get('notes') or []:
                    if isinstance(note, dict) and note.get('id') and note.get('t'):
                        meeting.upsert_note(str(note['id']), str(note.get('s') or 'points'), str(note['t']))
                        changed.append(str(note['id']))
                for note_id in ops.get('del') or []:
                    if meeting.items.pop(str(note_id), None):
                        meeting.dirty = True
                        changed.append(str(note_id))
                speakers = ops.get('speakers')
                if isinstance(speakers, dict):
                    self._apply_speaker_guess(meeting, speakers)
                if isinstance(ops.get('title'), str) and ops['title'].strip() and not meeting.named_title:
                    meeting.title = ops['title'].strip()[:70]
                    meeting.dirty = True
                    self.emit({'type': 'title', 'title': meeting.title})
                if changed:
                    self.last_notes = time.time()
                    self.emit({'type': 'notes', 'now': meeting.now, 'notes': meeting.notes_list(), 'changed': changed})
        if isinstance(ops.get('state'), str) and ops['state'].strip():
            self.project.write_status(ops['state'])
            self.emit({'type': 'state'})
            if not ops.get('changelog'):
                self.project.log_change('Project state updated.')
        changelog = ops.get('changelog')
        if isinstance(changelog, list):
            for line in changelog:
                self.project.log_change(line)
            if changelog:
                self.emit({'type': 'state'})
                self.add_feed('Project state: ' + str(changelog[0])[:90], 'state')
        if isinstance(ops.get('say'), str) and ops['say'].strip() and time.time() - self.last_nudge >= NUDGE_GAP_S:
            self.last_nudge = time.time()
            self.add_chat('nudge', ops['say'].strip(), engine=self.engine_name, label=self.label_for(self.engine_name))
        if isinstance(ops.get('feed'), str) and ops['feed'].strip():
            self.add_feed(ops['feed'].strip()[:120], 'note')
        helper = ops.get('helper') or ops.get('research')
        if isinstance(helper, str) and helper.strip():
            self.start_helper(helper.strip(), asked=asked, label=ops.get('helper_label'))

    def _apply_speaker_guess(self, meeting, speakers):
        for label, name in speakers.items():
            name = str(name).strip()[:40]
            pid = next((key for key, value in meeting.people.items() if value == label), None)
            if pid and pid not in ('me', 'other') and pid not in meeting.named and name and name != label:
                meeting.people[pid] = name
                meeting.dirty = True
                self.add_feed(f'{label} sounds like {name}. Click a name to change it.', 'note')
                self.emit({'type': 'people', 'people': meeting.people, 'named': sorted(meeting.named)})

    # ---- the helper: one thing at a time, with its own memory, separate from the fast main chat ----
    def helper_state(self):
        run = self.helper
        if not run:
            return None
        return {'brief': helper_label(run), 'status': run['status'], 'own_idea': not run['asked'], 'queued': len(self.helper_queue)}

    def start_helper(self, brief, asked=None, engine=None, label=None):
        """asked is what the user typed. With no asked, this is the assistant's own idea from the transcript."""
        run = {'brief': brief, 'asked': asked, 'label': label, 'status': 'Starting', 'stop': threading.Event(), 'engine': engine or self.engine_name}
        with self.lock:
            if self.helper:
                if asked:
                    self.helper_queue.appendleft(run)   # what the user asked for goes before the helper's own ideas
                else:
                    self.helper_queue.append(run)
                while len(self.helper_queue) > HELPER_QUEUE_MAX:
                    self.helper_queue.pop()
                self.add_feed('Helper is busy, so this waits its turn: ' + helper_label(run), 'helper')
                self.emit({'type': 'helper', 'helper': self.helper_state()})
                return
            self.helper = run
        self._launch_helper(run)

    def _launch_helper(self, run):
        start = 'Helper started: ' if run['asked'] else 'Helper is looking into: '
        self.add_feed(start + helper_label(run), 'helper')
        self.emit({'type': 'helper', 'helper': self.helper_state()})
        threading.Thread(target=self._helper_worker, args=(run,), daemon=True).start()

    def stop_helper(self):
        run = self.helper
        if run:
            run['stop'].set()

    def _opus_helper(self, request, own_idea, on_event, timeout, stop):
        """Runs on the project's helper session, starting a new one if the old one has gone missing."""
        for _ in range(2):
            meta = self.project.meta()
            session_id = meta.get('helper_session')
            if not session_id:
                session_id = str(uuid.uuid4())
                self.project.update_meta(helper_session=session_id, helper_started=False)
            result = engines.run_helper(request, self.project.dir, session_id, bool(meta.get('helper_started')), HELPER_ROLE_FILE, MCP_FILE,
                                        own_idea, on_event, timeout, stop)
            if result.get('kind') != 'lost_session':
                if result.get('kind') not in ('crashed', 'signed_out'):
                    self.project.update_meta(helper_started=True)
                return result
            self.project.update_meta(helper_session=str(uuid.uuid4()), helper_started=False)
        return result

    def _helper_worker(self, run):
        brief, stop, asked = run['brief'], run['stop'], run['asked']

        def on_event(what, value):
            if what == 'tool' and value:
                run['status'] = value
                self.add_feed('Helper: ' + value, 'helper')
                self.emit({'type': 'helper', 'helper': self.helper_state()})

        if run['engine'].startswith('h:'):
            result = self._plain_helper(run)
        elif run['engine'] == 'opus':
            source = 'user' if asked else 'assistant'
            request = f'<request source="{source}">\n' + (f'{settings.User} typed: {asked}\n\n' if asked else '') + f'Brief from the assistant: {brief}\n</request>'
            result = self._opus_helper(request, not asked, on_event, 3600 if asked else 900, stop)
        else:
            run['status'] = 'DeepSeek helper is working'
            self.emit({'type': 'helper', 'helper': self.helper_state()})
            try:
                if not settings.DEEPSEEK_HELPER:
                    raise RuntimeError('no DeepSeek helper is set up on this machine (settings.json "deepseek_helper")')
                import importlib
                done = importlib.import_module(settings.DEEPSEEK_HELPER).run(brief, self.project.dir, model=engines.DEEPSEEK_MODEL, timeout=1200 if asked else 600, web=not asked, full=bool(asked))
                result = {'ok': bool(done.get('ok')), 'text': done.get('text') or '', 'kind': 'other', 'error': done.get('error') or ''}
            except BaseException as exc:
                result = {'ok': False, 'kind': 'other', 'error': f'{type(exc).__name__}: {exc}', 'text': ''}
            if stop.is_set():
                result = {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': ''}
        self._log_usage('helper', run['engine'], result)
        with self.lock:
            self.helper = self.helper_queue.popleft() if self.helper_queue else None
            following = self.helper
        self.emit({'type': 'helper', 'helper': self.helper_state()})
        if result.get('ok'):
            self.add_feed('Helper finished. Passing it to the assistant.', 'helper')
            with self.lock:
                self.system_jobs.appendleft({'kind': 'helper_result', 'text': result['text'], 'question': brief, 'asked': asked})
                self.wake.notify_all()
        elif result.get('kind') == 'interrupted':
            self.add_feed('Helper stopped.', 'helper')
            self.add_chat('system', f'{settings.User} stopped the helper while it worked on: ' + brief[:200])
        else:
            self.add_feed('Helper failed: ' + str(result.get('error'))[:160], 'error')
            self.add_chat('system', 'The helper failed on: ' + brief[:200])
            if result.get('kind') == 'refused' and run['engine'] == 'opus':
                self.failed_helper = (brief, asked, run.get('label'))
                self.set_banner('refused_helper', 'Opus refused the helper\'s work.' + (' DeepSeek can try it.' if engines.deepseek_available() else ''), [{'id': 'helper_deepseek', 'label': 'Try with DeepSeek'}, {'id': 'dismiss', 'label': 'Leave it'}])
        if following:
            self._launch_helper(following)

    def _plain_helper(self, run):
        """The helper for a model reached through Flock: no tools there, so the app reads the project files that match
        the brief and the model answers from them. No web, no commands, no computer use (Claude Opus has those)."""
        model_id = run['engine'][2:]
        label = self.label_for(run['engine'])
        run['status'] = 'Reading the project files'
        self.emit({'type': 'helper', 'helper': self.helper_state()})
        info = harness_link.models().get(model_id) or harness_link.current() or {}
        budget = int(max(4000, min(int(info.get('context') or 128000), 200000) * 0.5) * 3)
        names, text = self._helper_sources(f'{run["asked"] or ""} {run["brief"]}', min(budget, 150000))
        run['status'] = f'{label} is reading ' + (f'{len(names)} project files' if len(names) != 1 else '1 project file')
        self.emit({'type': 'helper', 'helper': self.helper_state()})
        self.add_feed('Helper: ' + run['status'] + ('' if not names else ': ' + ', '.join(os.path.basename(n) for n in names[:4])), 'helper')
        source = 'user' if run['asked'] else 'assistant'
        brief = (f'<brief source="{source}">\n' + (f'{settings.User} typed: {run["asked"]}\n\n' if run['asked'] else '')
                 + f'Brief from the assistant: {run["brief"]}\n</brief>\n\n' + (text or 'No project file matched the brief.'))
        return harness_link.stream(model_id, [{'role': 'system', 'content': prompts.PLAIN_HELPER}, {'role': 'user', 'content': brief}],
                                   stop=run['stop'], timeout=600, effort='medium', max_tokens=3000, session=self._session_tag('-helper'))

    def _helper_sources(self, query, budget):
        """The project files that best match the words of a brief, as text within budget characters: the state always,
        then meeting summaries and transcripts and documents by how often the brief's words appear in them."""
        terms = [t for t in dict.fromkeys(re.findall(r'[a-z0-9][a-z0-9-]{3,}', (query or '').lower())) if t not in HELPER_STOP_WORDS][:16]
        project = self.project
        candidates = [('State/status.md', 'Project state')]
        for meeting in project.meetings()[:40]:
            for name in ('summary.md', 'transcript.md'):
                if name in meeting['files']:
                    candidates.append((f'Meetings/{meeting["id"]}/{name}', f'{meeting["title"]} {meeting["when"]}'))
        candidates += [(item['path'], item['name']) for item in project.project_files()[:300]]
        scored = []
        for rel, title in candidates:
            text = store.readable_text(os.path.join(project.dir, rel))
            if not text.strip():
                continue
            low, head = text.lower(), f'{rel} {title}'.lower()
            score = sum(min(low.count(t), 20) + 10 * (t in head) for t in terms)
            if rel == 'State/status.md':
                score += 10000
            if score:
                scored.append((score, rel, text))
        scored.sort(key=lambda item: -item[0])
        scored = scored[:8]
        pieces, names = [], []
        for index, (score, rel, text) in enumerate(scored):
            share = max(1500, (budget - sum(len(p) for p in pieces)) // (len(scored) - index))
            if len(text) > share:
                keep, size = [], 0
                lines = text.splitlines()
                hits = [i for i, line in enumerate(lines) if any(t in line.lower() for t in terms)] or list(range(len(lines)))
                wanted = sorted({j for i in hits for j in range(max(0, i - 2), min(len(lines), i + 3))})
                for j in wanted:
                    if size + len(lines[j]) > share:
                        break
                    keep.append(lines[j])
                    size += len(lines[j]) + 1
                text = '\n'.join(keep) + '\n(only the parts that match the brief)'
            pieces.append(f'<file path="{rel}">\n{text}\n</file>')
            names.append(rel)
        return names, '\n\n'.join(pieces)

    def helper_with_deepseek(self):
        failed, self.failed_helper = self.failed_helper, None
        self.clear_banner(('refused_helper',))
        if failed:
            self.start_helper(failed[0], asked=failed[1], engine='deepseek', label=failed[2])

    # ---- meetings ----
    def start_meeting(self, title='', mic=None):
        with self.lock:
            if self.meeting:
                raise ValueError('A meeting is already running.')
            title = (title or '').strip()
            when = store.now_stamp()
            folder = self.project.new_meeting_folder(title)
            meeting = LiveMeeting(self.project, folder, title or 'Meeting', when)
            meeting.named_title = bool(title)
            self.meeting = meeting
            self.last_activity = time.time()
            self.project.update_meeting(folder, title=meeting.title, when=when)
            self.project.update_meta(used=when, last_mic=(mic or {}).get('name', ''))
            meeting.flush()
            self.add_chat('system', f'Meeting started: "{meeting.title}" at {when}.')
            importer.make_way_for_live()        # a recording being transcribed gives the GPU back, and carries on afterwards
            self.clear_banner()
            self._start_capture(title, mic)
            self.emit({'type': 'meeting', 'live': meeting.snapshot()})
            self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})
            self.set_status('starting', 'Starting Whisper')

    def restart_capture(self):
        with self.lock:
            meeting = self.meeting
            if not meeting:
                return
            meeting.offset = (meeting.lines[-1]['end'] + 2) if meeting.lines else meeting.offset
            meeting.ready, meeting.paused, meeting.ending = False, False, False
            meeting.loading = 'Starting'
            importer.make_way_for_live()
            self.clear_banner()
            self._start_capture(meeting.title, None)
            self.emit({'type': 'meeting', 'live': meeting.snapshot()})

    def _start_capture(self, title, mic):
        try:
            self.capture = Capture(self._on_capture, title, mic)
        except OSError as exc:
            self.capture = None
            self.set_banner('capture', f'Transcription could not start: {exc}. Everything so far is saved.',
                            [{'id': 'restart_capture', 'label': 'Try again'}, {'id': 'finish_meeting', 'label': 'Finish the meeting'}])

    def _on_capture(self, event):
        kind = event.get('ev')
        meeting = self.meeting
        if kind == 'level':
            self.emit({'type': 'level', 'mic': event.get('mic', 0), 'system': event.get('system', 0)})
            return
        if meeting is None:
            return
        if kind == 'segments':
            with self.lock:
                new, changed = meeting.add_rows(event.get('items'), event.get('removed'))
                if new or changed:
                    self.emit({'type': 'lines', 'lines': new, 'people': meeting.people, 'all': meeting.lines if changed else None})
                    self.wake.notify_all()
        elif kind == 'preview':
            self.emit({'type': 'preview', 'channel': event.get('channel'), 'text': event.get('text') or '', 'final': event.get('final')})
        elif kind == 'loading':
            meeting.loading = event.get('text') or ''
            self.emit({'type': 'meeting_status', 'loading': meeting.loading, 'ready': meeting.ready})
        elif kind == 'ready':
            with self.lock:
                if not meeting.lines:
                    meeting.started = time.time()
                meeting.ready = True
                meeting.mic_name = event.get('mic') or ''
                meeting.loading = ''
            self.add_feed(f'Whisper is listening. Microphone: {meeting.mic_name or "default"}.', 'note')
            self.emit({'type': 'meeting', 'live': meeting.snapshot()})
            self._set_resting_status()
        elif kind == 'paused':
            meeting.paused = bool(event.get('paused'))
            self.emit({'type': 'meeting_status', 'paused': meeting.paused})
            self._set_resting_status()
        elif kind == 'error':
            message = str(event.get('message') or event.get('code') or 'Unknown error')
            self.add_feed('Transcription problem: ' + message[:200], 'error')
            if event.get('code') in ('start_failed', 'asr_unavailable'):
                self.set_banner('capture', 'Transcription could not start: ' + message[:200], [{'id': 'restart_capture', 'label': 'Try again'}, {'id': 'finish_meeting', 'label': 'Finish the meeting'}])
        elif kind == 'exit':
            if meeting.ending:
                threading.Thread(target=self._finish_meeting, args=(meeting,), daemon=True).start()
            else:
                started = meeting.ready
                meeting.ready = False
                self.emit({'type': 'meeting', 'live': meeting.snapshot()})
                if started:
                    text = 'Transcription stopped unexpectedly. Everything so far is saved.'
                elif (self.banner or {}).get('kind') == 'capture':
                    text = None                 # it already said why it could not start
                else:
                    tail = capture_log_tail()
                    text = 'Transcription could not start' + (f': {tail}' if tail else '.') + ' Everything so far is saved.'
                if text:
                    self.set_banner('capture', text[:400], [{'id': 'restart_capture', 'label': 'Keep listening'}, {'id': 'finish_meeting', 'label': 'Finish the meeting'}])
                self._set_resting_status()

    def pause_meeting(self, paused):
        if self.capture and self.meeting:
            self.capture.command('pause' if paused else 'resume')

    def end_meeting(self):
        with self.lock:
            meeting = self.meeting
            if not meeting or meeting.ending:
                return
            meeting.ending = True
            meeting.ready = False
            self.emit({'type': 'meeting_status', 'ending': True, 'loading': 'Finishing the last lines'})
            self.set_status('thinking', 'Finishing the last lines')
            capture = self.capture
            if capture is None or capture.proc.poll() is not None:
                threading.Thread(target=self._finish_meeting, args=(meeting,), daemon=True).start()
                return
            capture.command('end')

            def watchdog():
                try:
                    capture.proc.wait(timeout=240)
                except subprocess.TimeoutExpired:
                    capture.kill()

            threading.Thread(target=watchdog, daemon=True).start()

    def _finish_meeting(self, meeting):
        """Save everything, leave live mode, and have the proper summary written in the background."""
        with self.lock:
            if self.meeting is not meeting:
                return
            self.meeting = None
            self.capture = None
            duration = int((meeting.lines[-1]['end'] if meeting.lines else 0))
            people = sorted({meeting.label(line['pid']) for line in meeting.lines})
            folder = meeting.folder
            try:
                os.remove(os.path.join(self.project.app_dir, 'live.json'))
            except OSError:
                pass
            if not meeting.lines:
                store.remove_project_tree_guard(meeting.dir)         # nothing was said: leave no clutter behind
                with self.project.lock:
                    meta = self.project.meta()
                    (meta.get('meetings') or {}).pop(folder, None)
                    self.project.update_meta(meetings=meta.get('meetings') or {})
                self.add_chat('system', 'The meeting ended with nothing said, so it was not saved.')
                self.emit({'type': 'meeting', 'live': None})
                self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})
                self._set_resting_status()
                return
            meeting.flush()
            try:
                os.remove(os.path.join(self.project.app_dir, 'live.json'))
            except OSError:
                pass
            folder = self._rename_meeting(folder, meeting.title)
            self.project.update_meeting(folder, title=meeting.title, when=meeting.when, duration_s=duration, people=people)
            known = list(dict.fromkeys((self.project.meta().get('people') or []) + [p for p in people if p not in ('Me', 'Others') and not p.startswith('Person ')]))
            self.project.update_meta(people=known)
            self.add_chat('system', f'Meeting ended: "{meeting.title}", {max(1, round(duration / 60))} minutes, people: {", ".join(people)}.')
        self.emit({'type': 'meeting', 'live': None, 'ended': folder})
        self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})
        self._set_resting_status()
        self.make_summary(folder, final=True)

    def _rename_meeting(self, folder, title):
        """Give the folder the meeting's real title once it is known."""
        match = re.match(r'(\d{4}-\d\d-\d\d \d{4}) ', folder)
        clean = store.clean_name(title, 50)
        if not match or not clean:
            return folder
        target = f'{match[1]} {clean}'
        if target == folder or os.path.exists(self.project.meeting_dir(target)):
            return folder
        try:
            os.rename(self.project.meeting_dir(folder), self.project.meeting_dir(target))
        except OSError:
            return folder
        self.project.rename_meeting_key(folder, target)
        return target

    def rename_speaker(self, pid, name):
        name = store.clean_name(name, 40)
        with self.lock:
            meeting = self.meeting
            if not meeting or not name or pid in ('me',):
                return
            old = meeting.label(pid)
            meeting.people[pid] = name
            meeting.named.add(pid)
            meeting.dirty = True
            self.add_chat('system', f'{settings.User} says that {old} is {name}.')
            self.emit({'type': 'people', 'people': meeting.people, 'named': sorted(meeting.named)})

    def rename_stored_speaker(self, folder, old, new):
        """The user corrects a name in a finished meeting's transcript, for example a voice the import named wrongly."""
        old, new = (old or '').strip(), store.clean_name(new, 50).replace(':', ' ').strip()
        if not folder or not old or not new or old == new or old == 'On screen':
            return
        if self.meeting and self.meeting.folder == folder:
            raise ValueError('This meeting is still live: click the name in the live transcript instead.')
        if new.lower() in ('me', settings.NAME.lower() or 'me'):
            new = 'Me'
        path = os.path.join(self.project.meeting_dir(folder), 'transcript.md')
        text = store.read_text(path)
        if not text:
            raise ValueError('That meeting has no transcript.')
        pattern = re.compile(r'^(\[[\d:]+\] )' + re.escape(old) + r': ', re.M)
        changed, count = pattern.subn(lambda m: f'{m[1]}{new}: ', text)
        if not count:
            return
        info = self.project.meeting_meta(folder)
        people = [new if p == old else p for p in info.get('people') or []]
        people = sorted(set(people), key=lambda p: (p != 'Me', p))
        changed = re.sub(r'^(.*\. People: )(.*)\.$', lambda m: m[1] + ', '.join(people) + '.', changed, count=1, flags=re.M)
        store.atomic_write(path, changed)
        self.project.update_meeting(folder, people=people)
        if new != 'Me' and not new.startswith('Speaker '):
            self.project.update_meta(people=list(dict.fromkeys((self.project.meta().get('people') or []) + [new])))
        self.add_chat('system', f'{settings.User} says that "{old}" in the meeting "{info.get("title")}" is {new}. The transcript now says so.')
        self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})

    def rename_meeting_live(self, title):
        with self.lock:
            meeting = self.meeting
            title = (title or '').strip()[:70]
            if meeting and title:
                meeting.title = title
                meeting.named_title = True
                meeting.dirty = True
                self.emit({'type': 'title', 'title': title})

    # ---- the proper summary, at the end or on request ----
    def summary_engine(self):
        """The proper summary: the model this project follows in Flock, else Claude Opus 5.5 on extra high as before."""
        name = self.engine_name
        return name if name.startswith('h:') else 'opus'

    def make_summary(self, folder=None, final=False, engine=None):
        engine = engine or self.summary_engine()
        meeting = self.meeting
        folder = folder or (meeting.folder if meeting else None)
        if not folder:
            raise ValueError('There is no meeting to summarise.')
        with self.lock:
            if self.summaries.get(folder) == 'writing':
                return
            self.summaries[folder] = 'writing'
        self.project.update_meeting(folder, summary_status='writing')
        label = f'{engines.OPUS_LABEL} on extra high' if engine == 'opus' else self.label_for(engine)
        self.add_feed(f'Writing the proper summary with {label}. This can take a few minutes.', 'summary')
        self.emit({'type': 'summary', 'folder': folder, 'status': 'writing', 'engine': engine, 'label': self.label_for(engine)})
        self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})
        threading.Thread(target=self._summary_worker, args=(folder, final, engine), daemon=True).start()

    def _summary_prompt(self, folder, transcript=None):
        project = self.project
        meeting = self.meeting if self.meeting and self.meeting.folder == folder else None
        base = project.meeting_dir(folder)
        if meeting:
            meeting.flush()
        info = project.meeting_meta(folder)
        people = meeting and sorted({meeting.label(line['pid']) for line in meeting.lines}) or info.get('people') or []
        people = [p for p in people if p != 'Others']
        people_text = ('In this meeting: ' + ', '.join(people) + '. ' if people else '') + f'"Me" is {settings.user}.'
        known = [p for p in (project.meta().get('people') or []) if p not in people]
        if known:
            people_text += ' Names from earlier meetings in this project, which may help identify speakers: ' + ', '.join(known) + '.'
        duration = int(meeting.lines[-1]['end']) if meeting and meeting.lines else info.get('duration_s') or 0
        title = meeting.title if meeting else info.get('title') or 'Meeting'
        note = f'Working title: {title}.' if title not in ('Meeting', 'Recorded meeting') else 'No title yet.'
        if info.get('source') == 'recording':
            note += (f' Nobody heard this meeting live: {settings.user} imported a recording of it, so work out dates such as "next Friday" from '
                     'the date it started, not from today. Who said what was worked out from their voices and can be wrong in places. '
                     'Lines from "On screen" say what was shown on screen at that moment.')
            if '## How to' in store.read_text(os.path.join(base, 'live-summary.md')):
                note += (' The rough notes have a How to section: steps someone showed on screen for getting to the files and systems '
                         'the work needs. For this meeting, add one more heading after Work by person: "## How to", with those steps '
                         'as numbered lists under a ### heading each, keeping the exact names of sites, menus, buttons, drives, folders '
                         'and files, and checked against the transcript and the "On screen" lines.')
        return prompts.SUMMARY.format(state=project.read_status(), people=people_text, project=project.name,
                                      when=info.get('when') or '', length=f'{max(1, round(duration / 60))} minutes ({clock(duration)})', title_note=note,
                                      notes=store.read_text(os.path.join(base, 'live-summary.md')),
                                      transcript=store.read_text(os.path.join(base, 'transcript.md')) if transcript is None else transcript)

    def _summary_worker(self, folder, final, engine):
        prompt = self._summary_prompt(folder)
        if engine == 'opus':
            result = engines.oneshot_claude(prompt, engines.SUMMARY_EFFORT, self.project.dir, timeout=2400)
        elif engine.startswith('h:'):
            result = self._harness_summary(folder, engine[2:], prompt)
        else:
            # 'high', not 'max': measured on 2026-11-05, 'max' spent the whole token allowance reasoning and wrote nothing.
            result = engines.stream_deepseek([{'role': 'user', 'content': prompt}], effort='high', max_tokens=32000, timeout=1200)
        if result.get('ok') and not (result.get('text') or '').strip():
            result = {'ok': False, 'kind': 'other', 'error': 'The model finished without writing anything.'}
        self.summaries.pop(folder, None)
        usage = result.get('usage') or {}
        try:
            with open(os.path.join(self.project.app_dir, 'usage.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps({'t': time.strftime('%Y-%m-%dT%H:%M:%S'), 'kind': 'summary', 'engine': engine, 'ok': bool(result.get('ok')),
                                    'elapsed_s': result.get('elapsed_s'), 'input': usage.get('input_tokens') or usage.get('prompt_tokens'),
                                    'cache_read': usage.get('cache_read_input_tokens'), 'output': usage.get('output_tokens') or usage.get('completion_tokens')}) + '\n')
        except OSError:
            pass
        info = self.project.meeting_meta(folder)
        if not result.get('ok'):
            error = str(result.get('error'))[:200]
            self.project.update_meeting(folder, summary_status='failed')
            self.add_feed(f'The summary failed: {error}', 'error')
            self.emit({'type': 'summary', 'folder': folder, 'status': 'failed', 'error': error})
            self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})
            others = [{'id': f'summary_opus:{folder}', 'label': f'Write it with {engines.OPUS_LABEL}'}] if engine != 'opus' else []
            self.set_banner('summary', f'The proper summary could not be written. {error}', [
                {'id': f'summary_retry:{folder}', 'label': 'Try again'}, *others, {'id': f'summary_deepseek:{folder}', 'label': 'Write it with DeepSeek'}])
            if final and not info.get('state_done'):
                self.project.update_meeting(folder, state_done=True)
                self._queue_state_update(folder, store.read_text(os.path.join(self.project.meeting_dir(folder), 'live-summary.md')))
            return
        text = result['text'].strip()
        self.clear_banner(('summary',))
        meeting = self.meeting if self.meeting and self.meeting.folder == folder else None
        store.atomic_write(os.path.join(self.project.meeting_dir(folder), 'summary.md'), text + '\n')
        heading = re.match(r'#\s+(.+)', text)
        if not meeting and heading and info.get('title') in (None, '', 'Meeting'):
            title = heading[1].strip()[:70]
            new_folder = self._rename_meeting(folder, title)
            self.project.update_meeting(new_folder, title=title)
            folder = new_folder
        self.project.update_meeting(folder, summary_status='done')
        self.add_feed('The proper summary is ready.', 'summary')
        self.emit({'type': 'summary', 'folder': folder, 'status': 'done'})
        self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})
        if not meeting and not self.project.meeting_meta(folder).get('state_done'):
            self.project.update_meeting(folder, state_done=True)
            self._queue_state_update(folder, text)
        elif not meeting:
            self.add_chat('system', f'The summary of "{self.project.meeting_meta(folder).get("title")}" is now in Meetings/{folder}/summary.md.')

    def _harness_summary(self, folder, model_id, prompt):
        """The proper summary from a model reached through Flock. A transcript too long for its context is read in parts
        first: notes from each part, in order, stand in for the transcript."""
        info = harness_link.models().get(model_id) or harness_link.current() or {}
        context = min(int(info.get('context') or 128000), 200000)
        max_out = min(16000, int(info.get('max_output') or 16384))
        budget = int(max(4000, context * 0.8 - max_out) * engines.HarnessEngine.CHARS_PER_TOKEN)
        session = self._session_tag('-summary')
        t0 = time.time()
        if len(prompt) > budget:
            transcript = store.read_text(os.path.join(self.project.meeting_dir(folder), 'transcript.md'))
            rest = max(0, len(prompt) - len(transcript))
            size = max(3000, int((budget - 4000) * 0.8))
            parts, current = [], []
            for line in transcript.splitlines():
                if current and sum(len(x) + 1 for x in current) + len(line) > size:
                    parts.append('\n'.join(current))
                    current = []
                current.append(line[:size])
            if current:
                parts.append('\n'.join(current))
            label = self.label_for('h:' + model_id)
            notes = []
            for number, part in enumerate(parts, 1):
                self.add_feed(f'Summary: {label} is reading part {number} of {len(parts)} of the transcript.', 'summary')
                reply = harness_link.stream(model_id, [{'role': 'user', 'content': prompts.CHUNK_NOTES.format(part=number, parts=len(parts), text=part)}],
                                            timeout=900, max_tokens=min(3000, max_out), session=session)
                if not reply.get('ok'):
                    return reply
                notes.append(f'### Part {number} of {len(parts)}\n{reply["text"].strip()}')
            condensed = ('(The transcript was too long to read in one go, so these are detailed notes made from it part by part, '
                         'in order. Work from them as you would from the transcript.)\n\n' + '\n\n'.join(notes))
            prompt = self._summary_prompt(folder, transcript=condensed[:max(4000, budget - rest)])
        result = harness_link.stream(model_id, [{'role': 'user', 'content': prompt}], effort='high', max_tokens=max_out,
                                     timeout=1500, session=session)
        result['elapsed_s'] = round(time.time() - t0, 1)
        return result

    def _queue_state_update(self, folder, text):
        with self.lock:
            title = self.project.meeting_meta(folder).get('title') or folder
            self.add_chat('system', f'Summary of the meeting "{title}" is in Meetings/{folder}/summary.md.')
            self.system_jobs.append({'kind': 'meeting_ended', 'text': text, 'title': title, 'folder': folder})
            self.wake.notify_all()

    # ---- background chores ----
    def _tick(self):
        last_flush = 0.0
        last_files = 0.0
        while not self.closed:
            time.sleep(1)
            now = time.time()
            with self.lock:
                self.wake.notify_all()
                meeting = self.meeting
                if meeting and meeting.dirty and now - last_flush >= 3:
                    try:
                        meeting.flush()
                    except OSError:
                        pass
                    last_flush = now
            if now - last_files >= 5:
                last_files = now
                try:
                    signature = self.project.signature()
                    if signature != self.file_signature:
                        first = self.file_signature == 0
                        self.file_signature = signature
                        if not first:
                            self.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})
                except OSError:
                    pass
            if self.blocked in ('unavailable', 'starting') and now >= self.retry_at:
                with self.lock:
                    # Try again quietly: the banner stays until an answer comes. A question waiting for a model that was
                    # starting goes again too; one for a model that is off waits for Try again or another model.
                    if self.blocked == 'starting' and self.failed_job:
                        self.user_jobs.appendleft(self.failed_job)
                        self.failed_job = None
                    self.blocked = None
                    self.wake.notify_all()
            opus = self.opus
            if opus and not self.meeting and self.current is None and opus.alive() and now - opus.last_used > OPUS_IDLE_CLOSE_S:
                opus.close()

    def do_action(self, action):
        """The buttons on banners."""
        if action == 'use_deepseek':
            self.set_engine('deepseek')
        elif action == 'use_opus':
            self.set_engine('opus')
        elif action == 'use_harness':
            self.set_engine('harness')
        elif action == 'fresh_opus':
            self.fresh_opus()
        elif action == 'retry':
            with self.lock:
                self.blocked = None
                self.clear_banner()
                failed, self.failed_job = self.failed_job, None
                if failed:
                    self.user_jobs.appendleft(failed)
                self.wake.notify_all()
        elif action == 'dismiss':
            self.clear_banner()
        elif action == 'restart_capture':
            self.restart_capture()
        elif action == 'finish_meeting':
            self.clear_banner()
            self.end_meeting()
        elif action == 'helper_deepseek':
            self.helper_with_deepseek()
        elif action.startswith('summary_retry:'):
            self.clear_banner(('summary',))
            self.make_summary(action.split(':', 1)[1])
        elif action.startswith('summary_opus:'):
            self.clear_banner(('summary',))
            self.make_summary(action.split(':', 1)[1], engine='opus')
        elif action.startswith('summary_deepseek:'):
            self.clear_banner(('summary',))
            self.make_summary(action.split(':', 1)[1], engine='deepseek')
        else:
            raise ValueError('Unknown action.')

    def close(self):
        self.closed = True
        with self.lock:
            self.wake.notify_all()
        if self.opus:
            self.opus.close()
        if self.capture:
            self.capture.command('end')
