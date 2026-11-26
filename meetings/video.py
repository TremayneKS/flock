"""Pictures from a meeting recording, so Claude can see what was on screen. Runs in the speech engine's venv (PyAV, Pillow, numpy).

  python video.py frame <recording> --at 12:30 [--out DIR]
  python video.py frames <recording> --from 10:00 --to 14:00 [--every 30] [--out DIR]

Saves JPEGs (at most 1568 wide, so menus and file names stay readable) and prints one "time<TAB>path" line per picture.
--out defaults to the project's .copilot/tmp folder when run from a project folder, which the app clears.
"""
import argparse
import os
import sys

import settings  # noqa: F401  (first: loads the speech engine's venv when this runs on its base Python)

import av
import numpy as np
from PIL import Image

MAX_WIDTH = 1568            # the most detail Claude takes in from a picture
THUMB = (96, 60)            # small grey copies for spotting changes; fine enough to notice a menu or a new folder
CLEAR_CHANGE = 24           # a pixel counts as changed when it moves this much (out of 255)
LIVE_SHARE = 0.3            # pixels that change between most keyframes are video (webcams), not something being shown


def parse_time(text):
    """'12:30', '1:02:03' or '750' (seconds) to seconds."""
    parts = [float(p) for p in str(text).strip().split(':')]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def fmt_time(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def file_time(seconds):
    return fmt_time(seconds).replace(':', '-')


class Recording:
    """One video file. Times are seconds from the start of the recording."""

    def __init__(self, path):
        self.path = path
        self.container = av.open(path)
        self.stream = next((s for s in self.container.streams if s.type == 'video'), None)
        self.offset = 0.0
        if self.stream is not None:
            self.stream.thread_type = 'AUTO'
            if self.stream.start_time is not None:
                self.offset = float(self.stream.start_time * self.stream.time_base)
        duration = self.container.duration
        self.duration = float(duration) / av.time_base if duration else 0.0

    @property
    def has_video(self):
        return self.stream is not None

    def close(self):
        self.container.close()

    def _seek(self, t):
        target = int((t + self.offset) / self.stream.time_base)
        self.container.seek(max(0, target), stream=self.stream, backward=True, any_frame=False)

    def frame_at(self, t):
        """The picture showing at time t: back to the keyframe before it, then decode forward to it."""
        self.stream.codec_context.skip_frame = 'DEFAULT'
        self._seek(t)
        best = None
        for frame in self.container.decode(self.stream):
            if frame.time is None:
                continue
            if frame.time - self.offset > t + 0.02 and best is not None:
                break
            best = frame
            if frame.time - self.offset > t + 0.02:
                break
        return best

    def keyframe_thumbs(self):
        """(time, small grey picture) for every keyframe. Only keyframes are decoded, so this is quick."""
        self.stream.codec_context.skip_frame = 'NONKEY'
        self.container.seek(0)
        out = []
        for frame in self.container.decode(self.stream):
            if frame.time is not None:
                out.append((frame.time - self.offset, frame.reformat(width=THUMB[0], height=THUMB[1], format='gray').to_ndarray().astype(np.float32)))
        self.stream.codec_context.skip_frame = 'DEFAULT'
        return out

    def sampled_thumbs(self, every):
        """The same, for files with too few keyframes: decode everything, keep one picture every few seconds."""
        self.stream.codec_context.skip_frame = 'DEFAULT'
        self.container.seek(0)
        out, next_t = [], 0.0
        for frame in self.container.decode(self.stream):
            if frame.time is None:
                continue
            t = frame.time - self.offset
            if t >= next_t:
                out.append((t, frame.reformat(width=THUMB[0], height=THUMB[1], format='gray').to_ndarray().astype(np.float32)))
                next_t = t + every
        return out


def to_image(frame):
    image = frame.to_image()
    if image.width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)), Image.LANCZOS)
    return image


def save(frame, out_dir, name):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    to_image(frame).save(path, quality=80)
    return path


def still_parts(thumbs):
    """Where on screen to look for changes: everywhere except the parts that move all the time, such as people's webcams.
    A click into a folder or a menu opening then still counts, while someone nodding on camera does not."""
    if len(thumbs) < 6:
        return np.ones(thumbs[0][1].shape, dtype=bool) if thumbs else None
    moving = np.zeros(thumbs[0][1].shape, dtype=np.float32)
    for (_, a), (_, b) in zip(thumbs, thumbs[1:]):
        moving += np.abs(a - b) > CLEAR_CHANGE
    keep = moving / (len(thumbs) - 1) < LIVE_SHARE
    return keep if keep.mean() > 0.2 else np.ones_like(keep)


def changed_share(a, b, keep):
    """The share of the still parts of the screen that changed clearly between two small pictures."""
    return float(np.mean((np.abs(a - b) > CLEAR_CHANGE)[keep]))


def pick_changes(thumbs, threshold, every_s, keep, gap_s=2.0):
    """A picture whenever the screen differs enough from the last one kept (a new page, folder, menu or document), at least
    one every every_s seconds, and never two within gap_s seconds, so an animation does not fill the list."""
    kept, last, last_t = [], None, -1e9
    for t, small in thumbs:
        if last is None or t - last_t >= every_s or (t - last_t >= gap_s and changed_share(small, last, keep) > threshold):
            kept.append(t)
            last, last_t = small, t
    return kept


def screen_changes(path, out_dir, max_frames=80, every_s=120):
    """Pictures of each new thing on screen (a slide, a page, a folder, a menu, a shared document), for the whole recording,
    so a workflow someone shows can be followed step by step. Returns [{'t': seconds, 'file': name}], or [] with no video."""
    rec = Recording(path)
    try:
        if not rec.has_video:
            return []
        thumbs = rec.keyframe_thumbs()
        sparse = rec.duration > 60 and len(thumbs) < rec.duration / 15
        if sparse:
            thumbs = rec.sampled_thumbs(3.0)
        keep = still_parts(thumbs)
        threshold, kept = 0.006, []
        for _ in range(16):
            kept = pick_changes(thumbs, threshold, every_s, keep)
            if len(kept) <= max_frames:
                break
            threshold *= 1.35
        kept = kept[:max_frames]
        out = []
        if sparse:
            for t in kept:
                frame = rec.frame_at(t)
                if frame is not None:
                    name = f'screen_{file_time(t)}.jpg'
                    save(frame, out_dir, name)
                    out.append({'t': round(t, 2), 'file': name})
            return out
        wanted = set(round(t, 3) for t in kept)
        rec.stream.codec_context.skip_frame = 'NONKEY'
        rec.container.seek(0)
        for frame in rec.container.decode(rec.stream):
            if frame.time is None:
                continue
            t = frame.time - rec.offset
            if round(t, 3) in wanted:
                name = f'screen_{file_time(t)}.jpg'
                save(frame, out_dir, name)
                out.append({'t': round(t, 2), 'file': name})
                wanted.discard(round(t, 3))
                if not wanted:
                    break
        return out
    finally:
        rec.close()


def frames_at(path, times, out_dir, prefix='frame'):
    """A picture at each of the given times. Returns [{'t', 'file'}]."""
    rec = Recording(path)
    out = []
    try:
        if not rec.has_video:
            return []
        for t in times:
            t = min(max(0.0, t), max(0.0, rec.duration - 0.05))
            frame = rec.frame_at(t)
            if frame is not None:
                name = f'{prefix}_{file_time(t)}.jpg'
                save(frame, out_dir, name)
                out.append({'t': round(t, 2), 'file': name})
    finally:
        rec.close()
    return out


def main():
    parser = argparse.ArgumentParser(description='Pictures from a meeting recording.')
    parser.add_argument('command', choices=['frame', 'frames'])
    parser.add_argument('recording')
    parser.add_argument('--at', default='0')
    parser.add_argument('--from', dest='start', default='0')
    parser.add_argument('--to', dest='end', default='')
    parser.add_argument('--every', default='30')
    parser.add_argument('--out', default='')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    out_dir = args.out or (os.path.join('.copilot', 'tmp') if os.path.isdir('.copilot') else '.')
    if not os.path.isfile(args.recording):
        sys.exit(f'No such recording: {args.recording}')
    if args.command == 'frame':
        times = [parse_time(args.at)]
    else:
        start, every = parse_time(args.start), max(2.0, parse_time(args.every))
        end = parse_time(args.end) if args.end else start + every * 8
        count = min(40, int((end - start) / every) + 1)
        times = [start + i * every for i in range(count)]
    saved = frames_at(args.recording, times, out_dir)
    if not saved:
        sys.exit('This recording has no video.')
    for item in saved:
        print(f'{fmt_time(item["t"])}\t{os.path.abspath(os.path.join(out_dir, item["file"]))}')


if __name__ == '__main__':
    main()
