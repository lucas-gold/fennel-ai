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
from voice.memory import Memory
from voice.store import Store
from voice.stt import WhisperSTT
from voice.tools import ToolStream, normalize, system_prompt
from voice.tts import ClauseSplitter, KokoroTTS
from voice.vad import Endpointer

Control = Callable[[str], Awaitable[None]]   # send a JSON control string
Audio = Callable[[bytes], Awaitable[None]]   # send a binary audio frame


class Session:
    def __init__(self, send_control: Control, send_audio: Audio,
                 stt: WhisperSTT, llm: LLM, tts: KokoroTTS,
                 store: Store, memory: Memory,
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
        self._store = store
        self._memory = memory

        # Must be the exact string the LLM was primed with, or the cached
        # prefix misses and the first turn pays for the whole prefill again.
        self._system = system or system_prompt(config.LLM_SYSTEM)
        self._session_id: int = 0
        self._messages: list[dict] = [{"role": "system", "content": self._system}]
        self._summary_task: Optional[asyncio.Task] = None
        self._epoch = 0
        self._turn_no = 0
        self._turn_task: Optional[asyncio.Task] = None
        self._stop = threading.Event()
        self._assistant_active = False
        self._rx = 0  # mic frames received (debug)

        # Tool calls awaiting the app's real-world result, keyed by call id.
        self._pending: dict[str, asyncio.Future] = {}

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def warmup(self) -> None:
        await asyncio.to_thread(self._stt.warmup)

    async def close(self) -> None:
        await self._supersede()

    # ── chat sessions ──────────────────────────────────────────────────────

    async def open_session(self, session_id: Optional[int] = None,
                           create: bool = False) -> None:
        """Resume a conversation (or start one) and send the app its history."""
        await self._supersede()
        if create or session_id is None:
            session_id = (None if create else self._store.latest_session()) \
                         or self._store.new_session()
        self._session_id = int(session_id)
        self._rebuild()
        rows = self._store.messages(self._session_id)
        await self._send_control(P.encode(
            "session_opened", id=self._session_id,
            messages=[{"role": r["role"], "text": r["content"]} for r in rows]))
        await self.send_sessions()

    async def apply_system(self, system: str) -> None:
        """Adopt a new system prefix (the daily briefing arriving mid-session).

        Waits for any in-flight turn rather than superseding it: this fires once
        a day, off the user's initiative, and cutting a reply off mid-sentence to
        install a news update would be a strange thing to do to someone."""
        if system == self._system:
            return
        if self._turn_task and not self._turn_task.done():
            await self._turn_task
        self._system = system
        self._rebuild()

    async def send_sessions(self) -> None:
        await self._send_control(P.encode("sessions",
                                          items=self._store.list_sessions(),
                                          current=self._session_id))

    async def delete_session(self, session_id: int) -> None:
        self._store.delete_session(int(session_id))
        if int(session_id) == self._session_id:
            await self.open_session()          # fall back to the next newest
        else:
            await self.send_sessions()

    def _rebuild(self) -> None:
        """Rebuild the prompt from durable state: primed system prefix, then the
        facts/summary block, then the verbatim window. Costs a re-prefill, so it
        happens on session switches and chunked evictions only — never per turn."""
        msgs = [{"role": "system", "content": self._system}]
        if ctx := self._memory.context_message(self._session_id):
            msgs.append(ctx)
        msgs += self._memory.window(self._session_id)
        self._messages = msgs
        self._llm.reset()

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
            self._store.set_fact(card["key"], card["value"])

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
        # Read-style tools (agenda) answer from the app's side of the wire —
        # its `data` is the actual result the model has to speak from.
        if app is not None and isinstance(app.get("data"), dict):
            result = {**result, **app["data"]}
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
        # The store keeps what the user actually said; the prompt gets the
        # volatile preamble (clock + recall) glued on front — last position in
        # the prompt, so the cached prefix behind it survives (D4).
        self._store.add_message(self._session_id, "user", text)
        self._messages.append({
            "role": "user",
            "content": self._memory.preamble(self._session_id, text) + text,
        })

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
        visible = ""
        try:
            for round_ in range(config.LLM_TOOL_ROUNDS + 1):
                raw, calls, said = await llm_pass()
                visible += said
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
                self._store.add_message(self._session_id, "assistant", visible.strip())
                await self._send_control(P.encode("turn_end", turn=turn))
                if do_speak:
                    await self._send_control(P.encode("state", value="idle"))
                await self._after_turn()
            else:
                # barge-in: history records only what was actually spoken (D3)
                partial = (spoken.strip() + " —").strip()
                self._messages.append({"role": "assistant", "content": partial})
                self._store.add_message(self._session_id, "assistant", partial)

    async def _after_turn(self) -> None:
        """Housekeeping the user must never wait for."""
        # Chunked eviction: rebuild only once the window is well past budget, so
        # the re-prefill lands every few turns instead of every turn.
        if len(self._messages) > config.VERBATIM_TURNS * 4 + 2:
            self._rebuild()
        if self._summary_task and not self._summary_task.done():
            return
        if self._memory.needs_summary(self._session_id):
            sid = self._session_id
            self._summary_task = asyncio.create_task(
                asyncio.to_thread(self._memory.summarise, sid, self._llm))
