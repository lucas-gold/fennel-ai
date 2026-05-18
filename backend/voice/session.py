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
import re
import threading
import time
from collections import deque
from typing import Awaitable, Callable, Optional
from uuid import uuid4

import numpy as np

import config
import protocol as P
from voice import feeds, shortcuts
from voice.llm import LLM
from voice.memory import Memory
from voice.store import Store
from voice.stt import WhisperSTT
from voice.tools import ANSWERING_TOOLS, ToolStream, normalize, system_prompt
from voice.tts import ClauseSplitter, KokoroTTS
from voice.vad import Endpointer

# Tools slow enough that silence reads as a hang. The backend says these, not
# the model: instructing the model to speak before calling made it sometimes say
# the line and never call at all.
LEAD_INS = {
    "search_web": "Let me look that up.",
    "create_shortcut": "Let me put that together.",
    "agenda": "Let me check.",
}

# Unicode symbol/pictograph ranges plus the joiners that compose them.
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


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
        # Wall-clock instant the audio we've sent will finish playing. The app
        # buffers whole clauses, so playback outlives generation by seconds —
        # guarding only while `_assistant_active` left the tail unprotected.
        self._audio_until = 0.0
        # What we recently said out loud, for the echo check below.
        self._spoken_log: deque = deque(maxlen=40)
        # Emoji rationing. Asking the model for "occasionally" doesn't work: at
        # "almost never" it used none at all, at "occasionally" it put one in
        # every single reply. The middle the user actually wants is a rule, not
        # an adverb — at most one per reply, and never twice running.
        self._last_reply_had_emoji = False
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
        guarded = self._assistant_active or time.monotonic() < self._audio_until
        endpoint = self._endpointer.process(frame_f32, guarded=guarded)
        onset = self._endpointer.speaking and not was_speaking

        self._rx += 1
        if self._rx % 50 == 0:
            peak = float(np.abs(frame_f32).max())
            print(f"[vad] rx={self._rx} frames peak={peak:.3f} "
                  f"speaking={self._endpointer.speaking}", flush=True)
        if onset:
            print("[vad] speech onset", flush=True)

        if onset and guarded:      # includes the tail still coming out of the speakers
            await self._barge_in()
        if endpoint is not None:
            print(f"[vad] endpoint: {len(endpoint.audio)/16000:.2f}s", flush=True)
            await self._start_turn(audio=endpoint.audio, echo_risk=guarded)

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
        self._audio_until = 0.0
        await self._send_control(P.encode("cancel"))   # drop queued audio now
        await self._send_control(P.encode("state", value="idle"))
        # The interrupted turn commits its partial reply in _run_turn's finally.

    async def _supersede(self) -> None:
        """Ensure any in-flight turn has fully wound down (and committed).

        Sends `cancel` as well as bumping the epoch. The epoch stops us
        *generating*, but the app has already been handed whole clauses of audio
        and will happily finish playing them — so the assistant kept talking
        over an interruption whenever barge-in itself hadn't fired (a typed
        message, or speech the stricter VAD gate only recognised at endpoint).
        """
        if self._turn_task and not self._turn_task.done():
            self._epoch += 1
            self._stop.set()
            self._audio_until = 0.0
            await self._send_control(P.encode("cancel"))
            await self._turn_task

    async def _start_turn(self, audio: Optional[np.ndarray] = None,
                          text: Optional[str] = None, speak: bool = True,
                          echo_risk: bool = False) -> None:
        # The user is back: drop any summary still waiting for a lull rather
        # than letting it grab the LLM lock ahead of their turn.
        if self._summary_task and not self._summary_task.done():
            self._summary_task.cancel()
        await self._supersede()
        self._epoch += 1
        self._stop = threading.Event()
        epoch = self._epoch
        self._turn_task = asyncio.create_task(
            self._run_turn(epoch, audio, text, speak, echo_risk))

    # ── tools ──────────────────────────────────────────────────────────────

    def _is_echo(self, heard: str) -> bool:
        """Is this transcript just our own speech coming back through the mic?

        Echo cancellation plus a stricter VAD gate stop most of it, but not all —
        speakers at volume in a hard room still get through, and the failure is
        loud: the assistant answers itself. This is the last line, and it is only
        consulted for audio captured while our own voice was actually playing.

        Word overlap rather than exact match, because STT mangles re-recorded
        audio. Short fragments need near-total overlap so a genuine one-word
        reply survives; longer ones can be looser, since the odds of the user
        independently producing eight of our words in a row are slim.
        """
        words = _words(heard)
        if not words:
            return True                      # nothing intelligible; not worth a turn
        spoken = set()
        for clause in self._spoken_log:
            spoken.update(_words(clause))
        if not spoken:
            return False
        overlap = sum(1 for w in words if w in spoken) / len(words)
        threshold = 0.9 if len(words) <= 3 else 0.65
        if overlap >= threshold:
            print(f"[stt] echo overlap {overlap:.2f} of {len(words)} words", flush=True)
            return True
        return False

    async def _run_tool(self, call: dict, say=None) -> dict:
        """Normalize, hand to the app, and wait briefly for the real outcome so
        the model's spoken confirmation isn't a lie."""
        name, args = call["name"], call["args"]
        card, result = normalize(name, args)
        print(f"[tool] {name} {card or args}", flush=True)
        if not result.get("ok"):
            return {"name": name, **result}

        # Fill the silence before the slow part starts, not after.
        if say is not None and name in LEAD_INS:
            await say(LEAD_INS[name])

        if name == "set_fact":
            self._store.set_fact(card["key"], card["value"])

        if name == "create_shortcut":
            try:
                path = await asyncio.to_thread(
                    shortcuts.write_signed, card["name"], card["steps"])
            except shortcuts.ShortcutError as exc:
                return {"name": name, "ok": False, "error": str(exc)}
            except Exception as exc:
                return {"name": name, "ok": False,
                        "error": f"couldn't build the shortcut: {exc}"}
            # The app opens it; macOS shows an Add sheet listing every action, so
            # nothing reaches their library without them approving it.
            card = {**card, "path": path}
            result = {"ok": True, "name": card["name"],
                      "note": "waiting for them to press Add"}

        if name == "search_web":
            # Its own setting, not the daily-updates one: a daily fetch of fixed
            # feeds reveals nothing about the user, whereas this sends their
            # actual question to a third party (D-BRIEFING).
            if self._store.setting("web_search", "0") != "1":
                return {"name": name, "ok": False,
                        "error": "web search is turned off in settings; answer "
                                 "from what you know and say you couldn't look it up"}
            hits = await asyncio.to_thread(feeds.wiki_search, card["query"])
            if not hits:
                return {"name": name, "ok": False,
                        "error": f"nothing found on Wikipedia for {card['query']!r}"}
            # The card carries the extract, not just the title: a list of bare
            # headings tells the user nothing about what was actually found.
            card = {**card, "results": [
                {"title": h.title, "extract": h.summary[:320], "link": h.link}
                for h in hits]}
            result = {"ok": True, "source": "Wikipedia",
                      "results": [{"title": h.title, "extract": h.summary} for h in hits]}

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
                        text: Optional[str], speak: bool = True,
                        echo_risk: bool = False) -> None:
        from_voice = text is None
        do_speak = from_voice or speak       # voice turns always speak
        # Typed turns need this too. It used to be inside `if from_voice`, so a
        # typed message showed no thinking state and no typing indicator — the
        # window just sat there looking stuck.
        await self._send_control(P.encode("state", value="thinking"))
        if from_voice:
            text = await asyncio.to_thread(self._stt.transcribe, audio)
            print(f"[stt] -> {text!r}", flush=True)
            if echo_risk and self._is_echo(text):
                print("[stt] discarded: it's our own voice", flush=True)
                await self._send_control(P.encode("state", value="idle"))
                return
        if not self._live(epoch) or not text.strip():
            # Nothing to answer, but we already announced "thinking" — say so.
            if self._live(epoch):
                await self._send_control(P.encode("state", value="idle"))
            return
        if from_voice:  # let the UI show what was heard
            await self._send_control(P.encode("stt", text=text))
        # The store keeps what the user actually said; the prompt gets the
        # volatile preamble (clock + recall) glued on front — last position in
        # the prompt, so the cached prefix behind it survives (D4).
        prompt_user = self._memory.preamble(self._session_id, text) + text
        self._memory.remember(self._session_id, "user", text,
                              prompt_text=prompt_user)
        self._messages.append({"role": "user", "content": prompt_user})
        turn_start = len(self._messages)   # everything after this is this turn's

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
            now = time.monotonic()
            self._audio_until = max(now, self._audio_until) + len(pcm) / 24000.0
            self._spoken_log.append(clause)
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

            emoji_used = False

            def ration_emoji(prose: str) -> str:
                nonlocal emoji_used
                if not _EMOJI.search(prose):
                    return prose
                if emoji_used or self._last_reply_had_emoji:
                    return _EMOJI.sub("", prose).replace("  ", " ")
                emoji_used = True
                return prose

            async def lead_in(text: str) -> None:
                """Speak a holding line for a slow tool — but only if the model
                hasn't already said something itself, or they stack up."""
                nonlocal said
                if said.strip():
                    return
                said += text
                await self._send_control(P.encode("token", turn=turn, text=text))
                if do_speak:
                    await speak_clause(text)

            async for chunk in self._llm.astream(self._messages, stop=self._stop):
                if not self._live(epoch):
                    break
                prose, new_calls = ts.feed(chunk)
                calls += new_calls
                for c in new_calls:            # fire while the model still talks
                    c["result"] = await self._run_tool(c, say=lead_in)
                if not prose:
                    continue
                prose = space(ration_emoji(prose))
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
                    c["result"] = await self._run_tool(c, say=lead_in)
                calls += new_calls
                if prose:
                    prose = space(ration_emoji(prose))
                    said += prose
                    await self._send_control(P.encode("token", turn=turn, text=prose))
                    splitter.feed(prose)
                if do_speak:
                    await speak_clause(splitter.flush())
            self._last_reply_had_emoji = emoji_used
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
                # Never skip for a tool whose result IS the answer, or the user
                # hears "let me look that up" and then nothing.
                answering = any(c["name"] in ANSWERING_TOOLS for c in calls)
                if (not answering and len(said.strip()) >= 15
                        and all(r.get("ok") for r in results)):
                    break
        finally:
            self._assistant_active = False
            if self._live(epoch):
                self._commit(turn_start, visible.strip())
                await self._send_control(P.encode("turn_end", turn=turn))
                # Unconditional, to match the unconditional "thinking" above.
                # Gated on do_speak, a typed turn with speech off never came out
                # of thinking and the UI stayed stuck on it forever.
                await self._send_control(P.encode("state", value="idle"))
                await self._after_turn()
            else:
                # barge-in: history records only what was actually spoken (D3)
                partial = (spoken.strip() + " —").strip()
                self._messages.append({"role": "assistant", "content": partial})
                self._commit(turn_start, partial)

    def _window_over_budget(self) -> bool:
        return len(self._messages) > config.VERBATIM_TURNS * 4 + 2

    def _commit(self, start: int, visible: str) -> None:
        """Persist every message this turn produced — assistant generations with
        their <tool_call> blocks intact, and the tool results after them.

        Only the last assistant message carries the visible text, because that is
        what the chat shows; the rest store empty content and the app skips them.
        Storing just the spoken text is what let a resumed conversation teach the
        model to *describe* actions instead of taking them.
        """
        added = self._messages[start:]
        last_assistant = max(
            (i for i, m in enumerate(added) if m["role"] == "assistant"), default=-1)
        for i, m in enumerate(added):
            self._memory.remember(
                self._session_id, m["role"],
                content=visible if i == last_assistant else "",
                prompt_text=m["content"])

    async def _after_turn(self) -> None:
        """Housekeeping the user must never wait for."""
        # Safety valve: in a conversation with no pauses, `_start_turn` cancels
        # maintenance every turn and the window would grow without limit. Well
        # past budget we take the spike rather than let context run away.
        if len(self._messages) > config.VERBATIM_TURNS * 8:
            print("[session] window far over budget; trimming inline", flush=True)
            self._rebuild()
            return
        if self._summary_task and not self._summary_task.done():
            return
        if self._window_over_budget() or self._memory.needs_summary(self._session_id):
            self._summary_task = asyncio.create_task(self._maintain_when_idle())

    async def _maintain_when_idle(self) -> None:
        """Trim the window and summarise during a lull, never straight after a turn.

        Both hold the LLM lock for seconds. Trimming the window is the worse of
        the two: it changes the prompt at the front, so the next turn pays a full
        re-prefill — measured at 2.2 s, landing on one unlucky turn in ~17, which
        is exactly the intermittent hitch you notice in conversation. Doing it in
        a pause and pre-warming the new prompt removes the spike; `_start_turn`
        cancels this if the user comes back first.
        """
        await asyncio.sleep(config.SUMMARY_IDLE_S)
        sid = self._session_id
        if self._window_over_budget():
            self._rebuild()
            await asyncio.to_thread(self._llm.warm, list(self._messages))
        if self._memory.needs_summary(sid):
            await asyncio.to_thread(self._memory.summarise, sid, self._llm)
