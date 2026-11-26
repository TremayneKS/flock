"""Loads Meetings' speech venv into a Python that the venv's own launcher did not start.

The speech workers put this folder on PYTHONPATH for the Pythons they start (settings.pass_venv_to_children), such as the
speech engine's own Whisper worker, which it starts with sys.executable. Python runs sitecustomize at startup, so those
workers get the venv's packages the way the launcher would have given them, CUDA libraries included. Needed when the app
runs on the venv's base Python: Windows Smart App Control blocks the venv's unsigned python.exe (found 2026-11-09).
Does nothing without FLOCK_MEETINGS_VENV / KC_MEETINGS_VENV, inside the venv itself, or in any other Python version.
"""
import os
import site
import sys


def _enter(venv):
    try:
        with open(os.path.join(venv, 'pyvenv.cfg'), encoding='utf-8') as f:
            cfg = {key.strip().lower(): got.strip() for key, sep, got in (line.partition('=') for line in f) if sep}
    except OSError:
        return
    wanted = (cfg.get('version_info') or cfg.get('version') or '').split('.')[:2]
    if wanted != [str(n) for n in sys.version_info[:2]]:
        return
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(venv)):
        return
    packages = os.path.join(venv, 'Lib', 'site-packages')
    if os.path.isdir(packages):
        site.PREFIXES.insert(0, venv)       # site.getsitepackages() lists the venv, as the speech engine expects
        site.addsitedir(packages)           # its packages, with their .pth files


_venv = os.environ.get('FLOCK_MEETINGS_VENV') or os.environ.get('KC_MEETINGS_VENV')
if _venv:
    _enter(_venv)
