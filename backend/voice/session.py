"""Orchestrator: mic frames → endpoint → STT → LLM → clause splitter → TTS,
with the epoch counter and barge-in (reference D3), plus the Stage 3 tool loop.

Every stage captures the epoch it started under and checks `_live()` before any
side effect, because MLX generation is a blocking generator that can't be
cancelled cleanly mid-flight. Barge-in bumps the epoch (dropping all in-flight
output within ~100 ms) and records only what was actually spoken, with a `—`
marker, so the model's next turn is grounded in what the user really heard.

Tool calls (D-HOME) are split out of the stream before TTS ever sees them, fired
the moment they complete so the home card appears while the model is still
talking, and their results are appended as conversation messages — which keeps
them last in the prompt, so the cached prefix survives (D4).
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Awaitable, Callable, Optional
from uuid import uuid4

import numpy as np

import config
import protocol as P
from voice.llm import LLM
from voice.stt import WhisperSTT
from voice.tools import ToolStream, normalize, stamp, system_prompt
from voice.tts import ClauseSplitter, KokoroTTS
from voice.vad import Endpointer

Control = Callable[[str], Awaitable[None]]   # send a JSON control string
Audio = Callable[[bytes], Awaitable[None]]   # send a binary audio frame


class Session:
    def __init__(self, send_control: Control, send_audio: Audio,
                 stt: WhisperSTT, llm: LLM, tts: KokoroTTS,
                 system: Optional[str] = None) -> None:
        self._send_control = send_control
        self._send_audio = send_audio

        # Heavy model weights are shared across connections; only the KV cache
        # and VAD state are per-conversation.
        self._stt = stt
        self._llm = llm
        self._llm.reset()
        self._tts = tts
        self._endpointer = Endpointer()

        # Must be the exact string the LLM was primed with, or the cached
        # prefix misses and the first turn pays for the whole prefill again.
        self._messages: list[dict] = [
            {"role": "system", "content": system or system_prompt(config.LLM_SYSTEM)}
        ]
        self._epoch = 0
        self._turn_no = 0
        self._turn_task: Optional[asyncio.Task] = None
        self._stop = threading.Event()
        self._assistant_active = False
        self._rx = 0  # mic frames received (debug)

        # Tool calls awaiting the app's real-world result, keyed by call id.
        self._pending: dict[str, asyncio.Future] = {}
        # Facts remembered this session; Stage 4 makes them durable (SQLite).
        self._facts: dict[str, str] = {}

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

    def feed_tool_result(self, msg: dict) -> None:
        """The app reports how the real side effect went (EventKit write, etc.)."""
        fut = self._pending.get(str(msg.get("id", "")))
        if fut is not None and not fut.done():
            fut.set_result(msg)

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

    # ── tools ──────────────────────────────────────────────────────────────

    async def _run_tool(self, call: dict) -> dict:
        """Normalize, hand to the app, and wait briefly for the real outcome so
        the model's spoken confirmation isn't a lie."""
        name, args = call["name"], call["args"]
        card, result = normalize(name, args)
        print(f"[tool] {name} {card or args}", flush=True)
        if not result.get("ok"):
            return {"name": name, **result}

        if name == "set_fact":
            self._facts[card["key"]] = card["value"]

        call_id = uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        await self._send_control(P.encode("tool", id=call_id, name=name, args=card))
        try:
            app = await asyncio.wait_for(fut, config.TOOL_APP_TIMEOUT_S)
        except asyncio.TimeoutError:
            # The card is on screen either way; assume the write is in flight
            # rather than making the model apologise for a slow app.
            app = None
            print(f"[tool] {name}: app did not report back in time", flush=True)
        finally:
            self._pending.pop(call_id, None)

        if app is not None and not app.get("ok", True):
            return {"name": name, "ok": False,
                    "error": app.get("error") or "the app could not complete it"}
        return {"name": name, **result}

    # ── the turn ───────────────────────────────────────────────────────────

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
        self._messages.append({"role": "user", "content": stamp(text)})

        self._turn_no += 1
        turn = self._turn_no
        seq = 0
        spoken = ""
        gap = False   # a later pass resumes mid-sentence without one otherwise

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

        async def llm_pass() -> tuple[str, list[dict], str]:
            """One generation. Returns the raw text (for the history, so the
            cached prefix matches token-for-token), any tool calls, and the
            prose actually delivered — all streamed and spoken along the way."""
            nonlocal gap
            # Fresh splitter per pass: a post-tool confirmation is the moment
            # the user is waiting on, so it gets the aggressive first cut too.
            splitter = ClauseSplitter()
            ts = ToolStream()
            calls: list[dict] = []
            said = ""

            def space(prose: str) -> str:
                nonlocal gap
                if gap and prose:
                    gap = False
                    return " " + prose
                return prose

            async for chunk in self._llm.astream(self._messages, stop=self._stop):
                if not self._live(epoch):
                    break
                prose, new_calls = ts.feed(chunk)
                calls += new_calls
                for c in new_calls:            # fire while the model still talks
                    c["result"] = await self._run_tool(c)
                if not prose:
                    continue
                prose = space(prose)
                said += prose
                await self._send_control(P.encode("token", turn=turn, text=prose))
                if do_speak:
                    for clause in splitter.feed(prose):
                        await speak_clause(clause)
                        if not self._live(epoch):
                            break
            if self._live(epoch):
                prose, new_calls = ts.flush()
                for c in new_calls:
                    c["result"] = await self._run_tool(c)
                calls += new_calls
                if prose:
                    prose = space(prose)
                    said += prose
                    await self._send_control(P.encode("token", turn=turn, text=prose))
                    splitter.feed(prose)
                if do_speak:
                    await speak_clause(splitter.flush())
            return ts.raw, calls, said

        self._assistant_active = do_speak
        if do_speak:
            await self._send_control(P.encode("state", value="speaking"))
        try:
            for round_ in range(config.LLM_TOOL_ROUNDS + 1):
                raw, calls, said = await llm_pass()
                if not self._live(epoch):
                    break
                # Store the generation verbatim: re-rendering structured
                # tool_calls would re-tokenize differently and cost a re-prefill.
                self._messages.append({"role": "assistant", "content": raw})
                if not calls or round_ == config.LLM_TOOL_ROUNDS:
                    break
                results = [c.get("result", {"name": c["name"], "ok": True}) for c in calls]
                self._messages.append({
                    "role": "tool",
                    "content": json.dumps(results[0] if len(results) == 1 else results),
                })
                gap = bool(said) and not said.endswith((" ", "\n"))
                # Qwen puts its tool call last, so any prose in that pass was
                # already a confirmation. Saying it again is the single most
                # annoying failure mode in a voice UI — and skipping the extra
                # round also removes a whole generation from the critical path.
                # A failed tool still gets a round, so the model can own it.
                if len(said.strip()) >= 15 and all(r.get("ok") for r in results):
                    break
        finally:
            self._assistant_active = False
            if self._live(epoch):
                await self._send_control(P.encode("turn_end", turn=turn))
                if do_speak:
                    await self._send_control(P.encode("state", value="idle"))
            else:
                # barge-in: history records only what was actually spoken (D3)
                self._messages.append(
                    {"role": "assistant", "content": (spoken.strip() + " —").strip()}
                )
