"""VAD stage: Silero v5 on onnxruntime directly (D10 — never the silero-vad pip
package, which drags in ~2.5 GB of torch to run a 2 MB model).

The model is stateful (LSTM state across frames), which is why it beats an
energy gate on breathy speech and background noise. Reset between utterances.
Verified I/O: input[batch, samples], state[2, 1, 128], sr scalar int64;
outputs prob[batch, 1] and the next state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import onnxruntime as ort

import config


_CONTEXT = 64  # Silero v5 prepends 64 samples of the previous chunk to each frame


class SileroVAD:
    def __init__(self, path: str = config.VAD_MODEL) -> None:
        self._sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self._sr = np.array(16000, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT), dtype=np.float32)

    def __call__(self, frame_f32: np.ndarray) -> float:
        # v5 contract: model input = [64-sample context][512-sample frame].
        # Feeding bare 512 windows makes it see discontinuous audio and never
        # fire — the context carries the LSTM's receptive field across frames.
        frame = frame_f32.reshape(1, -1).astype(np.float32)
        x = np.concatenate([self._context, frame], axis=1)
        out, self._state = self._sess.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        self._context = x[:, -_CONTEXT:]
        return float(out.reshape(-1)[0])


@dataclass
class Endpoint:
    """A completed user turn: float32 mono @16 kHz, preroll included."""
    audio: np.ndarray


class Endpointer:
    """Turn boundaries from per-frame speech probabilities. Emits an Endpoint
    once speech is followed by END_SILENCE_MS of quiet. Keeps a short preroll so
    the first word isn't clipped. `speaking` lets the session detect barge-in
    (onset while the assistant is talking)."""

    def __init__(self) -> None:
        self._vad = SileroVAD()
        self._frame_ms = config.FRAME_SAMPLES / 16000 * 1000
        self._preroll_max = config.PREROLL_FRAMES
        self.reset()

    @property
    def speaking(self) -> bool:
        return self._speaking

    def reset(self) -> None:
        self._vad.reset()
        self._speaking = False
        self._utter: list[np.ndarray] = []
        self._preroll: list[np.ndarray] = []
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._onset_ms = 0.0

    def process(self, frame_f32: np.ndarray, guarded: bool = False) -> Optional[Endpoint]:
        """`guarded` = the assistant's own voice is currently coming out of the
        speakers. Echo cancellation removes most of it, but "most" is not enough
        when the penalty is the assistant interrupting itself — so while it
        talks, onset needs a higher probability *sustained* over several frames.
        Real interruption still gets through; a leaked syllable doesn't."""
        prob = self._vad(frame_f32)
        threshold = config.BARGE_IN_THRESHOLD if guarded else config.VAD_THRESHOLD
        needed_ms = config.BARGE_IN_MIN_MS if guarded else 0.0
        speech = prob >= threshold

        if not self._speaking:
            self._preroll.append(frame_f32)
            if len(self._preroll) > self._preroll_max:
                self._preroll.pop(0)
            if not speech:
                self._onset_ms = 0.0        # must be *consecutive* to count
                return None
            self._onset_ms += self._frame_ms
            if self._onset_ms >= needed_ms:
                self._speaking = True
                self._utter = self._preroll + [frame_f32]
                self._preroll = []
                self._silence_ms = 0.0
                self._speech_ms = self._onset_ms
                self._onset_ms = 0.0
            return None

        # speaking
        self._utter.append(frame_f32)
        if speech:
            self._speech_ms += self._frame_ms
            self._silence_ms = 0.0
        else:
            self._silence_ms += self._frame_ms
            if self._silence_ms >= config.END_SILENCE_MS:
                audio = np.concatenate(self._utter) if self._utter else np.zeros(0, np.float32)
                had_speech = self._speech_ms >= config.MIN_SPEECH_MS
                self.reset()
                return Endpoint(audio=audio) if had_speech else None
        return None
