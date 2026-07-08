"""Kokoro speech synthesis via mlx-audio, plus the clause splitter.

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


# Markdown the model emits for the chat pane but which must never be spoken:
# Kokoro reads "*" aloud as "asterisk".
_MD_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(.+?)\1", re.S)
_MD_CODE = re.compile(r"`{1,3}([^`]*)`{1,3}", re.S)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_LEADING = re.compile(r"^\s{0,3}(?:[#>]{1,6}\s*|[-*+]\s+|\d+[.)]\s+)", re.M)
_MD_LEFTOVER = re.compile(r"[*_`~]{1,3}")


def speakable(text: str) -> str:
    """What Kokoro should actually say: no emoji, no markdown.

    The model writes for the chat pane, so replies arrive with **bold**, bullet
    dashes and backticks in them. Kokoro pronounces those literally — "asterisk
    asterisk bold" — which is the kind of detail that makes a voice sound broken.
    Emphasis is unwrapped rather than deleted so the words survive.
    """
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_EMPHASIS.sub(r"\2", text)
    text = _MD_LEADING.sub("", text)
    text = _MD_LEFTOVER.sub("", text)          # unmatched pairs, e.g. a lone *
    kept = [ch for ch in text
            if unicodedata.category(ch) != "So" and ord(ch) not in _EMOJI_JOINERS]
    return re.sub(r"\s+", " ", "".join(kept)).strip()


class ClauseSplitter:
    """Cut the first fragment short (~18 chars) so audio starts sooner, then
    ramp to ~45 and ~90 for better prosody once it is playing. A hard sentence
    end always cuts, so "Sure." goes out immediately.

    The ramp keeps playback continuous: each clause has to be long enough to
    cover synthesising the next, so widening the steps opens an audible gap."""

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
