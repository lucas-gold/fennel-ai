"""TTS stage: Kokoro via mlx-audio, plus the greedy clause splitter (D5).

The mlx-audio call is isolated in `_synth` on purpose: its API moves between
releases, so if Kokoro breaks on load or synthesis that is the ONLY function to
change — don't refactor outward from it (CLAUDE.md).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

import numpy as np
from mlx_audio.tts.utils import load_model

import config

# A hard sentence end: . ! ? … optionally followed by a closing quote/bracket,
# then whitespace or end of buffer.
_SENT_END = re.compile(r"[.!?…]['\"”’)\]]?(?:\s|$)")

# Variation selector + zero-width joiner used to compose emoji.
_EMOJI_JOINERS = {0xFE0F, 0x200D}


def speakable(text: str) -> str:
    """Strip emoji and other symbol characters (Unicode category 'So') plus the
    emoji joiners, so Kokoro doesn't try to pronounce them."""
    kept = [ch for ch in text
            if unicodedata.category(ch) != "So" and ord(ch) not in _EMOJI_JOINERS]
    return re.sub(r"\s+", " ", "".join(kept)).strip()


class ClauseSplitter:
    """Cut the first speakable fragment aggressively (~18 chars) to minimise
    time-to-first-audio, then ramp to longer spans (~45, then ~90) for better
    prosody once audio is already playing. A hard sentence end always cuts
    regardless of length, so "Sure." goes out immediately (D5).

    The ramp is what keeps playback continuous: each clause has to be long
    enough to cover synthesising the next one, and jumping straight from 18 to
    90 chars left an audible gap about a second into every reply."""

    def __init__(self, first: int = config.CLAUSE_FIRST_CHARS,
                 second: int = config.CLAUSE_SECOND_CHARS,
                 rest: int = config.CLAUSE_REST_CHARS) -> None:
        self._steps = [first, second]
        self._rest = rest
        self._buf = ""
        self._n = 0            # clauses emitted so far

    def _threshold(self) -> int:
        return self._steps[self._n] if self._n < len(self._steps) else self._rest

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return any clauses that are now complete."""
        self._buf += text
        out: list[str] = []
        while (clause := self._try_cut()) is not None:
            out.append(clause)
            self._n += 1
        return out

    def _try_cut(self) -> Optional[str]:
        # 1) hard sentence end anywhere wins
        if m := _SENT_END.search(self._buf):
            clause, self._buf = self._buf[:m.end()].strip(), self._buf[m.end():]
            return clause or None
        # 2) otherwise cut at a word boundary once past the threshold
        thr = self._threshold()
        if len(self._buf) >= thr:
            sp = self._buf.find(" ", thr)
            if sp != -1:
                clause, self._buf = self._buf[:sp].strip(), self._buf[sp + 1:]
                return clause or None
        return None

    def flush(self) -> str:
        """Emit whatever is left (end of reply)."""
        clause, self._buf = self._buf.strip(), ""
        if clause:
            self._n += 1
        return clause


class KokoroTTS:
    SAMPLE_RATE = 24000

    def __init__(self, model_id: str = config.TTS_MODEL,
                 voice: str = config.TTS_VOICE) -> None:
        self._model = load_model(model_id)
        self._voice = voice

    def _synth(self, text: str) -> np.ndarray:
        """Isolated mlx-audio call. Returns float32 mono @24 kHz in [-1, 1]."""
        segs = self._model.generate(text=text, voice=self._voice, speed=config.TTS_SPEED)
        parts = [np.array(s.audio, copy=False).astype(np.float32).reshape(-1)
                 for s in segs]
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def synth_pcm(self, text: str) -> np.ndarray:
        """int16 PCM @24 kHz for one clause (emoji stripped). Empty if nothing
        speakable remains."""
        text = speakable(text)
        if not text:
            return np.zeros(0, dtype=np.int16)
        a = self._synth(text)
        return (np.clip(a, -1.0, 1.0) * 32767.0).astype(np.int16)

    def warmup(self) -> None:
        """Load the G2P pipeline + Metal kernels so the first real clause is fast."""
        self.synth_pcm("Hello there.")
