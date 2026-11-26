"""Tests for live dictation's streaming policy (harness/stream_policy.py), with no speech model: small made-up decodes,
and a real dictation's decodes recorded in tools/dictation_fixture.json, replayed in order.

    python -X utf8 tools/test_dictation.py"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness.stream_policy import RATE, Region, Stream, accept_polish, seam  # noqa: E402


def W(*items):
    """W((0.0, 0.4, ' Hello'), ...) as word lists."""
    return [tuple(x) for x in items]


def wer(ref, hyp):
    n = lambda s: re.sub(r"[^\w' ]+", " ", s.lower()).split()  # noqa: E731
    r, h = n(ref), n(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return d[len(h)] / max(1, len(r))


class Policy(unittest.TestCase):
    def test_two_agreeing_decodes_settle_words(self):
        s = Stream()
        s.insert(W((0.1, 0.4, " Hello"), (0.5, 0.8, " world")), 1.0)
        self.assertEqual((s.settled_text(), s.tentative_text()), ("", "Hello world"))
        s.insert(W((0.1, 0.4, " Hello"), (0.5, 0.8, " word"), (0.9, 1.2, " again")), 1.3)
        self.assertEqual((s.settled_text(), s.tentative_text()), ("Hello", "word again"))

    def test_case_and_punctuation_do_not_block_agreement(self):
        s = Stream()
        s.insert(W((0.1, 0.5, " this"), (0.6, 1.0, " afternoon")), 1.1)
        s.insert(W((0.1, 0.5, " This"), (0.6, 1.0, " afternoon."), (1.2, 1.5, " First")), 1.6)
        self.assertEqual(s.settled_text(), "This afternoon.")      # the newer decode's spelling is kept
        self.assertEqual(s.tentative_text(), "First")

    def test_settled_words_heard_again_are_not_repeated(self):
        s = Stream()
        for _ in range(2):
            s.insert(W((0.1, 0.4, " one"), (0.5, 0.8, " two")), 1.0)
        s.insert(W((0.12, 0.42, " one"), (0.52, 0.82, " two"), (0.9, 1.2, " three")), 1.3)
        s.insert(W((0.12, 0.42, " one"), (0.52, 0.82, " two"), (0.9, 1.2, " three")), 1.6)
        self.assertEqual(s.settled_text(), "one two three")

    def test_long_audio_is_cut_at_a_sentence_end(self):
        s = Stream()
        words = W((0.2, 1.0, " First"), (1.1, 2.0, " part."), (2.2, 3.5, " Second"), (3.6, 5.0, " part"), (5.1, 6.3, " goes"))
        s.insert(words, 6.5)
        s.insert(words, 6.8)
        self.assertEqual(len(s.regions), 1)
        self.assertEqual(s.regions[0].live, "First part.")
        self.assertTrue(2.0 <= s.offset <= 2.2)                    # the next decode starts between the sentences
        self.assertEqual(s.settled_text(), "First part. Second part goes")

    def test_silence_is_dropped(self):
        s = Stream()
        s.insert([], 3.0, quiet_since=0.0)
        self.assertAlmostEqual(s.offset, 2.5)

    def test_a_pause_ends_a_stretch_and_polish_waits_for_it(self):
        s = Stream()
        a = W((0.2, 0.6, " Second,"), (0.8, 1.2, " check"))
        s.insert(a, 1.3)
        s.insert(a, 1.6)                                           # settled; still talking (no quiet)
        long = a + W((1.3, 2.0, " the"), (2.1, 3.0, " logs"), (3.1, 4.0, " today"), (4.4, 6.0, " please"), (6.1, 6.5, " now"))
        s.insert(long, 6.6)
        s.insert(long, 6.9)                                        # cut at a gap: not a pause, so not ready to polish
        self.assertGreaterEqual(len(s.regions), 1)
        self.assertIsNone(s.ready_group())
        s.insert(long, 7.8, quiet_since=6.6)                       # quiet for 1.2 s: a pause
        self.assertTrue(s.regions[-1].pause)
        g = s.ready_group()
        self.assertEqual(len(s.regions), 1)                        # the stretch up to the pause is one region now
        self.assertEqual(g.live, "Second, check the logs today please now")
        self.assertAlmostEqual(g.end, 6.8, places=5)

    def test_finish_settles_everything(self):
        s = Stream()
        s.insert(W((0.1, 0.4, " Fix"), (0.5, 0.9, " the")), 1.0)
        s.finish(W((0.1, 0.4, " Fix"), (0.5, 0.9, " the"), (1.0, 1.5, " test.")), 1.6)
        self.assertEqual((s.settled_text(), s.tentative_text()), ("Fix the test.", ""))
        self.assertTrue(s.regions[-1].pause)
        self.assertIsNotNone(s.ready_group(final=True))

    def test_polished_text_replaces_live_text_with_mended_seams(self):
        s = Stream()
        s.regions = [Region(0, 2, W((0, 1, " Second,"))), Region(2, 4, W((2, 3, " check"), (3, 4, " logs")))]
        s.regions[0].polished, s.regions[1].polished = "Second.", "Check the logs."
        self.assertEqual(s.settled_text(), "Second, check the logs.")

    def test_seam_keeps_real_sentence_ends(self):
        self.assertEqual(seam("Speaking.", "speaking.", "Then go.", "then go"), ("Speaking.", "Then go."))
        self.assertEqual(seam("Ask.", "ask", "I said.", "I said"), ("Ask", "I said."))

    def test_polish_that_looks_like_other_speech_is_refused(self):
        self.assertTrue(accept_polish("run the test sweet again", "Run the test suite again."))
        self.assertFalse(accept_polish("fix it", "Fix it. Thank you for watching, and please subscribe to the channel."))
        self.assertFalse(accept_polish("fix the failing test now", ""))
        self.assertTrue(accept_polish("", ""))


class Recorded(unittest.TestCase):
    """A real dictation's decodes, replayed: the text must build up live and end as the script."""

    def test_replay(self):
        fx = json.loads((ROOT / "tools" / "dictation_fixture.json").read_text(encoding="utf-8"))
        s = Stream()
        prev, first_seen, settled_growth = "", None, True
        for i, d in enumerate(fx["decodes"]):
            off = d["start"] / RATE
            words = [(off + a, off + b, w) for a, b, w in d["words"]]
            if i == len(fx["decodes"]) - 1:
                s.finish(words, d["end"] / RATE)
            else:
                s.insert(words, d["end"] / RATE, d["quiet"])
                self.assertLessEqual(d["end"] / RATE - s.offset, Stream.FORCE_S + 3, "the decoded audio must stay short")
            now = s.settled_text()
            if first_seen is None and (now or s.tentative_text()):
                first_seen = d["end"] / RATE
            settled_growth &= now.startswith(prev)
            prev = now
        self.assertTrue(settled_growth, "settled words never change in the live pass")
        self.assertIsNotNone(first_seen)
        self.assertLess(first_seen, 3.0, "words show within the first seconds of speech")
        self.assertLessEqual(wer(fx["script"], s.settled_text()), 0.05, s.settled_text())
        self.assertEqual(s.tentative_text(), "")
        self.assertGreaterEqual(len(s.regions), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
