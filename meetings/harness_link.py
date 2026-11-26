"""Using the model picked in Flock.

When Flock starts Meetings (environment FLOCK_URL and FLOCK_TOKEN; legacy NEST_URL / RAVEN_URL / KC_HARNESS_URL also accepted) or finds
it running (POST /api/harness/link from Flock itself), it hands over the address of its local model endpoint and a token
for it. The endpoint speaks the OpenAI chat API and forwards to whichever connection is picked in Flock, so this app never
holds the connections' keys. The token stays in memory: after a restart Flock links again within seconds.

Which model: the Flock window tells the page (embed URL ?model=, then a message whenever the picker changes), the page
tells the server (POST /api/harness/model), and the choice is remembered in <data>/.app/harness.json (no token in it).
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import settings
import store

_lock = threading.Lock()
_state = {'url': '', 'token': '', 'model': None, 'models': {}, 'models_at': 0.0, 'reachable': None}
CLAUDE_KIND = 'claude-code'           # Meetings runs Claude Code itself, with its own sessions per project
STATE_FILE = os.path.join(store.APP_DIR, 'harness.json')


def _local(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost') and not parts.path.strip('/')


def _load():
    url = os.environ.get('FLOCK_URL') or os.environ.get('NEST_URL') or os.environ.get('RAVEN_URL') or os.environ.get('KC_HARNESS_URL', '')
    token = os.environ.get('FLOCK_TOKEN') or os.environ.get('NEST_TOKEN') or os.environ.get('RAVEN_TOKEN') or os.environ.get('KC_HARNESS_TOKEN', '')
    if _local(url) and token:
        _state.update(url=url.rstrip('/'), token=token)
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved.get('model'), dict) and saved['model'].get('id'):
            _state['model'] = {'id': str(saved['model']['id']), 'name': str(saved['model'].get('name') or saved['model']['id'])}
    except (OSError, ValueError, AttributeError):
        pass


_load()


def linked():
    return bool(_state['url'] and _state['token'])


def fingerprint():
    if not linked():
        return None
    import hashlib
    return {'url': _state['url'], 'key': hashlib.sha256(_state['token'].encode()).hexdigest()[:16]}


def link(url, token, model=None):
    url = str(url or '').rstrip('/')
    if not _local(url) or not isinstance(token, str) or len(token) < 16:
        raise ValueError('That is not a Flock instance on this computer.')
    with _lock:
        _state.update(url=url, token=token, models={}, models_at=0.0, reachable=None)
    if isinstance(model, dict) and model.get('id') and not _state['model']:
        set_model(model.get('id'), model.get('name'))


def set_model(model_id, name=None):
    """The model picked in the harness. Returns True when it changed."""
    model_id = str(model_id or '').strip()[:80]
    if not model_id:
        raise ValueError('No model.')
    name = str(name or model_id).strip()[:80]
    with _lock:
        before = _state['model']
        _state['model'] = {'id': model_id, 'name': name}
    try:
        store.atomic_write(STATE_FILE, json.dumps({'model': _state['model']}))
    except OSError:
        pass
    return not before or before['id'] != model_id or before['name'] != name


def models(force=False):
    """The harness's connections by id ({} when it cannot be asked). Cached for 30 s; a stale list is answered at once and
    refreshed in the background, so a closed harness never holds anything up."""
    if not linked():
        return {}
    if not force and time.time() - _state['models_at'] < 30:
        return _state['models']
    if not force and _state['models_at']:
        with _lock:
            busy, _state['refreshing'] = _state.get('refreshing'), True
        if not busy:
            threading.Thread(target=_refresh, daemon=True).start()
        return _state['models']
    return _refresh()


def _refresh():
    try:
        request = urllib.request.Request(_state['url'] + '/api/llm/v1/models', headers={'Authorization': 'Bearer ' + _state['token']})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read())
        found = {m['id']: m for m in data.get('data') or [] if isinstance(m, dict) and m.get('id')}
        _state.update(models=found, models_at=time.time(), reachable=True)
        if not _state['model'] and data.get('selected') in found:
            set_model(data['selected'], found[data['selected']].get('name'))
        return found
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        _state.update(models_at=time.time(), reachable=False)
        return _state['models']
    finally:
        _state['refreshing'] = False


def current():
    """The model picked in the harness: {id, name, kind, context, max_output}, or None if the harness never said."""
    model = _state['model']
    if not model:
        if linked():
            models()
            model = _state['model']
        if not model:
            return None
    info = models().get(model['id']) or {}
    return {'id': model['id'], 'name': info.get('name') or model['name'], 'kind': info.get('kind') or 'openai',
            'context': int(info.get('context') or 128000), 'max_output': int(info.get('max_output') or 16384)}


def is_claude(model):
    return bool(model) and model.get('kind') == CLAUDE_KIND


NOT_LINKED = ('Flock is not connected to Meetings, so its model cannot answer here. Open the Meetings view in Flock '
              '(it connects by itself), or use another model for now.')


def stream(model_id, messages, callback=None, stop=None, timeout=600, effort=None, max_tokens=8000, session=None, wait=0):
    """One streamed answer from Flock's model. Returns {ok, text, usage} or {ok: False, kind, error}; kind is
    'unavailable' (the model or Flock is off), 'starting', 'limit', 'refused', 'interrupted' or 'other'."""
    if not linked():
        return {'ok': False, 'kind': 'unavailable', 'error': NOT_LINKED, 'text': ''}
    body = {'model': model_id, 'messages': messages, 'stream': True, 'max_tokens': max_tokens, 'kc_wait': wait,
            'kc_session': session or 'meetings'}
    if effort:
        body['reasoning_effort'] = effort
    request = urllib.request.Request(_state['url'] + '/api/llm/v1/chat/completions', data=json.dumps(body).encode('utf-8'),
                                     method='POST', headers={'Authorization': 'Bearer ' + _state['token'],
                                                             'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
    t0 = time.time()
    parts, usage, finish, thinking_said = [], None, None, False
    response = None
    done = threading.Event()

    def closer():
        # Stop: close the answer. A read waiting on a quiet model wakes at the harness's next keep-alive (every 10 s).
        while stop is not None and not done.is_set():
            if stop.wait(0.3):
                try:
                    response.close()
                except Exception:
                    pass
                return
    try:
        response = urllib.request.urlopen(request, timeout=min(timeout, 180) + int(wait or 0))
        threading.Thread(target=closer, daemon=True).start()
        for raw in response:
            if stop is not None and stop.is_set():
                break
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get('error'):
                error = chunk['error'] if isinstance(chunk['error'], dict) else {'message': str(chunk['error'])}
                return {'ok': False, 'kind': _kind(error.get('type')), 'error': str(error.get('message') or 'The model failed.')[:500],
                        'text': ''.join(parts)}
            usage = chunk.get('usage') or usage
            for choice in chunk.get('choices') or []:
                finish = choice.get('finish_reason') or finish
                delta = choice.get('delta') or {}
                if (delta.get('reasoning_content') or delta.get('reasoning')) and not thinking_said:
                    thinking_said = True
                    if callback:
                        callback('thinking', '')
                if delta.get('content'):
                    parts.append(delta['content'])
                    if callback:
                        callback('delta', delta['content'])
    except urllib.error.HTTPError as exc:
        try:
            error = (json.loads(exc.read() or b'{}').get('error')) or {}
        except ValueError:
            error = {}
        if exc.code == 401:
            _state['models'] = {}
            return {'ok': False, 'kind': 'unavailable', 'text': '',
                    'error': 'Flock did not accept Meetings (it may have been reinstalled). Open the Meetings view in Flock to connect again.'}
        return {'ok': False, 'kind': _kind(error.get('type')), 'error': str(error.get('message') or f'Flock answered {exc.code}.')[:500], 'text': ''}
    except (urllib.error.URLError, OSError) as exc:
        if stop is not None and stop.is_set():
            return {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': ''.join(parts)}
        if parts:
            return {'ok': False, 'kind': 'other', 'error': f'The answer was cut off ({type(exc).__name__}).', 'text': ''.join(parts)}
        reason = str(getattr(exc, 'reason', exc))
        if 'refused' in reason.lower() or isinstance(getattr(exc, 'reason', None), ConnectionRefusedError):
            return {'ok': False, 'kind': 'unavailable', 'text': '',
                    'error': 'Flock is not running, so its model cannot answer here. Start Flock, or use another model for now.'}
        return {'ok': False, 'kind': 'other', 'error': f'Flock did not answer ({type(exc).__name__}).', 'text': ''}
    finally:
        done.set()
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
    if stop is not None and stop.is_set():
        return {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': ''.join(parts)}
    if finish == 'content_filter':
        return {'ok': False, 'kind': 'refused', 'error': "The model's filter stopped this reply.", 'text': ''}
    return {'ok': True, 'text': ''.join(parts), 'usage': usage or {}, 'finish': finish, 'elapsed_s': round(time.time() - t0, 1)}


def _kind(kind):
    return {'unavailable': 'unavailable', 'unsupported': 'unavailable', 'starting': 'starting', 'limit': 'limit',
            'auth_upstream': 'unavailable', 'busy': 'other'}.get(kind or '', 'other')
