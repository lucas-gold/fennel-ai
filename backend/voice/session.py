"""Orchestrator: mic frames → endpoint → STT → LLM → clause splitter → TTS,
with the epoch counter and barge-in (reference D3).

Every stage captures the epoch it started under and checks `_live()` before any
side effect, because MLX generation is a blocking generator that can't be
cancelled cleanly mid-flight. Barge-in bumps the epoch (dropping all in-flight
output within ~100 ms) and records only what was actually spoken, with a `—`
marker, so the model's next turn is grounded in what the user really heard.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable, Optional

import numpy as np

import config
import protocol as P
from voice.llm import LLM
from voice.stt import WhisperSTT
from voice.tts import ClauseSplitter, KokoroTTS
from voice.vad import Endpointer

Control = Callable[[str], Awaitable[None]]   # send a JSON control string
Audio = Callable[[bytes], Awaitable[None]]   # send a binary audio frame


class Session:
    def __init__(self, send_control: Control, send_audio: Audio,
                 stt: WhisperSTT, llm: LLM, tts: KokoroTTS) -> None:
        self._send_control = send_control
        self._send_audio = send_audio

        # Heavy model weights are shared across connections; only the KV cache
        # and VAD state are per-conversation.
        self._stt = stt
        self._llm = llm
        self._llm.reset()
        self._tts = tts
        self._endpointer = Endpointer()

        self._messages: list[dict] = [
            {"role": "system", "content": config.LLM_SYSTEM}
        ]
        self._epoch = 0
        self._turn_no = 0
        self._turn_task: Optional[asyncio.Task] = None
        self._stop = threading.Event()
        self._assistant_active = False
        self._rx = 0  # mic frames received (debug)

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def warmup(self) -> None:
        await asyncio.to_thread(self._stt.warmup)

    async def close(self) -> None:
        await self._supersede()

    def _live(self, epoch: int) -> bool:
        return epoch == self._epoch

    # ── inputs ─────────────────────────────────────────────────────────────

    async def feed_frame(self, frame_f32: np.ndarray) -> None:
        """One 16 kHz mic frame. Detects barge-in and turn endpoints."""
        was_speaking = self._endpointer.speaking
        endpoint = self._endpointer.process(frame_f32)
        onset = self._endpointer.speaking and not was_speaking

        self._rx += 1
        if self._rx % 50 == 0:
            peak = float(np.abs(frame_f32).max())
            print(f"[vad] rx={self._rx} frames peak={peak:.3f} "
                  f"speaking={self._endpointer.speaking}", flush=True)
        if onset:
            print("[vad] speech onset", flush=True)

        if onset and self._assistant_active:
            await self._barge_in()
        if endpoint is not None:
            print(f"[vad] endpoint: {len(endpoint.audio)/16000:.2f}s", flush=True)
            await self._start_turn(audio=endpoint.audio)

    async def feed_text(self, text: str, speak: bool = False) -> None:
        """Typed input: skip VAD/STT, go straight to the LLM. Speaks the reply
        only if the user enabled it (voice turns always speak)."""
        if text.strip():
            await self._start_turn(text=text, speak=speak)

    # ── turn control ───────────────────────────────────────────────────────

    async def _barge_in(self) -> None:
        self._epoch += 1        # invalidate every in-flight stage (D3)
        self._stop.set()        # unblock the LLM worker thread
        self._assistant_active = False
        await self._send_control(P.encode("state", value="idle"))
        # The interrupted turn commits its partial reply in _run_turn's finally.

    async def _supersede(self) -> None:
        """Ensure any in-flight turn has fully wound down (and committed)."""
        if self._turn_task and not self._turn_task.done():
            self._epoch += 1
            self._stop.set()
            await self._turn_task

    async def _start_turn(self, audio: Optional[np.ndarray] = None,
                          text: Optional[str] = None, speak: bool = True) -> None:
        await self._supersede()
        self._epoch += 1
        self._stop = threading.Event()
        epoch = self._epoch
        self._turn_task = asyncio.create_task(self._run_turn(epoch, audio, text, speak))

    async def _run_turn(self, epoch: int, audio: Optional[np.ndarray],
                        text: Optional[str], speak: bool = True) -> None:
        from_voice = text is None
        do_speak = from_voice or speak       # voice turns always speak
        if from_voice:
            await self._send_control(P.encode("state", value="thinking"))
            text = await asyncio.to_thread(self._stt.transcribe, audio)
            print(f"[stt] -> {text!r}", flush=True)
        if not self._live(epoch) or not text.strip():
            return
        if from_voice:  # let the UI show what was heard
            await self._send_control(P.encode("stt", text=text))
        self._messages.append({"role": "user", "content": text})

        self._turn_no += 1
        turn = self._turn_no
        splitter = ClauseSplitter()
        reply = ""
        spoken = ""
        seq = 0

        async def speak_clause(clause: str) -> None:
            nonlocal seq, spoken
            if not clause or not self._live(epoch):
                return
            pcm = await asyncio.to_thread(self._tts.synth_pcm, clause)
            if pcm.size == 0 or not self._live(epoch):
                return
            await self._send_audio(P.pack_audio(turn, seq, pcm))
            seq += 1
            spoken += clause + " "

        self._assistant_active = do_speak
        if do_speak:
            await self._send_control(P.encode("state", value="speaking"))
        try:
            async for chunk in self._llm.astream(self._messages, stop=self._stop):
                if not self._live(epoch):
                    break
                reply += chunk
                await self._send_control(P.encode("token", turn=turn, text=chunk))
                if do_speak:
                    for clause in splitter.feed(chunk):
                        await speak_clause(clause)
                        if not self._live(epoch):
                            break
            else:
                if do_speak:
                    await speak_clause(splitter.flush())
        finally:
            self._assistant_active = False
            if self._live(epoch):
                self._messages.append({"role": "assistant", "content": reply})
                await self._send_control(P.encode("turn_end", turn=turn))
                if do_speak:
                    await self._send_control(P.encode("state", value="idle"))
            else:
                # barge-in: history records only what was actually spoken (D3)
                self._messages.append(
                    {"role": "assistant", "content": (spoken.strip() + " —").strip()}
                )
