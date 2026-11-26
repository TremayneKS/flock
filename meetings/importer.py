"""Importing a recorded meeting. It becomes a meeting like any other: its three files, a proper summary, and an update to
the project state. The recording itself goes in Files/Recordings, so the meeting folder still has only its three files.

The steps, each shown in the app as it goes:
  copy      the recording is copied into Files/Recordings, when it came from a path rather than through the browser
  words     Whisper on the GPU, in import_host.py. It waits while a live meeting has the GPU, and gives way if one starts.
  voices    the voices told apart (one audio track has everyone mixed together)
  pictures  frames from the video: each change on screen, and each voice while it talks
  reading   Opus 5.5 reads the transcript and the pictures: names the voices, notes what was shown, writes the rough notes
  summary   the proper summary, then the project state update, exactly as at the end of a live meeting
Work files live in .copilot/imports/<id>, so a retry carries on where it stopped and Whisper never runs twice for one file.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid

import engines
import prompts
import settings
import store

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.path.join(HERE, 'import_host.py')
GPU = threading.Lock()                  # one Whisper at a time on the graphics card, across every project
_hosts = []                             # import processes still holding the GPU, so a live meeting can stop them
_hosts_lock = threading.Lock()
_live_since = [0.0]                     # when a live meeting last started, so an import never slips in as it loads
# Names in a recording that mean the user: "me", plus the user's name and aliases from settings.json.
USER_NAMES = re.compile('|'.join(['me'] + [re.escape(settings.NAME)] * bool(settings.NAME) + settings.ALIASES), re.I)


class Failed(Exception):
    pass


class Cancelled(Exception):
    pass


def make_way_for_live():
    """A live meeting is starting: stop any import that is still transcribing. It starts again by itself afterwards."""
    with _hosts_lock:
        _live_since[0] = time.time()
        for host in _hosts:
            host['preempted'] = True
            try:
                host['proc'].kill()
            except OSError:
                pass


def clock(seconds):
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def parse_clock(text):
    try:
        seconds = 0.0
        for part in str(text).strip().split(':'):
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return None


def normalise_when(text):
    """'2026-11-03T14:00' or '2026-11-03 14:00' to '2026-11-03 14:00'; '' when it is not a date."""
    match = re.match(r'(\d{4}-\d\d-\d\d)[T ](\d\d):(\d\d)', str(text or '').strip())
    return f'{match[1]} {match[2]}:{match[3]}' if match else ''


def nice_when(when):
    try:
        moment = datetime.datetime.strptime(when, '%Y-%m-%d %H:%M')
    except ValueError:
        return when
    return moment.strftime('%a ') + str(moment.day) + moment.strftime(' %b %Y, %H:%M')


def people_count(hint):
    """How many names the user listed, when the note is a list of names."""
    parts = [p for p in re.split(r',|;|\band\b|&|\n', hint or '') if p.strip()]
    return len(parts) if 2 <= len(parts) <= 15 else 0


def merge_lines(phrases, gap=2.5, longest=45):
    """Phrases into transcript lines: one person's run of speech stays together, up to about 45 seconds a line."""
    lines = []
    for phrase in phrases:
        last = lines[-1] if lines else None
        if last and last['who'] == phrase['who'] and phrase['s'] - last['e'] <= gap and phrase['e'] - last['s'] <= longest:
            last['e'] = phrase['e']
            last['text'] += ' ' + phrase['text']
        else:
            lines.append(dict(phrase))
    return lines


def clean_person(name):
    name = re.sub(r'\s+', ' ', str(name or '').replace(':', ' ')).strip()[:50]
    return 'Me' if USER_NAMES.fullmatch(name) else name


class Importer:
    """One project's imports, done one at a time, earliest meeting first so the project state builds up in order."""

    def __init__(self, session):
        self.session = session
        self.project = session.project
        self.wake = threading.Condition()
        self.stops = {}
        self.current = None             # the import id being worked on
        self.last_emit = 0.0
        for folder, info in (self.project.meta().get('meetings') or {}).items():
            job = info.get('import') or {}
            if job.get('status') in ('running', 'waiting'):
                self._set(folder, status='queued', text='Carrying on where it stopped')
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- starting, stopping ----
    def add(self, recording, title='', when='', hint='', modified=None, copy_from=None, copied=True):
        """recording is project-relative (Files/Recordings/...). Returns the new meeting's folder."""
        title = re.sub(r'\s+', ' ', title or '').strip()[:70]
        given = normalise_when(when)
        guess = ''
        if modified:
            try:
                guess = time.strftime('%Y-%m-%d %H:%M', time.localtime(float(modified) / 1000))
            except (ValueError, OverflowError, OSError):
                guess = ''
        when = given or guess or store.now_stamp()
        folder = self.project.new_meeting_folder(title or 'Recorded meeting', stamp=when.replace(':', ''))
        job = {'id': uuid.uuid4().hex[:12], 'status': 'queued', 'step': 'copy' if copy_from else 'words', 'text': 'Waiting to start',
               'hint': (hint or '').strip()[:500], 'from': copy_from, 'copied': copied, 'when_given': bool(given),
               'modified': modified, 'named': bool(title), 'added': time.time()}
        self.project.update_meeting(folder, title=title or 'Recorded meeting', when=when, source='recording', recording=recording,
                                    **{'import': job})
        self.project.update_meta(used=store.now_stamp())
        self.session.add_feed(f'Importing the recording {os.path.basename(recording)}.', 'summary')
        self._emit_meetings()
        with self.wake:
            self.wake.notify_all()
        return folder

    def cancel(self, folder):
        job = (self.project.meeting_meta(folder).get('import') or {})
        if not job or job.get('status') == 'done':
            raise ValueError('That meeting is not being imported.')
        if self.current == job['id']:
            self.stops.setdefault(job['id'], threading.Event()).set()       # the worker tidies up once it has stopped
        else:
            self._remove(folder, job)

    def retry(self, folder):
        job = (self.project.meeting_meta(folder).get('import') or {})
        if job.get('status') != 'failed':
            return
        self._set(folder, status='queued', text='Waiting to start', error='')
        with self.wake:
            self.wake.notify_all()

    def work_dir(self, job):
        return os.path.join(self.project.app_dir, 'imports', job['id'])

    def _remove(self, folder, job):
        """Cancel: nothing left behind, not even the copy of the recording if the app made it."""
        info = self.project.meeting_meta(folder)
        try:
            store.remove_project_tree_guard(self.project.meeting_dir(folder))
        except ValueError:
            pass
        shutil.rmtree(self.work_dir(job), ignore_errors=True)
        if job.get('copied') and info.get('recording'):
            try:
                os.remove(self.project.safe_path(info['recording']))
            except (OSError, ValueError):
                pass
        with self.project.lock:
            meetings = self.project.meta().get('meetings') or {}
            meetings.pop(folder, None)
            self.project.update_meta(meetings=meetings)
        self.session.add_feed('Import cancelled.', 'summary')
        self._emit_meetings()

    # ---- telling the app ----
    def _set(self, folder, quiet=False, **changes):
        with self.project.lock:
            info = (self.project.meta().get('meetings') or {}).get(folder)
            if info is None:
                return
            job = info.get('import') or {}
            job.update(changes)
            self.project.update_meeting(folder, **{'import': job})
        if quiet and time.time() - self.last_emit < 0.8:
            return
        self.last_emit = time.time()
        self.session.emit({'type': 'import', 'folder': folder, 'import': job})

    def _emit_meetings(self):
        self.session.emit({'type': 'meetings', 'meetings': self.project.meetings(), 'files': self.project.file_tree()})

    def _fail(self, folder, message):
        self._set(folder, status='failed', error=message[:400], text='Stopped')
        self.session.add_feed('The import stopped: ' + message[:160], 'error')
        self._emit_meetings()

    # ---- the work ----
    def _next(self):
        meetings = self.project.meta().get('meetings') or {}
        waiting = sorted((info.get('when') or '', folder) for folder, info in meetings.items()
                         if (info.get('import') or {}).get('status') == 'queued')
        return waiting[0][1] if waiting else None

    def _loop(self):
        while not self.session.closed:
            folder = self._next()
            if folder is None:
                with self.wake:
                    self.wake.wait(5)
                continue
            job = self.project.meeting_meta(folder).get('import') or {}
            self.current = job.get('id')
            stop = self.stops.setdefault(job['id'], threading.Event())
            try:
                self._process(folder, job, stop)
            except Cancelled:
                self._remove(self._folder_of(job['id']) or folder, job)
            except Failed as exc:
                self._fail(self._folder_of(job['id']) or folder, str(exc))
            except Exception as exc:    # noqa: BLE001
                with open(os.path.join(store.APP_DIR, 'errors.log'), 'a', encoding='utf-8') as f:
                    f.write(time.strftime('%H:%M:%S ') + 'import: ' + traceback.format_exc() + '\n')
                self._fail(self._folder_of(job['id']) or folder, f'{type(exc).__name__}: {exc}')
            finally:
                self.current = None
                self.stops.pop(job.get('id'), None)

    def _folder_of(self, job_id):
        for folder, info in (self.project.meta().get('meetings') or {}).items():
            if (info.get('import') or {}).get('id') == job_id:
                return folder
        return None

    def _process(self, folder, job, stop):
        work = self.work_dir(job)
        os.makedirs(work, exist_ok=True)
        self._set(folder, status='running', error='')
        if job.get('from') and job.get('step') == 'copy':
            self._copy(folder, job, stop)
        folder, result = self._transcribe(folder, job, stop)
        reading = self._read(folder, job, result, stop)
        folder = self._write(folder, job, result, reading)
        if reading.get('failed'):
            raise Failed('Claude could not read the meeting: ' + reading['failed'][:200] + ' The transcript is saved.')
        self._set(folder, status='done', step='summary', text='Done', finished=time.time())
        info = self.project.meeting_meta(folder)
        people = ', '.join(p for p in info.get('people') or [] if p != 'Me') or 'not known'
        self.session.add_chat('system', f'{settings.User} imported a recorded meeting: "{info.get("title")}", held {nice_when(info.get("when", ""))}, '
                                        f'{max(1, round((info.get("duration_s") or 0) / 60))} minutes, with {people}. Its files are in Meetings/{folder}/ '
                                        f'and the recording is {info.get("recording")}.')
        self._emit_meetings()
        self.session.make_summary(folder, final=True)

    def _copy(self, folder, job, stop):
        info = self.project.meeting_meta(folder)
        source, target = job['from'], self.project.safe_path(info['recording'])
        if not os.path.isfile(source):
            raise Failed(f'The recording is no longer at {source}.')
        total = os.path.getsize(source)
        self._set(folder, step='copy', text='Copying the recording into the project', done=0, total=total)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        part = target + '.part'
        done = 0
        with open(source, 'rb') as src, open(part, 'wb') as dst:
            while True:
                if stop.is_set():
                    raise Cancelled()
                chunk = src.read(4 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                self._set(folder, quiet=True, done=done, total=total)
        os.replace(part, target)
        self._set(folder, step='words', done=None, total=None)

    def _whisper_prompt(self, job):
        """A few words that help Whisper spell names and terms right."""
        meta = self.project.meta()
        bits = [self.project.name, meta.get('description', ''), ', '.join((meta.get('people') or [])[:12]), job.get('hint', '')]
        return re.sub(r'\s+', ' ', '. '.join(b.strip().rstrip('.') for b in bits if b and b.strip()))[:300]

    def _transcribe(self, folder, job, stop):
        import live
        work = self.work_dir(job)
        result_path = os.path.join(work, 'result.json')
        if os.path.isfile(result_path) and job.get('step') in ('reading', 'summary'):
            with open(result_path, encoding='utf-8') as f:
                return folder, json.load(f)
        try:
            os.remove(result_path)
        except OSError:
            pass
        recording = self.project.safe_path(self.project.meeting_meta(folder)['recording'])
        count = people_count(job.get('hint'))
        while True:
            told = ''
            while True:                 # the GPU, once no live meeting is using it
                if stop.is_set():
                    raise Cancelled()
                meeting = live.capture_running() or time.time() - _live_since[0] <= 10
                if not meeting and GPU.acquire(timeout=1):
                    if not live.capture_running():
                        break
                    GPU.release()
                    meeting = True
                busy = 'your live meeting is using the graphics card' if meeting else 'another recording is being transcribed'
                if busy != told:
                    told = busy
                    self._set(folder, status='waiting', step='words', text=f'Waiting: {busy}. It starts by itself.')
                time.sleep(1)
            self._set(folder, status='running', step='words', text='Starting Whisper', done=None, total=None)
            cmd = [live.VENV_PYTHON, '-X', 'utf8', HOST, recording, work, '--prompt', self._whisper_prompt(job)]
            if count:
                cmd += ['--people', str(count)]
            log = open(os.path.join(work, 'host.log'), 'a', encoding='utf-8', errors='replace')
            try:
                proc = subprocess.Popen(cmd, cwd=HERE, env=live.capture_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                        encoding='utf-8', errors='replace', creationflags=settings.NO_WINDOW)
            except OSError as exc:
                GPU.release()
                log.close()
                raise Failed(f'Could not start the transcriber: {exc}')
            host = {'proc': proc, 'preempted': False, 'holding': True}
            with _hosts_lock:
                _hosts.append(host)
                if live.capture_running() or time.time() - _live_since[0] < 10:     # a meeting started just now
                    host['preempted'] = True
                    proc.kill()

            def let_go():
                if host['holding']:
                    host['holding'] = False
                    with _hosts_lock:
                        if host in _hosts:
                            _hosts.remove(host)
                    GPU.release()

            def watch():
                while proc.poll() is None:
                    if stop.wait(0.5):
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        return

            threading.Thread(target=watch, daemon=True).start()
            ok, error = False, ''
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line.startswith('{'):
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    kind = event.get('ev')
                    if kind == 'step':
                        self._set(folder, status='running', step=event.get('name') or 'words', text=event.get('text') or '', done=None, total=None)
                    elif kind == 'progress':
                        self._set(folder, quiet=True, done=event.get('done'), total=event.get('total'))
                    elif kind == 'info':
                        folder = self._date_from_file(folder, job, event)
                    elif kind == 'gpu_free':
                        let_go()
                    elif kind == 'error':
                        error = str(event.get('message') or '')
                    elif kind == 'done':
                        ok = bool(event.get('ok'))
                proc.wait()
            finally:
                let_go()
                log.close()
            if stop.is_set():
                raise Cancelled()
            if host['preempted'] and not ok:
                self._set(folder, status='waiting', step='words', text='Paused while your live meeting uses the graphics card. It carries on afterwards.')
                continue
            if not ok or not os.path.isfile(result_path):
                raise Failed(error or 'The transcriber stopped unexpectedly.')
            with open(result_path, encoding='utf-8') as f:
                result = json.load(f)
            self.project.update_meeting(folder, duration_s=int(result.get('duration') or 0))
            self._set(folder, step='reading')
            return folder, result

    def _date_from_file(self, folder, job, event):
        """When the user did not give the date: the time the file says it was recorded, or else when it was last saved."""
        self.project.update_meeting(folder, duration_s=int(event.get('duration') or 0))
        if job.get('when_given'):
            return folder
        when = ''
        created = str(event.get('created') or '')
        match = re.match(r'(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)', created)  # publish-scan: ok
        if match and not created.startswith(('1970', '1904')):
            try:
                moment = datetime.datetime.fromisoformat(f'{match[1]}T{match[2]}+00:00').astimezone()
                when = moment.strftime('%Y-%m-%d %H:%M')
            except ValueError:
                when = ''
        if not when and job.get('modified'):
            try:
                when = time.strftime('%Y-%m-%d %H:%M', time.localtime(float(job['modified']) / 1000 - float(event.get('duration') or 0)))
            except (ValueError, OverflowError, OSError):
                when = ''
        info = self.project.meeting_meta(folder)
        if not when or when == info.get('when'):
            return folder
        return self._move(folder, when=when)

    def _move(self, folder, when=None, title=None):
        """Rename the meeting's folder to match its date and title."""
        info = self.project.meeting_meta(folder)
        when = when or info.get('when') or store.now_stamp()
        title = title or info.get('title') or 'Recorded meeting'
        target = f'{when.replace(":", "")} {store.clean_name(title, 50) or "Meeting"}'
        changes = {'when': when, 'title': title}
        if target == folder:
            self.project.update_meeting(folder, **changes)
            return folder
        candidate, n = target, 2
        while os.path.exists(self.project.meeting_dir(candidate)):
            candidate, n = f'{target} {n}', n + 1
        try:
            os.rename(self.project.meeting_dir(folder), self.project.meeting_dir(candidate))
        except OSError:
            self.project.update_meeting(folder, **changes)
            return folder
        self.project.rename_meeting_key(folder, candidate)
        self.project.update_meeting(candidate, **changes)
        self._emit_meetings()
        return candidate

    def _reader_prompt(self, folder, job, result):
        info = self.project.meeting_meta(folder)
        frames = os.path.join(self.work_dir(job), 'frames')
        rel_frames = self.project.rel(frames)
        voices = result.get('voice_frames') or []
        screens = result.get('screens') or []
        if voices or screens:
            pictures = (f'Pictures from the video are in {rel_frames}. Look at them with the Read tool; read several in one go. '
                        'Start with the voice pictures, each taken while that voice was talking. Screen pictures are taken whenever '
                        'something on screen changed (a page, folder, menu or document), so a run of them shows each step of what '
                        'someone did. Use the transcript to find the stretches where someone shares their screen to show where '
                        'things are or how to do something, and read every screen picture through those stretches, in order. '
                        'Elsewhere, read the ones that look useful from their times. Read at most about 60 pictures in all, so '
                        'you keep room to think; when there are more, choose the stretches that matter most for the work ' + settings.user + ' '
                        'has to do afterwards.\n')
            if voices:
                pictures += 'Voice pictures: ' + '; '.join(f'{v["file"]} ({v["voice"]} talking at {clock(v["t"])})' for v in voices) + '.\n'
            if screens:
                pictures += 'Screen pictures, taken whenever the screen changed: ' + '; '.join(f'{s["file"]} ({clock(s["t"])})' for s in screens) + '.'
        else:
            pictures = 'This recording has no video, so there are no pictures: work from the words alone.'
        table = []
        for voice, row in (result.get('speakers') or {}).items():
            table.append(f'{voice}: {int(row["seconds"])} seconds of speech in {row["phrases"]} phrases, first speaks at {clock(row["first"])}')
        for pair in (result.get('voices') or {}).get('similar') or []:
            table.append(f'{pair["a"]} and {pair["b"]} sound {"quite " if pair["similarity"] > 0.45 else ""}alike: {pair["similarity"]} (1 is the same voice)')
        lines = merge_lines([{**p, 'who': p['voice']} for p in result.get('phrases') or []])
        transcript = '\n'.join(f'[{clock(line["s"])}] {line["who"]}: {line["text"]}' for line in lines)
        meta = self.project.meta()
        known = ', '.join(meta.get('people') or []) or 'none yet'
        return prompts.READER.format(pictures=pictures, project=f'{self.project.name}. {meta.get("description", "")}'.strip(),
                                     state=self.project.read_status(), known=known, hint=job.get('hint') or 'none',
                                     when=nice_when(info.get('when', '')), length=clock(result.get('duration')),
                                     recording=info.get('recording', ''), voices='\n'.join(table), transcript=transcript.replace('</transcript', '< /transcript'))

    def _read(self, folder, job, result, stop):
        import live
        saved = os.path.join(self.work_dir(job), 'reading.json')
        try:
            with open(saved, encoding='utf-8') as f:
                reading = json.load(f)
            if not reading.get('failed'):
                return reading
        except (OSError, ValueError):
            pass
        self._set(folder, status='running', step='reading', text='Claude is reading the meeting', done=None, total=None)
        seen = {'count': 0}

        def on_event(what, value):
            if what == 'tool' and value:
                if value.startswith('Reading ') and value.endswith('.jpg'):
                    seen['count'] += 1
                    value = f'Claude is looking at the video ({seen["count"]} picture{"s" if seen["count"] > 1 else ""} so far)'
                self._set(folder, quiet=True, text=value)

        reply = engines.read_recording(self._reader_prompt(folder, job, result), self.project.dir, on_event, timeout=1800, stop=stop)
        if stop.is_set():
            raise Cancelled()
        usage = reply.get('usage') or {}
        try:
            with open(os.path.join(self.project.app_dir, 'usage.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps({'t': time.strftime('%Y-%m-%dT%H:%M:%S'), 'kind': 'import_read', 'engine': 'opus', 'ok': bool(reply.get('ok')),
                                    'elapsed_s': reply.get('elapsed_s'), 'input': usage.get('input_tokens'),
                                    'cache_read': usage.get('cache_read_input_tokens'), 'output': usage.get('output_tokens')}) + '\n')
        except OSError:
            pass
        if reply.get('ok'):
            reading = live.parse_json_reply(reply.get('text'))
            if not isinstance(reading.get('voices'), dict):
                reading = {'failed': 'it replied without the details the app needs'}
        else:
            reading = {'failed': str(reply.get('error') or 'unknown error')}
        with open(saved, 'w', encoding='utf-8') as f:
            json.dump(reading, f, ensure_ascii=False, indent=1)
        return reading

    def _write(self, folder, job, result, reading):
        """The meeting's transcript and rough notes, named and in the same format as a live meeting's."""
        import live
        info = self.project.meeting_meta(folder)
        voices = reading.get('voices') if isinstance(reading.get('voices'), dict) else {}
        names = {}
        for voice in result.get('speakers') or {}:
            names[voice] = clean_person(voices.get(voice)) or f'Speaker {voice[1:]}'
        lines = merge_lines([{**p, 'who': names.get(p['voice'], p['voice'])} for p in result.get('phrases') or []])
        for item in reading.get('screen') or []:
            at = parse_clock(item.get('at')) if isinstance(item, dict) else None
            what = re.sub(r'\s+', ' ', str(item.get('what') or '')).strip() if isinstance(item, dict) else ''
            if at is not None and what:
                lines.append({'s': at, 'e': at, 'who': 'On screen', 'text': what, 'screen': True})
        lines.sort(key=lambda line: (line['s'], 0 if line.get('screen') else 1))
        title = info.get('title') if job.get('named') else (str(reading.get('title') or '').strip()[:70] or info.get('title') or 'Recorded meeting')
        people = sorted({line['who'] for line in lines if not line.get('screen')}, key=lambda p: (p != 'Me', p))
        when = nice_when(info.get('when', ''))
        head = [f'# Transcript: {title}', '', f'{when}. People: {", ".join(people)}.',
                f'Imported from the recording {info.get("recording")}. Who said what was worked out from their voices, so a line can be under the wrong name.', '']
        text = '\n'.join(head + [f'[{clock(line["s"])}] {line["who"]}: {line["text"]}' for line in lines]) + '\n'
        base = self.project.meeting_dir(folder)
        store.atomic_write(os.path.join(base, 'transcript.md'), text)
        notes = [f'# Rough notes: {title}', '', f'{when}. Made from the recording afterwards.', '']
        if reading.get('about'):
            notes += ['## About', '', str(reading['about']).strip(), '']
        items = [n for n in reading.get('notes') or [] if isinstance(n, dict) and n.get('t')]
        for section, heading in live.SECTIONS:
            chosen = [n for n in items if (n.get('s') if n.get('s') in live.SECTION_IDS else 'points') == section]
            if chosen:
                notes += [f'## {heading}', '']
                for note in chosen:
                    notes += [str(note['t']).strip(), '']
        guides = [g for g in reading.get('howto') or [] if isinstance(g, dict) and isinstance(g.get('steps'), list) and g['steps']]
        if guides:
            # The steps someone showed on screen, so the work agreed in the meeting can be done afterwards.
            notes += ['## How to (shown on screen)', '']
            for guide in guides:
                at = f' ({guide["at"]})' if guide.get('at') else ''
                notes += [f'### {str(guide.get("title") or "Steps").strip()}{at}', '']
                notes += [f'{n}. {str(step).strip()}' for n, step in enumerate(guide['steps'], 1) if str(step).strip()] + ['']
        if not items and not reading.get('about') and not guides:
            notes += ['No rough notes: Claude could not read the meeting.' if reading.get('failed') else 'Nothing to note.', '']
        store.atomic_write(os.path.join(base, 'live-summary.md'), '\n'.join(notes))
        self.project.update_meeting(folder, people=people, duration_s=int(result.get('duration') or 0), summary_status='',
                                    voices=names, evidence=reading.get('evidence') if isinstance(reading.get('evidence'), dict) else {})
        known = list(dict.fromkeys((self.project.meta().get('people') or []) +
                                   [p for p in people if p != 'Me' and not p.startswith('Speaker ')]))
        self.project.update_meta(people=known)
        if not reading.get('failed'):
            folder = self._move(folder, title=title)
        self._emit_meetings()
        return folder
