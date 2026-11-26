"""Turns a meeting recording into words, voices and pictures, with no window, in the speech engine's venv.

A recording has one audio track with everyone mixed together, so voices are told apart afterwards: every phrase gets a
voice print from the engine's speaker model (on the CPU) and phrases with matching prints are grouped as Speaker 1, 2...
Whisper holds the GPU only while this process runs, and lets go as soon as the words are done ("gpu_free").

  import_host.py <recording> <work dir> [--people N] [--prompt TEXT] [--model large-v3-turbo] [--language en]

Stdout is JSON lines: step {name, text}, progress {name, done, total}, gpu_free, error {message}, done {ok}.
Writes <work dir>/words.json once Whisper is done, so a retry skips it, and <work dir>/result.json at the end.
"""
import argparse
import gc
import itertools
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import settings  # noqa: E402

settings.pass_venv_to_children()        # any worker the engine starts with this Python (a model download) needs the venv too

# The speech engine (OpenWhisper, MIT): its source folder, from the app or this machine's settings.  publish-scan: ok
ENGINE = os.environ.get('FLOCK_MEETINGS_ENGINE') or os.environ.get('KC_MEETINGS_ENGINE') or settings.ENGINE
if not ENGINE or not os.path.isdir(os.path.join(ENGINE, 'meeting')):
    for line in ({'ev': 'error', 'message': 'The speech engine is not installed. Run: python install.py --with-meetings'},
                 {'ev': 'done', 'ok': False}):
        print(json.dumps(line), flush=True)
    sys.exit(1)
os.chdir(ENGINE)
sys.path.insert(1, ENGINE)

_out = sys.stdout
sys.stdout = sys.stderr                 # libraries print; stdout belongs to the protocol
_lock = threading.Lock()

# Before anything loads Whisper: without these two calls it quietly falls back to the CPU (see capture_host.py).
from services import native_libraries   # noqa: E402

native_libraries.register_cuda_dll_directories()
native_libraries.preload_cuda_libraries()

import numpy as np  # noqa: E402

SAMPLE_RATE = 16000
MERGE_DISTANCE = 0.45       # average-linkage stop point, as cosine distance between voice prints
MIN_EMBED_S = 1.0           # phrases this long are grouped into voices
MIN_SHORT_S = 0.45          # shorter ones ("sounds good") join a voice only when their print clearly matches it, else borrow a neighbour's
MAX_EMBED_S = 8.0
MAX_CLUSTERED = 1500        # the longest phrases are grouped; the rest join the nearest group


def send(event, **fields):
    with _lock:
        try:
            _out.write(json.dumps({'ev': event, **fields}, ensure_ascii=False, default=str) + '\n')
            _out.flush()
        except (OSError, ValueError):
            pass


def fail(message):
    send('error', message=message)
    send('done', ok=False)
    sys.stderr.flush()
    os._exit(1)


# ---- words ----

def transcribe(audio, duration, args):
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    import whisper_model
    args.model = whisper_model.choose(args.model)
    try:
        whisper_model.ensure(args.model, lambda text: send('step', name='words', text=text))
    except Exception as exc:            # noqa: BLE001
        fail(f'The Whisper model could not be downloaded: {exc}')
    cuda = whisper_model.has_cuda()
    send('step', name='words', text='Loading Whisper' + (' onto your graphics card' if cuda else ''))
    t0 = time.time()
    model = None
    for device, compute in ([('cuda', 'float16'), ('cuda', 'int8_float16')] if cuda else []) + [('cpu', 'int8')]:
        try:
            model = WhisperModel(args.model, device=device, compute_type=compute, local_files_only=True)
            break
        except Exception as exc:        # noqa: BLE001
            print(f'Whisper on the {device} with {compute} failed: {exc}', file=sys.stderr)
    if model is None:
        fail('Whisper could not load (see host.log in the import work folder).')
    print(f'Whisper loaded in {time.time() - t0:.1f}s', file=sys.stderr, flush=True)
    send('step', name='words', text='Transcribing')
    options = dict(language=args.language or None, word_timestamps=True, initial_prompt=args.prompt or None)
    try:
        pipeline = BatchedInferencePipeline(model=model)
        segments, _info = pipeline.transcribe(audio, batch_size=args.batch, **options)
        segments = iter(segments)
        first = next(segments, None)
        chain = ([first] if first is not None else [])
    except Exception as exc:            # noqa: BLE001  (fall back to the plain, slower way)
        print(f'batched transcription failed, going one by one: {exc}', file=sys.stderr)
        segments, _info = model.transcribe(audio, beam_size=5, vad_filter=True, condition_on_previous_text=False, **options)
        segments, chain = iter(segments), []
    rows, last_sent = [], 0.0
    for segment in itertools.chain(chain, segments):        # one at a time, so the progress bar moves as Whisper goes
        words = [{'s': round(w.start, 2), 'e': round(w.end, 2), 'w': w.word, 'p': round(w.probability or 0, 3)} for w in (segment.words or [])]
        rows.append({'s': round(segment.start, 2), 'e': round(segment.end, 2), 'text': segment.text.strip(), 'words': words})
        if time.time() - last_sent > 1.5:
            last_sent = time.time()
            send('progress', name='words', done=round(segment.end, 1), total=round(duration, 1))
    print(f'Whisper finished {duration:.0f}s of audio in {time.time() - t0:.1f}s', file=sys.stderr, flush=True)
    del model
    gc.collect()
    send('gpu_free')
    return rows


# ---- voices ----

class VoicePrints:
    """The engine's speaker model (WeSpeaker ResNet34) with a few CPU threads, since a whole meeting is done at once."""

    def __init__(self, model_path):
        import onnxruntime
        from meeting.diarize.fbank import apply_cmn, compute_fbank
        self.apply_cmn, self.compute_fbank = apply_cmn, compute_fbank
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        options.inter_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(model_path, sess_options=options, providers=['CPUExecutionProvider'])
        inputs = self.session.get_inputs()
        self.input = inputs[0].name
        self.length = next((i.name for i in inputs[1:] if 'len' in i.name.lower()), None)
        outputs = [o.name for o in self.session.get_outputs()]
        self.output = 'embs' if 'embs' in outputs else None

    def embed(self, audio):
        feats = self.compute_fbank(np.asarray(audio, dtype=np.float32))
        if feats.shape[0] == 0:
            return None
        batch = self.apply_cmn(feats)[np.newaxis].astype(np.float32)
        feeds = {self.input: batch}
        if self.length:
            feeds[self.length] = np.array([batch.shape[1]], dtype=np.int64)
        vector = np.asarray(self.session.run([self.output] if self.output else None, feeds)[0], dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        return vector / norm if np.isfinite(norm) and norm > 1e-8 else None


def phrases_from(rows):
    """Split the words at pauses and sentence ends, so each phrase is most likely one person talking."""
    words = [w for row in rows for w in row['words'] if w['w'].strip()]
    phrases, current = [], None
    for word in words:
        if current is not None:
            gap = word['s'] - current['e']
            length = current['e'] - current['s']
            sentence_end = current['words'][-1]['w'].strip().endswith(('.', '?', '!'))
            if gap > 0.6 or length > 12 or (sentence_end and gap > 0.12 and length > 1.2):
                phrases.append(current)
                current = None
        if current is None:
            current = {'s': word['s'], 'e': word['e'], 'words': [word]}
        else:
            current['e'] = max(current['e'], word['e'])
            current['words'].append(word)
    if current is not None:
        phrases.append(current)
    for phrase in phrases:
        phrase['text'] = ''.join(w['w'] for w in phrase['words']).strip()
    return phrases


def average_linkage(dist, threshold):
    from meeting.diarize.clustering import _average_linkage
    return _average_linkage(dist, threshold)


def tell_voices_apart(audio, phrases, expected=None):
    """Label every phrase with a voice: 'S1' for the first voice heard, 'S2' for the next, and so on."""
    from services.components import ensure_speaker_model, speaker_model_path
    model = speaker_model_path()
    if not model:
        send('step', name='voices', text='Downloading the voice model (about 25 MB, first time only)')
        try:
            model = ensure_speaker_model()
        except Exception as exc:
            fail(f'The voice model could not be downloaded ({exc}), so the voices could not be told apart. Press Try again.')
    if not model or not os.path.isfile(model):
        # Never lump everyone together quietly. Seen 2026-11-06: started from Microsoft Store Python, the app could not see
        # the model in AppData at all, and both meetings came out as one speaker.
        fail("The engine's voice model is not visible to the app, so the voices could not be told apart. Start Meetings "
             "with the Python it was installed with (not the Microsoft Store one), then press Try again.")
    prints = VoicePrints(model)
    send('step', name='voices', text='Telling the voices apart')
    vectors, short, last_sent = {}, {}, 0.0
    for i, phrase in enumerate(phrases):
        length = phrase['e'] - phrase['s']
        if length >= MIN_SHORT_S:
            middle = (phrase['s'] + phrase['e']) / 2
            half = min(length, MAX_EMBED_S) / 2 + 0.05
            piece = audio[int(max(0.0, middle - half) * SAMPLE_RATE):int((middle + half) * SAMPLE_RATE)]
            try:
                vector = prints.embed(piece)
            except Exception as exc:    # noqa: BLE001
                print('voice print failed:', exc, file=sys.stderr)
                vector = None
            if vector is not None:
                (vectors if length >= MIN_EMBED_S else short)[i] = vector
        if time.time() - last_sent > 1.5:
            last_sent = time.time()
            send('progress', name='voices', done=i + 1, total=len(phrases))
    if not vectors:
        for phrase in phrases:
            phrase['voice'] = 'S1'
        return {'note': 'Too little speech to tell voices apart.'}

    # Group the longest phrases by voice; their prints are the most reliable.
    ids = sorted(vectors, key=lambda i: phrases[i]['e'] - phrases[i]['s'], reverse=True)[:MAX_CLUSTERED]
    matrix = np.stack([vectors[i] for i in ids])
    dist = np.clip(1.0 - matrix @ matrix.T, 0.0, 2.0)
    groups = average_linkage(dist, MERGE_DISTANCE)
    seconds = lambda members: sum(phrases[ids[m]]['e'] - phrases[ids[m]]['s'] for m in members)     # noqa: E731
    total = sum(seconds(g) for g in groups) or 1.0

    def centroid(members):
        c = matrix[members].mean(axis=0)
        return c / (np.linalg.norm(c) or 1.0)

    # A group with only a few seconds of speech is usually a stray, not a person: fold it into the nearest real voice.
    groups.sort(key=seconds, reverse=True)
    big = [g for g in groups if seconds(g) >= max(8.0, 0.015 * total)] or groups[:1]
    for small in [g for g in groups if g not in big]:
        c = centroid(small)
        best = max(big, key=lambda g: float(centroid(g) @ c))
        best.extend(small)
    # When the user said how many people were there, merge the closest voices until it fits, unless they really differ.
    if expected:
        while len(big) > expected:
            pairs = [(float(centroid(a) @ centroid(b)), x, y) for x, a in enumerate(big) for y, b in enumerate(big) if x < y]
            similarity, x, y = max(pairs)
            if similarity < 0.3:
                break
            big[x].extend(big[y])
            del big[y]
    centroids = [centroid(g) for g in big]
    label_of = {}
    for g, members in enumerate(big):
        for m in members:
            label_of[ids[m]] = g
    for i, vector in vectors.items():
        if i not in label_of:
            label_of[i] = int(np.argmax([float(c @ vector) for c in centroids]))
    known = sorted(label_of)
    for i, vector in short.items():
        scores = sorted(((float(c @ vector), g) for g, c in enumerate(centroids)), reverse=True)
        if scores[0][0] >= 0.5 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.12):
            label_of[i] = scores[0][1]
    # The rest ("yes", "okay") take the voice of the nearest phrase that has a reliable one.
    for i in range(len(phrases)):
        if i in label_of:
            continue
        before = next((k for k in reversed(known) if k < i), None)
        after = next((k for k in known if k > i), None)
        if before is None or (after is not None and phrases[after]['s'] - phrases[i]['e'] < phrases[i]['s'] - phrases[before]['e']):
            label_of[i] = label_of[after] if after is not None else 0
        else:
            label_of[i] = label_of[before]
    # Number the voices in the order they are first heard.
    order = {}
    for i in range(len(phrases)):
        group = label_of[i]
        if group not in order:
            order[group] = f'S{len(order) + 1}'
        phrases[i]['voice'] = order[group]
    similar = []
    for x in range(len(centroids)):
        for y in range(x + 1, len(centroids)):
            similar.append({'a': order.get(x), 'b': order.get(y), 'similarity': round(float(centroids[x] @ centroids[y]), 2)})
    return {'similar': sorted(similar, key=lambda s: -s['similarity'])[:6]}


def speaker_table(phrases):
    table = {}
    for phrase in phrases:
        row = table.setdefault(phrase['voice'], {'seconds': 0.0, 'phrases': 0, 'first': phrase['s'], 'longest': []})
        row['seconds'] += phrase['e'] - phrase['s']
        row['phrases'] += 1
        row['longest'].append((phrase['e'] - phrase['s'], (phrase['s'] + phrase['e']) / 2))
    for row in table.values():
        row['seconds'] = round(row['seconds'], 1)
        row['moments'] = [round(t, 1) for _, t in sorted(row['longest'], reverse=True)[:2]]
        del row['longest']
    return table


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('recording')
    parser.add_argument('work')
    parser.add_argument('--people', type=int, default=0)
    parser.add_argument('--prompt', default='')
    parser.add_argument('--model', default='')
    parser.add_argument('--language', default='en')
    parser.add_argument('--batch', type=int, default=8)
    args = parser.parse_args()
    os.makedirs(args.work, exist_ok=True)
    words_path = os.path.join(args.work, 'words.json')
    stat = os.stat(args.recording)
    key = {'size': stat.st_size, 'mtime': int(stat.st_mtime), 'model': args.model}

    send('step', name='audio', text='Reading the audio')
    from faster_whisper.audio import decode_audio
    import video
    try:
        audio = decode_audio(args.recording, sampling_rate=SAMPLE_RATE)
    except Exception as exc:            # noqa: BLE001
        fail(f'Could not read the audio in this file: {exc}')
    duration = len(audio) / SAMPLE_RATE
    if duration < 1:
        fail('This recording has no sound in it.')
    created = ''
    try:
        import av
        with av.open(args.recording) as container:
            created = container.metadata.get('creation_time') or container.metadata.get('date') or ''
    except Exception:                   # noqa: BLE001
        pass
    send('info', duration=round(duration, 1), created=created)       # when it was recorded, if the file says

    rows = None
    try:
        with open(words_path, encoding='utf-8') as f:
            saved = json.load(f)
        if saved.get('key') == key:
            rows = saved['rows']
            send('gpu_free')
    except (OSError, ValueError, KeyError):
        pass
    if rows is None:
        rows = transcribe(audio, duration, args)
        with open(words_path, 'w', encoding='utf-8') as f:
            json.dump({'key': key, 'rows': rows}, f, ensure_ascii=False)
    if not any(row['words'] for row in rows):
        fail('Whisper heard no speech in this recording.')

    phrases = phrases_from(rows)
    voices = tell_voices_apart(audio, phrases, args.people or None)
    speakers = speaker_table(phrases)
    del audio

    frames_dir = os.path.join(args.work, 'frames')
    screens, faces = [], []
    try:
        rec = video.Recording(args.recording)
        has_video = rec.has_video
        rec.close()
    except Exception:                   # noqa: BLE001
        has_video = False
    if has_video:
        send('step', name='pictures', text='Picking out what was on screen')
        try:
            screens = video.screen_changes(args.recording, frames_dir, max_frames=int(min(220, max(20, duration / 9))))
            for voice, row in speakers.items():
                for item in video.frames_at(args.recording, row['moments'], frames_dir, prefix=f'voice_{voice}'):
                    faces.append({'voice': voice, **item})
        except Exception as exc:        # noqa: BLE001
            print('pictures failed:', type(exc).__name__, exc, file=sys.stderr)
    result = {'duration': round(duration, 1), 'video': has_video,
              'phrases': [{'s': p['s'], 'e': p['e'], 'voice': p['voice'], 'text': p['text']} for p in phrases],
              'speakers': speakers, 'voices': voices, 'screens': screens, 'voice_frames': faces}
    with open(os.path.join(args.work, 'result.json'), 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    send('done', ok=True)
    sys.stderr.flush()
    os._exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:        # noqa: BLE001
        import traceback
        traceback.print_exc()
        fail(f'{type(exc).__name__}: {exc}')
