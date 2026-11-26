"""The models. OpusEngine is the fast main chat: one long-running Claude Code session per project, resumed across meetings,
with no tools at all so it can never go quiet. run_helper is the one helper: Opus with full tools and its own memory per
project, which runs in the background. DeepSeekEngine is the stand-in the user can switch to. HarnessEngine is the model picked in Flock, reached through the harness (harness_link), so
this app holds none of its keys: used when Meetings follows the harness (the default inside it, from 2026-11-07).

Design (2026-11-05): Claude Code on Opus 5.5 only, with DeepSeek as the manual switch. GLM never. No automatic fallback.
The helper may run tools whenever it decides to, without the user asking. When a run is its own idea rather than something the user
typed, it can't reach production or credentials, send anything, change files or take over the mouse and keyboard.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import harness_link
import settings

MODEL = 'claude-opus-5-5'
OPUS_LABEL = 'Claude Opus 5.5'
LIVE_EFFORT = 'high'
SUMMARY_EFFORT = 'xhigh'
HELPER_EFFORT = 'high'
READER_EFFORT = 'high'                  # reading an imported recording: naming voices, noting the screen, rough notes
DEEPSEEK_MODEL = 'flash'                # deepseek-v4.1-flash, by the provider's own name for it; never GLM here
HELPER_TOOLS = 'Bash,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,NotebookEdit,Skill,ToolSearch'
# A helper run that is its own idea, not something the user typed. It can look at anything else and run anything else.
OWN_IDEA_ALLOW = ['Bash', 'Read', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill', 'ToolSearch',
                  'mcp__computer__screenshot', 'mcp__computer__list_windows', 'mcp__computer__wait']
OWN_IDEA_DENY = ['Edit', 'Write', 'NotebookEdit',
                 'mcp__computer__click', 'mcp__computer__move', 'mcp__computer__drag', 'mcp__computer__scroll',
                 'mcp__computer__type_text', 'mcp__computer__press', 'mcp__computer__focus_window', 'mcp__computer__open_item',
                 # other machines, and anything sent as the user (a machine adds its own tools in settings.json "helper_deny")
                 'Bash(ssh *)', 'Bash(scp *)', 'Bash(sftp *)', 'Bash(rsync *)', 'Bash(rclone *)',
                 'Bash(gh *)', 'Bash(git push*)', 'Bash(git commit*)',
                 # credential stores
                 'Read(~/.ssh/**)', 'Read(~/.claude/.credentials.json)', 'Read(~/.codex/**)', 'Read(~/.aws/**)', 'Read(~/.config/gh/**)',
                 'Read(**/providers.json)', 'Read(**/auth.json)', 'Read(**/rclone.conf)', 'Read(**/*secrets*/**)', 'Read(**/.env)',
                 'Bash(*.ssh*)', 'Bash(*secrets*)', 'Bash(*auth.json*)', 'Bash(*credentials*)', 'Bash(*providers.json*)',
                 'Bash(*rclone.conf*)', 'Bash(*id_ed25519*)', 'Bash(*id_rsa*)'] + settings.HELPER_DENY
# No user settings (so no hooks, no allow-list, no plugins, no hub memory). The app is the one deciding about tools.
SETTINGS = json.dumps({'disableAllHooks': True, 'switchModelsOnFlag': False, 'autoMemoryEnabled': False})
SIGN_IN_HELP = "Claude Code isn't signed in on this computer. Run this in a terminal: claude auth login"
MISSING_HELP = ("The assistant needs Claude Code, which is not installed on this computer. Install it from "
                "https://claude.com/claude-code, run: claude auth login, then press Try again.")


def claude_exe():
    local = os.path.expanduser(os.path.join('~', '.local', 'bin', 'claude.exe' if settings.WINDOWS else 'claude'))
    return shutil.which('claude') or local


def claude_installed():
    return bool(shutil.which('claude')) or os.path.exists(claude_exe())


def deepseek_available():
    return settings.deepseek() is not None


def clean_env():
    """The user's own Claude sign-in, not anything inherited from a Claude session that launched this app."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CLAUDE', 'ANTHROPIC'))}
    env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] = '1'
    env['PYTHONUTF8'] = '1'
    return env


def signed_in():
    try:
        out = subprocess.run([claude_exe(), 'auth', 'status'], capture_output=True, text=True, timeout=20, env=clean_env(),
                             creationflags=settings.NO_WINDOW).stdout
        return bool(json.loads(out).get('loggedIn'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def classify_error(text):
    low = (text or '').lower()
    if 'safeguards' in low or 'flagged' in low or 'usage policy' in low or '/legal/aup' in low or 'refus' in low:
        return 'refused'
    if 'authenticate' in low or 'not logged in' in low or 'oauth' in low or 'please run /login' in low:
        return 'signed_out'
    if 'usage limit' in low or 'rate limit' in low or 'hit your' in low or 'limit reached' in low or 'out of extra usage' in low:
        return 'limit'
    return 'other'


def describe_tool(name, data):
    data = data or {}
    if name == 'Bash':
        return 'Running: ' + str(data.get('description') or data.get('command') or '')[:70]
    if name in ('Read', 'NotebookRead'):
        return 'Reading ' + os.path.basename(str(data.get('file_path') or ''))
    if name in ('Edit', 'Write', 'NotebookEdit'):
        return 'Editing ' + os.path.basename(str(data.get('file_path') or data.get('notebook_path') or ''))
    if name == 'Grep':
        return f'Searching files for "{str(data.get("pattern") or "")[:40]}"'
    if name == 'Glob':
        return 'Looking through the project files'
    if name == 'WebSearch':
        return f'Searching the web: {str(data.get("query") or "")[:60]}'
    if name == 'WebFetch':
        match = re.match(r'https?://([^/]+)', str(data.get('url') or ''))
        return 'Reading ' + (match[1] if match else 'a web page')
    if name.startswith('mcp__computer__'):
        return 'Using your computer: ' + name.rsplit('__', 1)[-1].replace('_', ' ')
    if name == 'ToolSearch':
        return ''       # housekeeping, not worth showing the user
    return 'Using ' + name


class OpusEngine:
    """One `claude -p` process, reading stream-json. Started on the first turn; resumes the project's session."""
    name = 'opus'
    label = OPUS_LABEL

    def __init__(self, cwd, session_id, resume, role_file, on_started=None):
        self.cwd, self.session_id, self.resume = cwd, session_id, resume
        self.role_file = role_file
        self.on_started = on_started
        self.proc = None
        self.write_lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.callback = None
        self.done = threading.Event()
        self.result = None
        self.text_parts = []
        self.interrupted = False
        self.compacted = False
        self.wrong_model = ''
        self.last_used = time.time()
        self.context_tokens = 0
        self.totals = {'input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'turns': 0, 'cost': 0.0}
        self.rate_limit = {}
        self.stderr_tail = []

    def _spawn(self):
        os.makedirs(self.cwd, exist_ok=True)
        cmd = [claude_exe(), '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
               '--include-partial-messages', '--model', MODEL, '--effort', LIVE_EFFORT, '--autocompact', '100000',
               '--system-prompt-file', self.role_file, '--permission-mode', 'dontAsk', '--tools', '',
               '--setting-sources', '', '--settings', SETTINGS, '--strict-mcp-config']
        cmd += ['--resume', self.session_id] if self.resume else ['--session-id', self.session_id]
        self.proc = subprocess.Popen(cmd, cwd=self.cwd, env=clean_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, encoding='utf-8', errors='replace',
                                     creationflags=settings.NO_WINDOW)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()

    def _read_err(self, proc):
        for line in proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.strip()])[-20:]

    def _write(self, message):
        with self.write_lock:
            self.proc.stdin.write(json.dumps(message) + '\n')
            self.proc.stdin.flush()

    def _emit(self, kind, value=''):
        callback = self.callback
        if callback:
            try:
                callback(kind, value)
            except Exception:
                pass

    def _read(self, proc):
        open_block = {}
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get('type')
            if kind == 'control_request':
                self._answer_control(event)
            elif kind == 'stream_event':
                inner = event.get('event') or {}
                inner_type = inner.get('type')
                if inner_type == 'content_block_start':
                    block = inner.get('content_block') or {}
                    open_block[inner.get('index')] = block.get('type')
                    if block.get('type') == 'thinking':
                        self._emit('thinking')
                elif inner_type == 'content_block_delta':
                    delta = inner.get('delta') or {}
                    if delta.get('type') == 'text_delta':
                        self._emit('delta', delta.get('text') or '')
                elif inner_type == 'message_start':
                    model = (inner.get('message') or {}).get('model') or ''
                    if model and not model.startswith(MODEL):
                        self.wrong_model = model
                        self._emit('wrong_model', model)
            elif kind == 'assistant':
                message = event.get('message') or {}
                model = message.get('model') or ''
                if model and model != '<synthetic>' and not model.startswith(MODEL):
                    self.wrong_model = model
                    self._emit('wrong_model', model)
                for block in message.get('content') or []:
                    if block.get('type') == 'tool_use':
                        self._emit('tool', describe_tool(block.get('name') or '', block.get('input')))
                    elif block.get('type') == 'text' and block.get('text'):
                        self.text_parts.append(block['text'])
            elif kind == 'system':
                subtype = event.get('subtype')
                if subtype == 'init':
                    model = event.get('model') or ''
                    if model and not model.startswith(MODEL):
                        self.wrong_model = model
                    if self.on_started:
                        self.on_started(event.get('session_id') or self.session_id)
                elif subtype == 'compact_boundary':
                    self.compacted = True
                    self._emit('status', 'Memory tidied')
                elif subtype == 'status' and event.get('status') == 'compacting':
                    self._emit('status', 'Tidying its memory, about a minute')
            elif kind == 'rate_limit_event':
                self.rate_limit = event.get('rate_limit_info') or {}
                self._emit('rate_limit', self.rate_limit)
            elif kind == 'result':
                self._finish(event)
        self._on_exit()

    def _answer_control(self, event):
        request = event.get('request') or {}
        request_id = event.get('request_id')
        if request.get('subtype') == 'can_use_tool':            # the main chat has no tools, so this should never happen
            response = {'subtype': 'success', 'request_id': request_id,
                        'response': {'behavior': 'deny', 'message': 'You have no tools. Use the helper field.'}}
        else:
            response = {'subtype': 'error', 'request_id': request_id, 'error': 'Not supported by this app.'}
        try:
            self._write({'type': 'control_response', 'response': response})
        except OSError:
            pass

    def _finish(self, event):
        usage = event.get('usage') or {}
        self.totals['input'] += usage.get('input_tokens') or 0
        self.totals['cache_read'] += usage.get('cache_read_input_tokens') or 0
        self.totals['cache_write'] += usage.get('cache_creation_input_tokens') or 0
        self.totals['output'] += usage.get('output_tokens') or 0
        self.totals['turns'] += 1
        self.totals['cost'] += event.get('total_cost_usd') or 0
        self.context_tokens = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
        text = event.get('result') or ''
        final_text = text or (self.text_parts[-1] if self.text_parts else '')
        stop_reason = event.get('stop_reason') or ''
        is_error = bool(event.get('is_error')) or stop_reason == 'refusal' or str(event.get('subtype', '')).startswith('error')
        if self.interrupted:
            self.result = {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': final_text}
        elif is_error:
            detail = final_text or stop_reason or event.get('subtype') or 'Claude Code failed'
            kind = 'refused' if stop_reason == 'refusal' else classify_error(detail)
            self.result = {'ok': False, 'kind': kind, 'error': SIGN_IN_HELP if kind == 'signed_out' else detail[:500], 'text': ''}
        elif not text and classify_error(final_text) in ('refused', 'signed_out'):
            kind = classify_error(final_text)
            self.result = {'ok': False, 'kind': kind, 'error': SIGN_IN_HELP if kind == 'signed_out' else final_text[:500], 'text': ''}
        else:
            kind = classify_error(text) if text.startswith(('API Error', 'Failed to authenticate')) else ''
            if kind:
                self.result = {'ok': False, 'kind': kind, 'error': SIGN_IN_HELP if kind == 'signed_out' else text[:500], 'text': ''}
            else:
                self.result = {'ok': True, 'text': final_text}
        self.result['usage'] = usage
        self.done.set()

    def _on_exit(self):
        if not self.done.is_set():
            tail = ' '.join(self.stderr_tail[-3:])
            kind = classify_error(tail)
            if 'No conversation found' in tail:
                kind = 'lost_session'
            self.result = {'ok': False, 'kind': kind if kind != 'other' else 'crashed',
                           'error': (SIGN_IN_HELP if kind == 'signed_out' else tail[:300]) or 'Claude Code stopped unexpectedly.', 'text': ''}
            self.done.set()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def turn(self, text, callback, timeout):
        """Send one message and wait for its reply. Returns {ok, text} or {ok: False, kind, error}."""
        with self.turn_lock:
            self.last_used = time.time()
            if not self.alive():
                try:
                    self._spawn()
                except OSError:
                    return {'ok': False, 'kind': 'missing', 'error': MISSING_HELP, 'text': ''}
            self.callback, self.interrupted = callback, False
            self.result, self.text_parts = None, []
            self.done.clear()
            t0 = time.time()
            try:
                self._write({'type': 'user', 'message': {'role': 'user', 'content': text}, 'parent_tool_use_id': None, 'session_id': self.session_id})
            except OSError as exc:
                self.close()
                return {'ok': False, 'kind': 'crashed', 'error': f'Claude Code stopped ({exc})', 'text': ''}
            if not self.done.wait(timeout):
                self.interrupt()
                if not self.done.wait(10):
                    self.close()
                    return {'ok': False, 'kind': 'timeout', 'error': 'Claude Code took too long.', 'text': ''}
            result = dict(self.result)
            result['elapsed_s'] = round(time.time() - t0, 1)
            self.resume = True                  # from now on this session exists on disk
            self.last_used = time.time()
            return result

    def interrupt(self):
        if self.alive():
            self.interrupted = True
            try:
                self._write({'type': 'control_request', 'request_id': 'req_' + uuid.uuid4().hex[:12], 'request': {'subtype': 'interrupt'}})
            except OSError:
                pass

    def close(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class DeepSeekEngine:
    """DeepSeek flash through the provider named in settings.json, with its own saved conversation for this project."""
    name = 'deepseek'
    label = 'DeepSeek'

    def __init__(self, path, system_prompt):
        self.path, self.system_prompt = path, system_prompt
        self.session = 'meetings-' + uuid.uuid4().hex
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.compacted = False
        self.context_tokens = 0
        self.totals = {'input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'turns': 0, 'cost': 0.0}
        try:
            with open(path, encoding='utf-8') as f:
                self.messages = json.load(f)
        except (OSError, ValueError):
            self.messages = []

    @property
    def fresh(self):
        return not self.messages

    def alive(self):
        return True

    def reset(self):
        self.messages = []
        self._save()

    def _save(self):
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.messages, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def turn(self, text, callback, timeout):
        with self.lock:
            self.stop.clear()
            t0 = time.time()
            messages = [{'role': 'system', 'content': self.system_prompt}] + self.messages + [{'role': 'user', 'content': text}]
            result = stream_deepseek(messages, callback, self.stop, timeout=timeout, session=self.session)
            if self.stop.is_set():
                result = {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': result.get('text') or ''}
            if result.get('ok'):
                self.messages += [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': result['text']}]
                usage = result.get('usage') or {}
                self.totals['input'] += usage.get('prompt_tokens') or 0
                self.totals['output'] += usage.get('completion_tokens') or 0
                self.totals['turns'] += 1
                self.context_tokens = usage.get('prompt_tokens') or 0
                self._save()
            elif result.get('kind') == 'interrupted':
                self.messages.append({'role': 'user', 'content': text})
                self.messages.append({'role': 'assistant', 'content': result.get('text') or '(stopped)'})
                self._save()
            result['elapsed_s'] = round(time.time() - t0, 1)
            return result

    def interrupt(self):
        self.stop.set()

    def close(self):
        pass


class HarnessEngine:
    """The model picked in Flock, with its own saved conversation for this project (one per model). No tools and no
    server-side session: the conversation is sent each turn, and when it outgrows the model's context the app rebuilds it
    from the project record (live.py asks over_budget() before each message)."""
    CHARS_PER_TOKEN = 3.0               # on the safe side for English with names and numbers
    REPLY_TOKENS = 4000

    def __init__(self, path, model_id, system_prompt, session):
        self.model_id = model_id
        self.name = 'h:' + model_id
        self.path, self.system_prompt, self.session = path, system_prompt, session
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.compacted = False
        self.effort, self.wait = None, 0     # set by live.py for each turn
        self.context_tokens = 0
        self.totals = {'input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'turns': 0, 'cost': 0.0}
        try:
            with open(path, encoding='utf-8') as f:
                self.messages = json.load(f)
        except (OSError, ValueError):
            self.messages = []

    @property
    def info(self):
        model = harness_link.current()
        if model and model['id'] == self.model_id:
            return model
        return {'id': self.model_id, 'name': (harness_link.models().get(self.model_id) or {}).get('name') or self.model_id,
                'context': int((harness_link.models().get(self.model_id) or {}).get('context') or 128000), 'max_output': 16384}

    @property
    def label(self):
        return self.info['name'] if self.model_id != 'selected' else "Flock's model"

    def budget_chars(self):
        """How much of the model's context the conversation may fill, in characters."""
        tokens = min(int(self.info.get('context') or 128000), 200000) * 0.8 - self.REPLY_TOKENS
        return int(max(6000, tokens) * self.CHARS_PER_TOKEN)

    @property
    def handoff_chars(self):
        """How much of a running meeting's transcript a handoff may carry."""
        return min(60000, int(self.budget_chars() * 0.3))

    def used_chars(self):
        return len(self.system_prompt) + sum(len(str(m.get('content') or '')) for m in self.messages)

    def over_budget(self, coming=2000):
        return bool(self.messages) and self.used_chars() + coming > self.budget_chars() * 0.7

    @property
    def fresh(self):
        return not self.messages

    def alive(self):
        return True

    def reset(self):
        self.messages = []
        self._save()

    def _save(self):
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.messages, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def turn(self, text, callback, timeout):
        with self.lock:
            self.stop.clear()
            t0 = time.time()
            messages = [{'role': 'system', 'content': self.system_prompt}] + self.messages + [{'role': 'user', 'content': text}]
            result = harness_link.stream(self.model_id, messages, callback, self.stop, timeout=timeout, effort=self.effort,
                                         max_tokens=min(self.REPLY_TOKENS * 2, int(self.info.get('max_output') or 16384)),
                                         session=self.session, wait=self.wait)
            if self.stop.is_set():
                result = {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': result.get('text') or ''}
            if result.get('ok'):
                self.messages += [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': result['text']}]
                usage = result.get('usage') or {}
                self.totals['input'] += usage.get('prompt_tokens') or 0
                self.totals['output'] += usage.get('completion_tokens') or 0
                self.totals['turns'] += 1
                self.context_tokens = usage.get('prompt_tokens') or 0
                self._save()
            elif result.get('kind') == 'interrupted':
                self.messages.append({'role': 'user', 'content': text})
                self.messages.append({'role': 'assistant', 'content': result.get('text') or '(stopped)'})
                self._save()
            result['elapsed_s'] = round(time.time() - t0, 1)
            return result

    def interrupt(self):
        self.stop.set()

    def close(self):
        pass


def stream_deepseek(messages, callback=None, stop=None, timeout=300, session=None, effort=None, max_tokens=8000):
    """One streamed completion with account failover. Returns {ok, text, usage} or {ok: False, kind, error}."""
    provider = settings.deepseek()
    if provider is None:
        return {'ok': False, 'kind': 'other', 'error': 'DeepSeek is not set up on this machine.', 'text': ''}
    model_id = provider.approved_model(DEEPSEEK_MODEL)
    base, keys = provider.go_keys()
    body = {'model': model_id, 'messages': messages, 'stream': True, 'max_tokens': max_tokens,
            'stream_options': {'include_usage': True}}
    if effort:
        body['reasoning_effort'] = effort
    data = json.dumps(body).encode('utf-8')
    session = session or 'meetings-' + uuid.uuid4().hex
    last_error, last_kind = 'DeepSeek did not answer.', 'other'
    t0 = time.time()

    def scrub(text):
        for key in keys:
            text = text.replace(key, '<KEY>')
        return text

    for attempt in range(4):
        retry_filter = False
        for key in keys:
            request = urllib.request.Request(base + '/chat/completions', data=data, method='POST', headers={
                'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                'User-Agent': getattr(provider, 'UA', 'Meetings'), 'x-opencode-session': session})
            parts, usage, finish, thinking_said = [], None, None, False
            try:
                with urllib.request.urlopen(request, timeout=min(timeout, 180)) as response:
                    for raw in response:
                        if stop is not None and stop.is_set():
                            return {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': ''.join(parts)}
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
                if finish == 'content_filter':
                    last_error, last_kind = 'DeepSeek\'s filter stopped this reply.', 'refused'
                    retry_filter = True
                    break
                try:
                    provider.remember_active_key(key) if key != keys[0] and hasattr(provider, 'remember_active_key') else None
                    getattr(provider, 'log_jsonl', lambda *a: None)('ds.jsonl', {'t': time.strftime('%Y-%m-%dT%H:%M:%S'), 'model': model_id, 'usage': usage, 'ok': True, 'app': 'meetings'})
                except Exception:
                    pass
                return {'ok': True, 'text': ''.join(parts), 'usage': usage, 'finish': finish, 'elapsed_s': round(time.time() - t0, 1)}
            except urllib.error.HTTPError as exc:
                detail = scrub(exc.read().decode('utf-8', 'replace'))[:300]
                last_error, last_kind = f'DeepSeek error {exc.code}: {detail}', 'other'
                if exc.code in (401, 402, 403, 429) or 'UsageLimit' in detail:
                    last_kind = 'limit'
                    continue
                break
            except OSError as exc:
                last_error, last_kind = f'DeepSeek connection problem: {type(exc).__name__}', 'other'
                break
        if not retry_filter and last_kind == 'limit':
            break
        if stop is not None and stop.is_set():
            break
        time.sleep(1.5)
    return {'ok': False, 'kind': last_kind, 'error': last_error, 'text': ''}


def oneshot_claude(prompt, effort, cwd, timeout=1800):
    """One separate Opus 5.5 run with no tools that never touches the project's session: the proper summary."""
    cmd = [claude_exe(), '-p', '--output-format', 'stream-json', '--verbose', '--model', MODEL, '--effort', effort,
           '--setting-sources', '', '--settings', SETTINGS, '--strict-mcp-config', '--no-session-persistence', '--tools', '']
    return _run_claude(cmd, prompt, cwd, timeout=timeout)


def read_recording(prompt, cwd, callback=None, timeout=1800, stop=None):
    """One Opus 5.5 run that reads an imported meeting: the transcript in the prompt, and the video's pictures with Read.
    It can only look (Read, Glob, Grep inside the project), and it never touches the project's sessions."""
    cmd = [claude_exe(), '-p', '--output-format', 'stream-json', '--verbose', '--model', MODEL, '--effort', READER_EFFORT,
           '--setting-sources', '', '--settings', SETTINGS, '--strict-mcp-config', '--no-session-persistence',
           '--tools', 'Read,Glob,Grep', '--permission-mode', 'dontAsk', '--allowedTools', 'Read', 'Glob', 'Grep']
    return _run_claude(cmd, prompt, cwd, callback, timeout, stop)


def run_helper(request, cwd, session_id, resume, role_file, mcp_file, own_idea, callback=None, timeout=900, stop=None):
    """One piece of work for the helper. Its session is resumed each time, so it remembers its earlier work in this project."""
    cmd = [claude_exe(), '-p', '--output-format', 'stream-json', '--verbose', '--model', MODEL, '--effort', HELPER_EFFORT,
           '--autocompact', '100000', '--append-system-prompt-file', role_file, '--setting-sources', '', '--settings', SETTINGS,
           '--strict-mcp-config', '--mcp-config', mcp_file, '--tools', HELPER_TOOLS]
    cmd += ['--resume', session_id] if resume else ['--session-id', session_id]
    if own_idea:
        cmd += ['--permission-mode', 'dontAsk', '--allowedTools', *OWN_IDEA_ALLOW, '--disallowedTools', *OWN_IDEA_DENY]
    else:
        cmd += ['--dangerously-skip-permissions']
    return _run_claude(cmd, request, cwd, callback, timeout, stop)


def _run_claude(cmd, prompt, cwd, callback=None, timeout=1800, stop=None):
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=clean_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding='utf-8', errors='replace', creationflags=settings.NO_WINDOW)
    except OSError:
        return {'ok': False, 'kind': 'missing', 'error': MISSING_HELP, 'text': ''}
    errors = []
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()

    def feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed, daemon=True).start()
    timer = threading.Timer(timeout, proc.kill)
    timer.start()

    def stop_watch():
        while proc.poll() is None:
            if stop is not None and stop.wait(0.5):
                proc.kill()
                return
            if stop is None:
                time.sleep(0.5)

    threading.Thread(target=stop_watch, daemon=True).start()
    result = None
    usage = {}
    wrong = ''
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get('type')
        if kind == 'assistant':
            message = event.get('message') or {}
            model = message.get('model') or ''
            if model and model != '<synthetic>' and not model.startswith(MODEL):
                wrong = model
            for block in message.get('content') or []:
                if block.get('type') == 'tool_use' and callback:
                    callback('tool', describe_tool(block.get('name') or '', block.get('input')))
        elif kind == 'result':
            result = event
            usage = event.get('usage') or {}
    proc.wait()
    timer.cancel()
    elapsed = round(time.time() - t0, 1)
    if stop is not None and stop.is_set():
        return {'ok': False, 'kind': 'interrupted', 'error': 'Stopped', 'text': '', 'elapsed_s': elapsed}
    if result is None:
        detail = ''.join(errors)[-300:]
        kind = 'lost_session' if 'No conversation found' in detail else classify_error(detail)
        return {'ok': False, 'kind': kind, 'error': SIGN_IN_HELP if kind == 'signed_out' else detail or 'Claude Code stopped unexpectedly.', 'text': '', 'elapsed_s': elapsed}
    text = result.get('result') or ''
    if result.get('is_error') or result.get('stop_reason') == 'refusal' or text.startswith(('API Error', 'Failed to authenticate')):
        kind = 'refused' if result.get('stop_reason') == 'refusal' else classify_error(text)
        return {'ok': False, 'kind': kind, 'error': SIGN_IN_HELP if kind == 'signed_out' else text[:500] or 'Claude Code refused.', 'text': '', 'elapsed_s': elapsed, 'usage': usage}
    return {'ok': True, 'text': text, 'elapsed_s': elapsed, 'usage': usage, 'wrong_model': wrong, 'cost': result.get('total_cost_usd') or 0}
