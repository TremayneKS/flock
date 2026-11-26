"""Where things are on this machine, and who the user is. Nothing machine-specific lives in the code.

Each value comes from, in order: an environment variable (FLOCK_MEETINGS_* or legacy KC_MEETINGS_*), settings.json next to
this file (or the file FLOCK_MEETINGS_SETTINGS / KC_MEETINGS_SETTINGS names; never in git), then a default that works for
a fresh install.

settings.json (every key optional):
  {"data": "~/Meetings",               project folders (FLOCK_MEETINGS_ROOT / KC_MEETINGS_ROOT)
   "port": 8770,                       (FLOCK_MEETINGS_PORT / KC_MEETINGS_PORT)
   "python": "path/to/python",         the Python with the speech engine's packages, for the hidden workers (FLOCK_MEETINGS_PYTHON / KC_MEETINGS_PYTHON;
                                       default: the base Python of "venv" when that is set, else the one running the app)
   "venv": "path/to/venv",             a virtual environment used without its own python.exe (FLOCK_MEETINGS_VENV / KC_MEETINGS_VENV): the app and its
                                       workers run on the Python it was made from and load its packages. For Windows with
                                       Smart App Control on, which blocks a venv's unsigned launcher but not that Python
   "speech_engine": "path",            the speech engine's source folder (FLOCK_MEETINGS_ENGINE / KC_MEETINGS_ENGINE; default: ./engine or ../engine)
   "engine_data": "path",              its scratch folder, emptied after each meeting (default: <data>/.app/engine)
   "hf_home": "path",                  where Whisper and other models are cached (default: Hugging Face's own)
   "user_name": "Sam",                 how the assistants refer to the user (FLOCK_MEETINGS_USER / KC_MEETINGS_USER; default: "the user")
   "user_aliases": ["sam", "samuel"],  names in a recording that mean the user (regexes, matched whole)
   "about_user": "Sam uses she/her.",  one line for the assistants
   "standing_rules": "~/rules.md",     a file of rules the helper follows on this machine
   "helper_deny": ["Bash(mytool *)"],  more tools or commands the helper may not use on its own ideas
   "deepseek_provider": "module",      a module (on python_paths) that gives DeepSeek access as a switchable second model:
                                       approved_model(name), go_keys() -> (base_url, [keys]), UA, remember_active_key(key),
                                       log_jsonl(name, row); with a helper module named by "deepseek_helper" (run(brief, cwd, ...))
   "python_paths": ["path"]}           folders added to sys.path for those modules
"""
import glob
import json
import os
import re
import site
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = os.name == 'nt'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)     # creationflags for hidden helper processes


def _load():
    path = os.environ.get('FLOCK_MEETINGS_SETTINGS') or os.environ.get('KC_MEETINGS_SETTINGS') or os.path.join(HERE, 'settings.json')
    try:
        with open(os.path.expanduser(path), encoding='utf-8-sig') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f'Meetings: {path} could not be read ({exc}); using defaults', file=sys.stderr)
        return {}


SETTINGS = _load()


def value(key, env=None, default=None):
    envs = (env,) if isinstance(env, str) else (env or ())
    for name in envs:
        if name and os.environ.get(name):
            return os.environ[name]
    got = SETTINGS.get(key)
    return default if got in (None, '') else got


def path(p):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(p)))) if p else ''


def _venv_home(venv):
    """The folder of the Python a venv was made from (its pyvenv.cfg "home"), or ''."""
    try:
        with open(os.path.join(venv, 'pyvenv.cfg'), encoding='utf-8') as f:
            for line in f:
                key, _, got = line.partition('=')
                if key.strip().lower() == 'home':
                    return got.strip()
    except OSError:
        pass
    return ''


def _enter_venv(venv):
    """Gives a Python that the venv's own launcher did not start what that launcher would have: the venv's packages, with
    their .pth files (pywin32 needs them), listed by site.getsitepackages() too, where the speech engine looks for its CUDA
    libraries. Found 2026-11-09: Smart App Control blocks the venv's unsigned python.exe, so the app runs on its base Python."""
    if not venv or os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(venv):
        return
    if WINDOWS:
        packages = os.path.join(venv, 'Lib', 'site-packages')
    else:
        packages = next(iter(sorted(glob.glob(os.path.join(venv, 'lib', 'python3*', 'site-packages')))), '')
    if not os.path.isdir(packages) or os.path.normcase(packages) in {os.path.normcase(p) for p in sys.path}:
        return
    site.PREFIXES.insert(0, venv)
    site.addsitedir(packages)


def pass_venv_to_children():
    """For the speech workers: Pythons they start with sys.executable (the speech engine's own Whisper worker) get the venv
    too, through venvboot/sitecustomize.py. Not set for the app itself, so tools its assistant runs are left alone."""
    if not VENV or os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(VENV):
        return
    boot = os.path.join(HERE, 'venvboot')
    paths = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p and os.path.normcase(p) != os.path.normcase(boot)]
    os.environ['PYTHONPATH'] = os.pathsep.join([boot] + paths)
    os.environ['FLOCK_MEETINGS_VENV'] = VENV
    os.environ['KC_MEETINGS_VENV'] = VENV


def _python():
    exe = value('python', ('FLOCK_MEETINGS_PYTHON', 'KC_MEETINGS_PYTHON'))
    if exe:
        return path(exe)
    if VENV:
        home = _venv_home(VENV)
        exe = os.path.join(home, 'python.exe') if WINDOWS and home else os.path.join(VENV, 'bin', 'python')
        if os.path.exists(exe):
            return exe
    exe = sys.executable
    # Workers talk over pipes; pythonw has no console, which is fine, but python.exe is the usual choice when it is there.
    if WINDOWS and os.path.basename(exe).lower() == 'pythonw.exe':
        candidate = os.path.join(os.path.dirname(exe), 'python.exe')
        exe = candidate if os.path.exists(candidate) else exe
    return exe


def _engine():
    got = value('speech_engine', ('FLOCK_MEETINGS_ENGINE', 'KC_MEETINGS_ENGINE'))
    if got:
        return path(got)
    for candidate in (os.path.join(HERE, 'engine'), os.path.join(os.path.dirname(HERE), 'engine')):
        if os.path.isdir(os.path.join(candidate, 'meeting')):
            return candidate
    return ''


def _default_root():
    for cand in ('~/Meetings', '~/KC Meetings'):
        if os.path.exists(os.path.expanduser(cand)):
            return cand
    return '~/Meetings'


ROOT = path(value('data', ('FLOCK_MEETINGS_ROOT', 'KC_MEETINGS_ROOT'), _default_root()))
PORT = int(value('port', ('FLOCK_MEETINGS_PORT', 'KC_MEETINGS_PORT'), 8770))
VENV = path(value('venv', ('FLOCK_MEETINGS_VENV', 'KC_MEETINGS_VENV'), ''))
_enter_venv(VENV)
PYTHON = _python()
ENGINE = _engine()
ENGINE_DATA = path(value('engine_data', ('FLOCK_MEETINGS_ENGINE_DATA', 'KC_MEETINGS_ENGINE_DATA'), os.path.join(ROOT, '.app', 'engine')))
HF_HOME = path(value('hf_home', ('FLOCK_MEETINGS_HF_HOME', 'KC_MEETINGS_HF_HOME'), ''))
NAME = str(value('user_name', ('FLOCK_MEETINGS_USER', 'KC_MEETINGS_USER'), '')).strip()
user = NAME or 'the user'               # mid-sentence: "the user's files"
User = NAME or 'The user'               # at the start of a sentence
ALIASES = [str(a) for a in (SETTINGS.get('user_aliases') or []) if str(a).strip()]
ABOUT_USER = str(value('about_user', None, '')).strip()
STANDING_RULES = path(value('standing_rules', None, ''))
HELPER_DENY = [str(a) for a in (SETTINGS.get('helper_deny') or [])]
DEEPSEEK_PROVIDER = str(value('deepseek_provider', None, '')).strip()
DEEPSEEK_HELPER = str(value('deepseek_helper', None, '')).strip()
for _p in SETTINGS.get('python_paths') or []:
    if path(_p) not in sys.path:
        sys.path.insert(0, path(_p))


def personal(text):
    """Model-facing text written with @user@: the user's name, or "the user" (capitalised where a sentence starts)."""
    if NAME:
        return text.replace('@user@', NAME)
    text = re.sub(r'(^|[.!?]\s+|\n\s*(?:[-*]\s+|\d+\.\s+)?|:\s+|"|>)@user@', lambda m: m.group(1) + 'The user', text)
    return text.replace('@user@', 'the user')


def deepseek():
    """The DeepSeek provider module, or None when this machine has none (the switch is then hidden)."""
    if not DEEPSEEK_PROVIDER:
        return None
    try:
        import importlib
        return importlib.import_module(DEEPSEEK_PROVIDER)
    except Exception as exc:
        print(f'Meetings: DeepSeek provider {DEEPSEEK_PROVIDER!r} could not load ({type(exc).__name__})', file=sys.stderr)
        return None
