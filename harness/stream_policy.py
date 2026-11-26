"""Live dictation's streaming policy (standard library only, so it can be tested without a speech model).

Whisper hears the same stretch of audio again and again while it grows. A word is settled once two decodes in a row agree on
it (LocalAgreement-2, as in whisper_streaming): those words never change again in the live pass, the rest of the latest decode
is shown as "may still change". Once the settled words cover a few seconds, the audio before them is cut off (at a sentence
end or a pause when there is one) so every decode stays short; each cut-off stretch is a Region, which the accurate model can
later transcribe again ("polish") with the whole stretch heard at once.

Words are (start_s, end_s, text) with times in seconds from the start of the recording; text keeps Whisper's leading space."""
import re

RATE = 16000

_NORM = re.compile(r"[^\w']+")


def norm(text):
    """A word as compared between decodes: case and punctuation do not matter ("afternoon" then "afternoon.")."""
    return _NORM.sub("", text.lower())


def join(words):
    return "".join(w[2] for w in words).strip()


class Region:
    """A settled stretch of the recording: its live text, and the accurate model's text once polished."""

    def __init__(self, start, end, words):
        self.start, self.end, self.words = start, end, list(words)
        self.pause = False          # it ends where the speaker paused (so the accurate model may start a stretch there)
        self.live = join(self.words)
        self.polished = None

    def text(self):
        return self.polished if self.polished is not None else self.live


def seam(a, a_live, b, b_live):
    """Join two polished stretches. Each was transcribed alone, so the accurate model ends the first with a full stop and
    starts the next with a capital even where the speaker only paused mid-sentence; the live pass heard across the cut, so
    its punctuation and case decide there."""
    if a and b and a_live and a.endswith(".") and not a.endswith("...") and not a_live.rstrip().endswith((".", "?", "!")):
        a = a[:-1] + (a_live.rstrip()[-1] if a_live.rstrip()[-1] in ",;:" else "")
        bw, lw = b.split(" ", 1), b_live.split(" ", 1)
        if bw[0] and lw[0] and lw[0][:1].islower() and bw[0][:1].isupper() and norm(bw[0]) == norm(lw[0]) and bw[0] != "I":
            b = " ".join([bw[0][:1].lower() + bw[0][1:]] + bw[1:])
    return a, b


def accept_polish(live, polished):
    """Use the accurate pass only when it looks like the same speech: not empty when words were heard, not far longer (Whisper
    can invent a sentence on a breath or a cut-off word)."""
    a, b = live.split(), polished.split()
    if not b:
        return not a
    if not a:
        return len(b) <= 3
    if len(b) > 1.6 * len(a) + 3 or len(b) < 0.5 * len(a) - 2:
        return False
    sa, sb = {norm(w) for w in a}, {norm(w) for w in b}
    return len(sa & sb) >= 0.4 * min(len(sa), len(sb))


class Stream:
    """One dictation's live text. Feed it each decode of the audio from `offset` (seconds) onward with insert()."""

    TRIM_S = 6.0          # cut the audio once settled words cover this much of it ...
    FORCE_S = 12.0        # ... even without a sentence end or a gap between words once it is this long
    SILENCE_S = 2.0       # nothing heard for this long: drop the silence (keep the last half second)
    PAUSE_S = 0.7         # the audio has been quiet this long after the settled words: a pause, which ends a stretch

    def __init__(self):
        self.offset = 0.0           # where the next decode starts (s)
        self.regions = []           # settled and cut off
        self.settled = []           # settled words not yet in a region
        self.tentative = []         # the latest decode's words after the settled ones
        self.region_start = 0.0
        self.last_time = 0.0        # end of the last settled word

    # ---- the live pass
    def _fresh(self, words):
        """The decode's words after the settled ones (Whisper hears the not yet cut-off settled words again)."""
        new = [w for w in words if w[0] > self.last_time - 0.1 and w[2].strip()]
        if new and self.settled and abs(new[0][0] - self.last_time) < 1:
            for n in range(min(5, len(self.settled), len(new)), 0, -1):
                if [norm(w[2]) for w in self.settled[-n:]] == [norm(w[2]) for w in new[:n]]:
                    return new[n:]
        return new

    def insert(self, words, audio_end, quiet_since=None):
        """words: the latest decode (absolute times) of the audio [offset, audio_end]; quiet_since: when the audio went quiet,
        if it is quiet at audio_end (from its loudness, not from Whisper). Returns True if the text changed."""
        before = (len(self.settled), len(self.regions), [w[2] for w in self.tentative])
        new = self._fresh(words)
        agreed = 0
        while agreed < len(new) and agreed < len(self.tentative) and norm(new[agreed][2]) == norm(self.tentative[agreed][2]):
            agreed += 1
        if agreed:
            self.settled.extend(new[:agreed])
            self.last_time = new[agreed - 1][1]
        self.tentative = new[agreed:]
        self._trim(audio_end, quiet_since)
        return before != (len(self.settled), len(self.regions), [w[2] for w in self.tentative])

    def _trim(self, audio_end, quiet_since):
        length = audio_end - self.offset
        paused = quiet_since is not None and audio_end - quiet_since >= self.PAUSE_S
        if not self.settled:
            if not self.tentative and length > self.SILENCE_S:
                self.offset = self.region_start = max(self.offset, audio_end - 0.5)
            return
        if paused and not self.tentative:                  # the speaker paused and every word is settled
            self._close(len(self.settled), min(audio_end, max(quiet_since + 0.2, self.last_time + 0.1)), pause=True)
            return
        if length < self.TRIM_S:
            return
        after = self.tentative[0][0] if self.tentative else None
        cut = None
        for i in range(len(self.settled) - 1, -1, -1):     # latest sentence end, else latest gap, among the settled words
            w = self.settled[i]
            nxt = self.settled[i + 1][0] if i + 1 < len(self.settled) else after
            if w[2].rstrip().endswith((".", "?", "!")) or (nxt is not None and nxt - w[1] >= 0.25):
                cut = i
                break
        if cut is None:
            if length < self.FORCE_S:
                return
            cut = len(self.settled) - 1
        nxt = self.settled[cut + 1][0] if cut + 1 < len(self.settled) else after
        end = self.settled[cut][1] if nxt is None else (self.settled[cut][1] + nxt) / 2
        if nxt is None:
            end = min(audio_end, end + 0.15)
        self._close(cut + 1, end)

    def _close(self, n, end, pause=False):
        words, self.settled = self.settled[:n], self.settled[n:]
        if words:
            self.regions.append(Region(self.region_start, end, words))
            self.regions[-1].pause = pause
        elif pause and self.regions:
            self.regions[-1].pause = True
        self.region_start = self.offset = end

    def finish(self, words, audio_end):
        """The last decode after the speaker stopped: everything in it is settled."""
        self.settled.extend(self._fresh(words))
        self.tentative = []
        if self.settled:
            self.last_time = self.settled[-1][1]
        self._close(len(self.settled), audio_end, pause=True)

    def give_up_tentative(self):
        """No last decode is possible (the speech engine stopped): keep the unsettled words as heard."""
        self.settled.extend(self.tentative)
        self.tentative = []

    # ---- the accurate pass
    def ready_group(self, final=False):
        """The next stretch to polish: the unpolished regions up to a pause (cut there, the accurate model hears whole
        phrases), merged into one region; or None. final: the dictation has ended, so the rest counts too."""
        todo = []
        for r in self.regions:
            if r.polished is not None:
                continue
            todo.append(r)
            if r.pause:
                break
        if not todo or not (todo[-1].pause or final):
            return None
        if len(todo) == 1:
            return todo[0]
        merged = Region(todo[0].start, todo[-1].end, [w for r in todo for w in r.words])
        merged.pause = todo[-1].pause
        i = self.regions.index(todo[0])
        self.regions[i:i + len(todo)] = [merged]
        return merged

    def unpolished(self):
        return [r for r in self.regions if r.polished is None]

    # ---- the text
    def settled_text(self):
        texts = [r.text() for r in self.regions] + [join(self.settled)]
        lives = [r.live for r in self.regions] + [texts[-1]]
        for i in range(len(texts) - 1):
            texts[i], texts[i + 1] = seam(texts[i], lives[i], texts[i + 1], lives[i + 1])
        return " ".join(t for t in texts if t)

    def tentative_text(self):
        return join(self.tentative)
