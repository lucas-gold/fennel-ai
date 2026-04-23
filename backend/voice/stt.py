"""STT stage: mlx-whisper (large-v3-turbo, D9).

Takes a float32 mono 16 kHz array — never a file path, so `mlx_whisper` never
shells out to ffmpeg. The Swift app already captures at 16 kHz, so STT gets the
right array directly. `mlx_whisper` caches the loaded model internally, so the
first call warms it and later calls reuse it.
"""
from __future__ import annotations

import numpy as np
import mlx_whisper

import config


class WhisperSTT:
    SAMPLE_RATE = 16000

    def __init__(self, model_id: str = config.STT_MODEL) -> None:
        self._model_id = model_id

    def transcribe(self, audio_16k_f32: np.ndarray) -> str:
        result = mlx_whisper.transcribe(
            audio_16k_f32, path_or_hf_repo=self._model_id
        )
        return result["text"].strip()

    def warmup(self) -> None:
        """Prime the model with a short silence so the first real turn is fast."""
        self.transcribe(np.zeros(self.SAMPLE_RATE // 2, dtype=np.float32))
