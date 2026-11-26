"""Runs the speech engine's meeting engine with no window, in its venv: Whisper on the GPU (or CPU), mic and system audio,
speaker separation on the PC. It exists only while a meeting runs, so Whisper holds the GPU only during meetings.

Talks in JSON lines. Stdout is events (loading, ready, segments, preview, level, status, error, ended, done);
stdin is commands ({"cmd": "end" | "pause" | "resume"}). Closing stdin counts as "end".
After the meeting it deletes the engine's own copy (spooled audio and database rows): the transcript lives in the
project folder and the audio is not kept.
"""
import argparse
import json
import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import settings  # noqa: E402

settings.pass_venv_to_children()        # the engine loads Whisper in a worker it starts with this Python

# The speech engine (OpenWhisper, MIT): its source folder, from the app or this machine's settings.  publish-scan: ok
ENGINE = os.environ.get('FLOCK_MEETINGS_ENGINE') or os.environ.get('KC_MEETINGS_ENGINE') or settings.ENGINE
if not ENGINE or not os.path.isdir(os.path.join(ENGINE, 'meeting')):
    for line in ({'ev': 'error', 'code': 'start_failed', 'message': 'The speech engine is not installed. '
                  'Run: python install.py --with-meetings'}, {'ev': 'done'}):
        print(json.dumps(line), flush=True)
    sys.exit(1)
os.chdir(ENGINE)
sys.path.insert(1, ENGINE)

_out = sys.stdout
sys.stdout = sys.stderr                 # libraries print; stdout belongs to the protocol

# Before anything loads Whisper: without these two calls it quietly falls back to the CPU, as the engine's own main.py knows.
from services import native_libraries

native_libraries.register_cuda_dll_directories()
native_libraries.preload_cuda_libraries()

_out_lock = threading.Lock()
_levels = {}
_levels_lock = threading.Lock()


def send(event, **fields):
    """Never raises: if the app has gone, the meeting must still shut down cleanly and let go of the mic and the GPU."""
    with _out_lock:
        try:
            _out.write(json.dumps({'ev': event, **fields}, ensure_ascii=False, default=str) + '\n')
            _out.flush()
        except (OSError, ValueError):
            pass


def list_devices():
    """Print the microphones and the default speakers, then exit."""
    from meeting.capture.devices import list_input_devices
    import sounddevice as sd
    mics = []
    default_name = ''
    wasapi_name = next((h['name'] for h in sd.query_hostapis() if 'wasapi' in h['name'].lower()), '')
    try:
        if wasapi_name:
            default_index = next(h['default_input_device'] for h in sd.query_hostapis() if h['name'] == wasapi_name)
        else:
            default_index = sd.default.device[0]
        default_name = sd.query_devices(default_index)['name'] if default_index is not None and default_index >= 0 else ''
    except Exception:
        pass
    # The same microphone is listed once per Windows audio system. Showing the modern one keeps the list to real choices.
    # Elsewhere (macOS, Linux) there is one list, so every input that is not a loopback is a choice.
    for device in list_input_devices(sd):
        name = device.name
        if (wasapi_name and device.hostapi != wasapi_name) or '[Loopback]' in name or 'Steam Streaming' in name or '.monitor' in name.lower():
            continue
        mics.append({'name': device.name, 'hostapi': device.hostapi})
    speaker = ''
    try:
        import soundcard
        speaker = soundcard.default_speaker().name
    except Exception:
        pass
    send('devices', mics=mics, default_mic=default_name, speaker=speaker)


def watch_levels(block):
    """Peak loudness per channel, for the 'it can hear you' meters."""
    try:
        import numpy as np
        pcm = block.pcm
        level = float(np.sqrt(np.mean(np.square(pcm.astype('float32'))))) / 32768.0 if len(pcm) else 0.0
        with _levels_lock:
            _levels[block.channel] = max(_levels.get(block.channel, 0.0), level)
    except Exception:
        pass


def level_loop():
    while True:
        time.sleep(0.4)
        with _levels_lock:
            snapshot = dict(_levels)
            _levels.clear()
        if snapshot:
            send('level', mic=round(snapshot.get('mic', 0.0), 4), system=round(snapshot.get('loopback', 0.0), 4))


def clean_old_copies(repository, meetings_root):
    """Anything left by a crashed run: this data folder is only ever used by this app."""
    from meeting.persist.data_lifecycle import delete_meeting_data
    try:
        for row in repository.list_meetings():
            delete_meeting_data(repository, str(row.get('id')), meetings_root)
    except Exception as exc:
        print('cleanup of old meeting rows failed:', type(exc).__name__, file=sys.stderr)
    try:
        for entry in os.scandir(meetings_root):
            if entry.is_dir():
                shutil.rmtree(entry.path, ignore_errors=True)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--title', default='')
    parser.add_argument('--mic-name', default='')
    parser.add_argument('--mic-hostapi', default='')
    parser.add_argument('--model', default='')
    parser.add_argument('--language', default='en')
    parser.add_argument('--devices', action='store_true')
    args = parser.parse_args()
    if args.devices:
        list_devices()
        return

    import whisper_model
    args.model = whisper_model.choose(args.model)
    send('loading', text='Loading Whisper' + (' onto your graphics card' if whisper_model.has_cuda() else ''))
    from config import config
    from meeting.asr import engine as asr_engine
    from meeting.capture import health
    from meeting.engine import MeetingEngine, MeetingEngineOptions
    from meeting.persist.repository import SqlMeetingRepository
    from meeting.persist.data_lifecycle import delete_meeting_data
    from services.components import ensure_speaker_model, speaker_model_path
    from transcriber.local_backend import LocalWhisperBackend

    # The engine starts recording first and loads Whisper afterwards, but the load starves the thread saving the audio, so
    # the first seconds of a meeting get dropped (seen 2026-11-05). Load first, then listen, so nothing is lost once "ready" shows.
    t0 = time.time()
    try:
        import onnxruntime  # noqa: F401  (speaker separation; same reason)
    except ImportError:
        pass
    try:
        whisper_model.ensure(args.model, lambda text: send('loading', text=text))
    except Exception as exc:
        send('error', code='start_failed', message=f'The Whisper model could not be downloaded: {exc}')
        send('done')
        os._exit(1)
    send('loading', text='Loading Whisper' + (' onto your graphics card' if whisper_model.has_cuda() else ''))
    backend = LocalWhisperBackend(model_name=args.model)
    if not backend.is_available():
        send('error', code='start_failed', message=getattr(backend, 'last_error', '') or 'Whisper could not load.')
        send('done')
        os._exit(1)
    print(f'Whisper loaded in {time.time() - t0:.1f}s', file=sys.stderr, flush=True)
    asr_engine.MeetingAsrEngine._construct_backend = lambda self, cls, models: backend
    asr_engine.MeetingAsrEngine._load_backend_model = lambda self, loaded, models: None

    original_observe = health.CaptureSignalMonitor.observe

    def observe(self, block, **kwargs):
        watch_levels(block)
        return original_observe(self, block, **kwargs)

    health.CaptureSignalMonitor.observe = observe

    repository = SqlMeetingRepository()
    os.makedirs(config.MEETINGS_FOLDER, exist_ok=True)
    clean_old_copies(repository, config.MEETINGS_FOLDER)

    mic = ({'name': args.mic_name, 'hostapi': args.mic_hostapi},) if args.mic_name else ()
    options = MeetingEngineOptions(
        title=args.title, cloud_enabled=False, mic_priority=mic, asr_model=args.model, asr_language=args.language,
        diarization_model_path=speaker_model_path(), diarization_model_resolver=ensure_speaker_model,
        speaker_id_backend='local', speaker_id_audio_consent=False, server_bind='localhost', server_port=0,
        spool_root=config.MEETINGS_FOLDER, end_redecode=False, redecode_coverage_guard=False,
        end_polish=False, end_report=False)
    engine = MeetingEngine(options, repository=repository)
    finished = threading.Event()

    def on_event(kind, payload):
        if kind == 'segments':
            rows = [{k: row.get(k) for k in ('id', 'segment_id', 'channel', 'speaker', 'speaker_participant_id',
                                             'text', 'start_s', 'end_s')} for row in payload.get('items') or []]
            send('segments', items=rows, removed=payload.get('removed_ids') or [])
        elif kind == 'status':
            send('status', status=payload.get('status'), capture=payload.get('capture'),
                 diarization=payload.get('diarization_available'), note=payload.get('note'))
        elif kind == 'error':
            send('error', code=payload.get('code'), message=payload.get('message'))
        elif kind == 'ended':
            send('ended', status=payload.get('status'))
            finished.set()

    engine.add_listener(on_event)

    # Words as they are being said, before Whisper commits the full line.
    broadcast = engine._broadcast

    def broadcast_with_preview(message):
        if isinstance(message, dict) and message.get('type') == 'speech_preview':
            send('preview', channel=message.get('channel'), text=message.get('text') or '', final=bool(message.get('final')))
        return broadcast(message)

    engine._broadcast = broadcast_with_preview

    def commands():
        for line in sys.stdin:
            try:
                command = json.loads(line).get('cmd')
            except ValueError:
                continue
            if command == 'end':
                break
            if command == 'pause':
                engine.pause()
                send('paused', paused=True)
            elif command == 'resume':
                engine.resume()
                send('paused', paused=False)
        send('loading', text='Finishing the last lines')
        engine.end()

    try:
        engine.start()
    except Exception as exc:
        send('error', code='start_failed', message=str(exc))
        send('done')
        os._exit(1)
    threading.Thread(target=level_loop, daemon=True).start()
    threading.Thread(target=commands, daemon=True).start()
    send('ready', model=args.model, mic=(engine._mic_device or {}).get('name', ''))
    finished.wait()
    meeting_id = engine.meeting_id
    try:
        engine.wait_for_end()
        engine.shutdown()
    except Exception as exc:
        print('engine shutdown:', type(exc).__name__, exc, file=sys.stderr)
    try:
        delete_meeting_data(repository, meeting_id, config.MEETINGS_FOLDER)
    except Exception as exc:
        print('cleanup failed:', type(exc).__name__, exc, file=sys.stderr)
    send('done')
    sys.stderr.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
