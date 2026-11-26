"""Which Whisper model to use, and getting it onto this machine the first time (the speech engine itself never downloads
without asking, and its download worker needs desktop libraries a server may not have). Imported by the hidden workers."""
import os
import threading
import time

import settings

SIZES = {'large-v3-turbo': 1600, 'turbo': 1600, 'large-v3': 3100, 'medium': 1500, 'small': 480, 'base': 150, 'tiny': 75}
FILES = ['config.json', 'preprocessor_config.json', 'model.bin', 'tokenizer.json', 'vocabulary.*']   # as faster-whisper fetches


def has_cuda():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def choose(asked=''):
    """The model to load: the one asked for, else the machine's setting, else large-v3-turbo on an NVIDIA GPU and small on
    the CPU (large models on a CPU fall minutes behind a live meeting)."""
    return asked or str(settings.value('whisper_model', ('FLOCK_MEETINGS_WHISPER_MODEL', 'KC_MEETINGS_WHISPER_MODEL'), '')).strip() or (
        'large-v3-turbo' if has_cuda() else 'small')


def _cached(name):
    from faster_whisper.utils import download_model
    try:
        download_model(name, local_files_only=True)
        return True
    except Exception:
        return False


def ensure(name, say):
    """Download the model into the Hugging Face cache if it is not there yet. say(text) reports progress. Raises on failure."""
    if os.path.isdir(name) or _cached(name):
        return
    from faster_whisper.utils import _MODELS
    from huggingface_hub import constants, snapshot_download
    repo = _MODELS.get(name, name)
    folder = os.path.join(constants.HF_HUB_CACHE, 'models--' + repo.replace('/', '--'))
    total = SIZES.get(name)
    say(f'Downloading the Whisper model {name}' + (f' (about {total} MB)' if total else '') + ', first time only')
    done = threading.Event()

    def report():
        while not done.wait(2):
            got = 0
            for root, _dirs, files in os.walk(os.path.join(folder, 'blobs')):     # snapshots/ only links to these
                for f in files:
                    try:
                        got += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            got //= 1024 * 1024
            say(f'Downloading the Whisper model {name}: {got} MB' + (f' of about {total} MB' if total else '') + ', first time only')

    threading.Thread(target=report, daemon=True).start()
    try:
        snapshot_download(repo, allow_patterns=FILES)
    finally:
        done.set()
