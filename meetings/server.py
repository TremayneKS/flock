"""Meetings: the local web app. Standard library only for the web part; runs in the speech engine's venv.

Listens on 127.0.0.1 only. Because the assistant behind it can run commands, a web page open in the browser must never be
able to reach it: the Host and Origin headers are checked on every request, and anything that changes state needs a custom
header a cross-site request cannot send. Files from a project are served sandboxed, so a project's HTML can't reach the API.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engines
import harness_link
import live
import settings
import store

PORT = settings.PORT
SERVER_NAMES = ('FlockMeetings', 'NestMeetings', 'KCMeetings')  # publish-scan: ok (legacy Server header kept for compatibility with older installs)
SERVER_NAME = 'KCMeetings'                     # publish-scan: ok (legacy Server header kept for older installs without /api/hello)
VERSION = 2                             # /api/hello: lets Flock tell this app from anything else on the port
WEB = os.path.join(HERE, 'web')
ALLOWED_HOSTS = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
ALLOWED_ORIGINS = {f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'}
MAX_UPLOAD = 500 * 1024 * 1024
MAX_RECORDING = 20 * 1024 ** 3          # a long video call at high quality
# Only pages on this machine (Flock) may show the app in a frame, so a web site cannot frame it and trick clicks.
APP_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self'; connect-src 'self'; "
           "font-src 'self'; media-src 'self'; object-src 'self'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'self' http://127.0.0.1:* http://localhost:*")
SANDBOX_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self' data:; media-src 'self'; frame-ancestors 'self'"
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.png': 'image/png'}

sessions = {}
sessions_lock = threading.Lock()
auth_cache = {'at': 0, 'value': None, 'checking': False}
auth_lock = threading.Lock()


def _check_auth():
    try:
        auth_cache.update(value=engines.signed_in(), at=time.time())
    finally:
        auth_cache['checking'] = False


def signed_in(force=False):
    """Is Claude Code signed in? `claude auth status` can take 10 s or more, so the answer comes from a cache that is
    refreshed in the background; only an explicit re-check (force) waits for it. Unknown counts as signed in."""
    if force:
        _check_auth()
        return auth_cache['value']
    with auth_lock:
        stale = auth_cache['value'] is None or time.time() - auth_cache['at'] > 30
        if stale and not auth_cache['checking']:
            auth_cache['checking'] = True
            threading.Thread(target=_check_auth, daemon=True).start()
    return True if auth_cache['value'] is None else auth_cache['value']


def session_for(name):
    name = name or ''
    if not name or store.clean_name(name, 200) != name:
        raise ValueError('Unknown project.')
    project = store.Project(name)
    if not os.path.isdir(project.app_dir):
        raise ValueError('Unknown project.')
    with sessions_lock:
        if name not in sessions:
            sessions[name] = live.ProjectSession(project)
        return sessions[name]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = SERVER_NAME

    def log_message(self, *args):
        pass

    def _guard(self, changing):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in ALLOWED_HOSTS or (origin and origin not in ALLOWED_ORIGINS):
            self._send_json({'error': 'Not allowed.'}, 403)
            return False
        if changing and self.headers.get('X-Flock-App') != '1' and self.headers.get('X-Flock-App') != '1' and self.headers.get('X-KC-App') != '1':
            self._send_json({'error': 'Not allowed.'}, 403)
            return False
        return True

    def _send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type, csp=None, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if csp:
            self.send_header('Content-Security-Policy', csp)
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        rel = 'index.html' if path == '/' else path[len('/static/'):] if path.startswith('/static/') else None
        if rel is None:
            return self._send_json({'error': 'Not found.'}, 404)
        full = os.path.realpath(os.path.join(WEB, rel))
        if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
            return self._send_json({'error': 'Not found.'}, 404)
        with open(full, 'rb') as f:
            body = f.read()
        ext = os.path.splitext(full)[1].lower()
        self._send_bytes(body, STATIC_TYPES.get(ext, 'application/octet-stream'), APP_CSP if ext == '.html' else None)

    def _events(self, session):
        client = queue.Queue(maxsize=5000)
        session.clients.append(client)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b': connected\n\n')
            self.wfile.flush()
            while True:
                try:
                    event = client.get(timeout=15)
                    payload = 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                except queue.Empty:
                    payload = ': keep-alive\n\n'
                self.wfile.write(payload.encode('utf-8'))
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            if client in session.clients:
                session.clients.remove(client)

    def _serve_file(self, session, rel):
        full = session.project.safe_path(rel)
        if not os.path.isfile(full):
            return self._send_json({'error': 'That file no longer exists.'}, 404)
        content_type = store.content_type(full)
        size = os.path.getsize(full)
        media = content_type.startswith(('video/', 'audio/'))
        sandbox = not media and not content_type.startswith('application/pdf')
        start, end = 0, size - 1
        # Video and audio are sent in pieces, so a long recording plays at once and can jump to any moment.
        match = re.match(r'bytes=(\d*)-(\d*)$', self.headers.get('Range') or '') if media else None
        if match and (match[1] or match[2]):
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Accept-Ranges', 'bytes' if media else 'none')
        if sandbox:
            self.send_header('Content-Security-Policy', SANDBOX_CSP)
        if content_type == 'application/octet-stream':
            self.send_header('Content-Disposition', 'attachment')
        self.end_headers()
        with open(full, 'rb') as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_GET(self):
        if not self._guard(False):
            return
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if not url.path.startswith('/api/'):
                return self._static(url.path)
            if url.path == '/api/hello':
                return self._send_json({'app': 'kc-meetings', 'version': VERSION, 'root': store.ROOT, 'harness': harness_link.fingerprint()})
            if url.path == '/api/projects':
                return self._send_json({'projects': store.list_projects(), 'signed_in': signed_in('fresh' in query), 'root': store.ROOT,
                                       'claude': engines.claude_installed()})
            if url.path == '/api/devices':
                return self._send_json(live.audio_devices())
            session = session_for(query.get('project'))
            if url.path == '/api/snapshot':
                return self._send_json(session.snapshot())
            if url.path == '/api/events':
                return self._events(session)
            if url.path == '/api/search':
                return self._send_json({'results': store.search(session.project, query.get('q', ''))})
            if url.path == '/api/view':
                return self._send_json(store.view_file(session.project, query.get('path', '')))
            if url.path == '/api/file':
                return self._serve_file(session, query.get('path', ''))
            return self._send_json({'error': 'Not found.'}, 404)
        except ValueError as exc:
            return self._send_json({'error': str(exc)}, 400)
        except (ConnectionError, BrokenPipeError):
            return None

    def _read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > 5_000_000:
            raise ValueError('That is too much text.')
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            raise ValueError('Expected JSON.')
        raw = self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8'))

    def do_POST(self):
        if not self._guard(True):
            length = int(self.headers.get('Content-Length') or 0)
            if length and length < 5_000_000:
                self.rfile.read(length)
            return
        url = urllib.parse.urlsplit(self.path)
        try:
            if url.path == '/api/upload':
                return self._upload(url)
            if url.path == '/api/import':
                return self._import_upload(url)
            body = self._read_json()
            if url.path in ('/api/harness/link', '/api/harness/model'):
                return self._harness(url.path, body)
            if url.path == '/api/projects':
                project = store.create_project(body.get('name', ''), body.get('description', ''))
                return self._send_json({'name': project.name})
            session = session_for(body.get('project'))
            if url.path == '/api/message':
                session.submit(body.get('text', ''))
            elif url.path == '/api/stop':
                session.stop()
            elif url.path == '/api/engine':
                session.set_engine(body.get('engine'))
            elif url.path == '/api/action':
                session.do_action(str(body.get('action') or ''))
            elif url.path == '/api/meeting/start':
                session.start_meeting(body.get('title', ''), body.get('mic'))
            elif url.path == '/api/meeting/end':
                session.end_meeting()
            elif url.path == '/api/meeting/pause':
                session.pause_meeting(bool(body.get('paused')))
            elif url.path == '/api/meeting/title':
                session.rename_meeting_live(body.get('title', ''))
            elif url.path == '/api/meeting/summary':
                session.make_summary(body.get('folder'), engine=body.get('engine') or None)
            elif url.path == '/api/meeting/speaker':
                session.rename_speaker(str(body.get('pid') or ''), body.get('name', ''))
            elif url.path == '/api/meeting/rename-speaker':
                session.rename_stored_speaker(body.get('folder'), str(body.get('old') or ''), str(body.get('new') or ''))
            elif url.path == '/api/import/path':
                folder = self._import_path(session, body)
                return self._send_json({'ok': True, 'folder': folder})
            elif url.path == '/api/import/cancel':
                session.importer.cancel(body.get('folder'))
            elif url.path == '/api/import/retry':
                session.importer.retry(body.get('folder'))
            elif url.path == '/api/helper/stop':
                session.stop_helper()
            elif url.path == '/api/reveal':
                full = session.project.safe_path(body.get('path') or '') if body.get('path') else session.project.dir
                reveal(full)
            else:
                return self._send_json({'error': 'Not found.'}, 404)
            return self._send_json({'ok': True})
        except ValueError as exc:
            return self._send_json({'error': str(exc)}, 400)
        except (ConnectionError, BrokenPipeError):
            return None

    def _harness(self, path, body):
        """Flock hands over its model endpoint (link, from Flock itself), or its window says which model is
        picked (model, from this page inside it). Projects that follow Flock use the new model from their next turn."""
        with sessions_lock:
            open_sessions = list(sessions.values())
        before = {id(s): s.engine_name for s in open_sessions}
        if path == '/api/harness/link':
            harness_link.link(body.get('url'), body.get('token'), body.get('model'))
        else:
            model = body.get('model') if isinstance(body.get('model'), dict) else body
            harness_link.set_model(model.get('id'), model.get('name'))
        for session in open_sessions:
            session.harness_changed(before[id(session)])
        return self._send_json({'ok': True})

    def _upload(self, url):
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        session = session_for(query.get('project'))
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_UPLOAD:
            self.close_connection = True
            raise ValueError('That file is too big (the limit is 500 MB).')
        rel = session.project.add_file(query.get('name', 'file'), self.rfile.read(length))
        session.add_chat('system', f'{settings.User} added a project file: {rel}')
        session.emit({'type': 'meetings', 'meetings': session.project.meetings(), 'files': session.project.file_tree()})
        return self._send_json({'ok': True, 'path': rel})

    def _import_upload(self, url):
        """A recording sent from the browser: written straight to Files/Recordings in pieces, never held in memory."""
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        session = session_for(query.get('project'))
        length = int(self.headers.get('Content-Length') or 0)
        name = os.path.basename(query.get('name') or 'recording.mp4')
        if length <= 0 or length > MAX_RECORDING or os.path.splitext(name)[1].lower() not in store.RECORDING_EXT:
            self.close_connection = True
            raise ValueError('That is not a recording the app can read.' if length > 0 else 'That file is empty.')
        path = store.unique_path(session.project.recordings_dir, name)
        part = path + '.part'
        left = length
        try:
            with open(part, 'wb') as f:
                while left > 0:
                    chunk = self.rfile.read(min(4 * 1024 * 1024, left))
                    if not chunk:
                        raise ConnectionError('The upload stopped part way.')
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(part, path)
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        folder = session.importer.add(session.project.rel(path), title=query.get('title', ''), when=query.get('when', ''),
                                      hint=query.get('people', ''), modified=query.get('modified'))
        return self._send_json({'ok': True, 'folder': folder})

    @staticmethod
    def _import_path(session, body):
        """A recording already on this PC (a command-line import): copied in by the import itself, or used where it is when it is
        already one of the project's files."""
        source = os.path.realpath(str(body.get('path') or ''))
        if not os.path.isfile(source):
            raise ValueError(f'No such file: {source}')
        if os.path.splitext(source)[1].lower() not in store.RECORDING_EXT:
            raise ValueError('That is not a recording the app can read.')
        project = session.project
        files = os.path.realpath(project.files_dir)
        options = dict(title=body.get('title', ''), when=body.get('when', ''), hint=body.get('people', ''),
                       modified=os.path.getmtime(source) * 1000)
        if source.startswith(files + os.sep):
            return session.importer.add(project.rel(source), copied=False, **options)
        target = store.unique_path(project.recordings_dir, os.path.basename(source))
        open(target, 'wb').close()          # keeps the name while the copy waits its turn
        return session.importer.add(project.rel(target), copy_from=source, **options)


def reveal(full):
    """Show a file or folder in the system's file manager."""
    if settings.WINDOWS:
        cmd = ['explorer', f'/select,{full}'] if os.path.isfile(full) else ['explorer', full]
    elif sys.platform == 'darwin':
        cmd = ['open', '-R', full] if os.path.isfile(full) else ['open', full]
    else:
        cmd = ['xdg-open', os.path.dirname(full) if os.path.isfile(full) else full]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=settings.NO_WINDOW)
    except OSError:
        raise ValueError('There is no file manager to show it in.')


def who_has_port():
    """'us' when Meetings already answers on PORT, 'other' when something else does, '' when nothing answers."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/hello', timeout=5) as response:
            srv = response.headers.get('Server') or ''
            return 'us' if any(s in srv for s in SERVER_NAMES) else 'other'
    except urllib.error.HTTPError as exc:          # an older Meetings install has no /api/hello, but says who it is
        srv = exc.headers.get('Server') or ''
        return 'us' if any(s in srv for s in SERVER_NAMES) else 'other'
    except Exception:
        return ''


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def main():
    try:
        server = Server(('127.0.0.1', PORT), Handler)
    except OSError:
        if who_has_port() == 'us':
            print(f'Meetings is already running on port {PORT}.', flush=True)
            return
        print(f'Meetings could not start: port {PORT} is used by another program. Close it, or choose another port '
              f'(FLOCK_MEETINGS_PORT / KC_MEETINGS_PORT, or "port" in settings.json).', file=sys.stderr, flush=True)
        sys.exit(2)
    live.prepare_app_files()
    print(f'Meetings on http://127.0.0.1:{PORT}/ (data: {store.ROOT})', flush=True)
    signed_in()                         # start the first sign-in check now, so the window does not wait for it
    server.serve_forever()


if __name__ == '__main__':
    main()
