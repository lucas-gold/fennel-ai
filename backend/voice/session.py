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
import os
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
    "search_wikipedia": "Let me look that up.",
    "search_web": "Let me search the web for that.",
    "create_shortcut": "Let me put that together.",
    "agenda": "Let me check.",
}

# A request to write something the user will send or keep. Drafting is the one
# task where conversational temperature measurably hurts: the padding a warm
# sampler adds is exactly where the invented details live (D-DRAFT).
# "Draw me a X" is unmistakable, and whether it becomes a picture should not
# depend on a 4B choosing to call a tool this time. If the user plainly asked
# and no image was raised, the request is honoured from their own words.
#: One picture at a time. A second request while one is drawing waits rather
#: than doubling the memory and halving the speed of both.
_IMAGE_LOCK = asyncio.Lock()

_DRAWY = re.compile(
    r"\b(draw|sketch|paint|generate|create|make|render|show|design)\b[^.?!]{0,24}?"
    # Not just "picture": a logo, an icon, a poster are all this feature, and
    # asking for one used to fall through to an empty reply.
    r"\b(image|picture|photo|photograph|drawing|painting|artwork|illustration|"
    r"logo|icon|poster|wallpaper|portrait|banner|sketch|render|mockup|design)s?\b"
    r"|\bimagine\s+(a|an|the)\b"
    # The bare imperative — "draw a red barn". Requires an article after the
    # verb, which is what separates it from "draw your own conclusions".
    r"|^\s*(please\s+)?(draw|sketch|paint|render)\s+(me\s+)?(a|an|the)\b", re.I)

_DRAFTY = re.compile(
    r"\b(write|draft|compose|reword|rewrite|edit|proofread)\b[^.?!]{0,40}?"
    r"\b(email|e-mail|message|note|letter|text|reply|response|memo|post|"
    r"caption|bio|invitation|invite|thank[- ]?you)\b", re.I)

# A reply that announces an action instead of taking it: "let me look that up",
# "one moment", "I'll check". Harmless when a tool call follows in the same
# generation; a dead end when none does, and the user is left watching a promise.
# It happens most with models whose template drops the tool schemas entirely, but
# any model does it occasionally, so the recovery lives here rather than in the
# prompt (a rule the model must not break belongs in code).
_PROMISE = re.compile(
    r"\b(let me (just |quickly )?(search|look|check|find|see)"
    r"|i'?ll (search|look|check|find|see|get)"
    r"|looking (that|it|this) up|searching (the web|for|now)"
    r"|one (moment|sec|second)|hold on|give me a (sec|second|moment)"
    r"|checking (that|it|this|now)|let me pull)\b", re.I)

# Unicode symbol/pictograph ranges plus the joiners that compose them.
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


# Led in from inside the search branch instead, once we know a request is
# actually going out — announcing "let me search the web" and then admitting we
# can't is worse than simply saying we can't.
_LEADS_ITSELF = {"search_web", "search_wikipedia"}


async def _lead(say, name: str) -> None:
    """Speak the holding line for a slow tool. `say` suppresses itself once the
    turn has produced any prose, so calling this twice is harmless."""
    if say is not None and (line := LEAD_INS.get(name)):
        await say(line)


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
        # Set by the server: await it with True to have the language model
        # unloaded, False to bring it back. Only image generation uses this.
        self._on_need_memory = None
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
        self._warm_task: Optional[asyncio.Task] = None
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

        # Resuming replays the verbatim window, and that is a real prefill —
        # measured 3,370 tokens (~18 s here) landing on whatever the user says
        # first, with the orb claiming to speak the whole time. Do it now, in the
        # lull between the window appearing and them saying anything.
        if len(self._messages) > 1:
            self._warm_task = asyncio.create_task(
                asyncio.to_thread(self._llm.warm, list(self._messages)))

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
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()
        await self._supersede()
        self._epoch += 1
        self._stop = threading.Event()
        epoch = self._epoch
        self._turn_task = asyncio.create_task(
            self._run_turn(epoch, audio, text, speak, echo_risk))

    # ── tools ──────────────────────────────────────────────────────────────

    def _quota_block(self) -> Optional[str]:
        """Why web search shouldn't be attempted right now, or None.

        Once a key is refused we stop calling out entirely rather than retrying
        into a wall — but only for a cooldown, since free allowances reset. The
        tool stays listed so that if the user insists, the model can explain
        what happened instead of pretending the ability never existed.
        """
        if not self._store.setting("web_key", ""):
            return ("no web search key is set up; Wikipedia is available though, "
                    "and they can add a key in the network settings")
        hit = self._store.setting("web_quota_hit", "")
        if not hit:
            return None
        try:
            age = time.time() - float(hit)
        except ValueError:
            return None
        if age < config.WEB_QUOTA_COOLDOWN_S:
            hours = int((config.WEB_QUOTA_COOLDOWN_S - age) // 3600) + 1
            return (f"web search is paused — the key ran out of allowance. It "
                    f"retries in about {hours} hour(s). Say so, and offer "
                    f"Wikipedia instead.")
        self._store.set_setting("web_quota_hit", "")   # cooldown over, try again
        return None

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

        if name in ("search_wikipedia", "search_web"):
            # Its own setting, not the daily-updates one: a daily fetch of fixed
            # feeds reveals nothing about the user, whereas this sends their
            # actual question to a third party (D-BRIEFING).
            if self._store.setting("lookups", "0") != "1":
                return {"name": name, "ok": False,
                        "error": "looking things up is turned off in settings; "
                                 "answer from what you know and say you couldn't "
                                 "look it up"}
            if name == "search_web":
                if blocked := self._quota_block():
                    # Before any lead-in: promising "let me search the web" and
                    # then admitting we can't is worse than simply saying so.
                    # Short-circuit: no request is made at all, and the model is
                    # told why so it can explain if the user pushes.
                    return {"name": name, "ok": False, "error": blocked}
                key = self._store.setting("web_key", "")
                await _lead(say, name)
                try:
                    hits = await asyncio.to_thread(feeds.web_search, card["query"], key)
                except feeds.QuotaExhausted as exc:
                    self._store.set_setting("web_quota_hit", str(time.time()))
                    print(f"[search] web quota/auth failure: {exc}", flush=True)
                    return {"name": name, "ok": False,
                            "error": "the web search key is out of allowance or "
                                     "no longer valid, so web search is paused. "
                                     "Tell them, and offer Wikipedia instead."}
                source = "Web"
            else:
                await _lead(say, name)
                hits = await asyncio.to_thread(feeds.wiki_search, card["query"])
                source = "Wikipedia"

            if not hits:
                return {"name": name, "ok": False,
                        "error": f"nothing useful came back from {source} for "
                                 f"{card['query']!r} — try once more with "
                                 f"different terms, or say you couldn't find it"}
            # The card carries the extract, not just the title: a list of bare
            # headings tells the user nothing about what was actually found.
            card = {**card, "source": source, "results": [
                {"title": h.title, "extract": h.summary[:320], "link": h.link}
                for h in hits]}
            result = {"ok": True, "source": source,
                      "results": [{"title": h.title, "extract": h.summary,
                                   "url": h.link} for h in hits]}

        if name not in _LEADS_ITSELF:
            await _lead(say, name)

        call_id = uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        await self._send_control(P.encode("tool", id=call_id, name=name, args=card))

        if name == "generate_image":
            # The picture takes about a minute. The card is already on screen,
            # so draw it in the background and let the turn finish talking —
            # blocking here would leave the user watching a dead conversation.
            self._pending.pop(call_id, None)
            asyncio.create_task(self._draw_image(call_id, card))
            return {"name": name, "ok": True, "status": "started"}
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

        # The language model may have stepped aside so a picture could be drawn
        # (see _draw_image). Answer plainly instead of calling into an object
        # whose weights have been freed — that used to leave the conversation
        # dead until the image finished.
        if self._llm is None or not self._llm.available:
            await self._hold_the_line(epoch, text, do_speak)
            return

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
            if not announced_speaking[0]:
                announced_speaking[0] = True
                await self._send_control(P.encode("state", value="speaking"))
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

            async for chunk in self._llm.astream(self._messages, stop=self._stop,
                                                 temp=draft_temp):
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
            self._image_desc = ts.image_description.strip()
            return ts.raw, calls, said

        self._assistant_active = do_speak
        # "Speaking" is announced by speak_clause when the first audio actually
        # goes out, not here. Announcing it up front meant the orb read
        # "Speaking" through the whole prefill and generation — on a resumed
        # session that is ~18 s of silence labelled as speech.
        visible = ""
        announced_speaking = [False]   # list so the nested speak_clause can set it
        draft_temp = config.LLM_DRAFT_TEMP if _DRAFTY.search(text) else None
        if draft_temp is not None:
            print(f"[llm] drafting turn: temp={draft_temp}", flush=True)
        fired = False
        self._image_desc = ""
        try:
            for round_ in range(config.LLM_TOOL_ROUNDS + 1):
                raw, calls, said = await llm_pass()
                visible += said
                fired = fired or bool(calls)
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

            # Promised but never acted. One rescue round, never a loop: tell it
            # plainly that nothing was called and make it either call or answer.
            # Without this the turn ends on "let me search for that" and simply
            # stops, which reads as the app having hung.
            if self._live(epoch) and not fired and _PROMISE.search(visible):
                print("[llm] promise with no tool call — forcing a follow-up",
                      flush=True)
                self._messages.append({"role": "tool", "content": json.dumps({
                    "ok": False,
                    "error": "You said you would look something up, but no tool "
                             "was called and nothing ran. Either call the tool "
                             "now, or answer directly from what you already "
                             "know. Do not say you are checking or searching "
                             "again — the user is waiting on an answer."})})
                raw, _unused, said = await llm_pass()
                visible += said
                if self._live(epoch):
                    self._messages.append({"role": "assistant", "content": raw})
            # Smaller models answer "draw me X" by writing an
            # <image_description> block instead of calling generate_image, and
            # no amount of telling them otherwise fixes it. The block is a
            # perfectly good prompt, so use it: the user asked for a picture and
            # gets one, rather than an empty reply where the tag was stripped.
            drew = any(c["name"] == "generate_image" for c in calls)
            want_image = ((self._store.setting("images", "1") or "1") == "1"
                          and not drew and self._live(epoch)
                          and (self._image_desc or _DRAWY.search(text or "")))
            if want_image:
                # Prefer the model's own description — it is a richer prompt
                # than the bare request — but fall back to what the user said,
                # so asking plainly always produces a picture.
                self._image_desc = self._image_desc or (text or "")
                if not visible.strip():
                    # The whole reply was the description, which is stripped —
                    # so without this the user gets an empty bubble and a card
                    # appearing out of nowhere.
                    line = "Drawing that now — it takes about a minute."
                    visible += line
                    await self._send_control(
                        P.encode("token", turn=turn, text=line))
                await self._draw_described(self._image_desc)
            # Whatever happened above, an empty bubble is not an answer. This
            # caught a request for a logo that produced only a stripped tag and
            # no fallback: three dots, then nothing at all.
            if self._live(epoch) and not visible.strip() and not fired:
                line = "Sorry — nothing came back that time. Try asking again?"
                visible += line
                await self._send_control(P.encode("token", turn=turn, text=line))
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

    async def _hold_the_line(self, epoch: int, text: Optional[str],
                             do_speak: bool) -> None:
        """Reply while the model is set aside for image generation.

        A canned sentence rather than silence: the user typed something, and a
        chat that stops answering reads as broken. What they said is kept, so
        the model sees it once it is back.
        """
        # A backstop only: the app disables the composer while `busy` is set,
        # so this should never be reached. It exists because "should never" and
        # "cannot" are different, and the alternative is a crash.
        line = "One moment — I'm drawing that picture."
        self._turn_no += 1
        turn = self._turn_no
        if text:
            self._messages.append({"role": "user", "content": text})
        await self._send_control(P.encode("token", turn=turn, text=line))
        if do_speak:
            pcm = await asyncio.to_thread(self._tts.synth_pcm, line)
            if pcm.size and self._live(epoch):
                await self._send_control(P.encode("state", value="speaking"))
                await self._send_audio(P.pack_audio(turn, 0, pcm))
        await self._send_control(P.encode("turn_end", turn=turn))
        await self._send_control(P.encode("state", value="idle"))

    async def _draw_described(self, description: str) -> None:
        """Raise an image card for a description the model wrote instead of
        calling the tool, and draw it."""
        from voice.tools import normalize
        card, _ = normalize("generate_image", {"prompt": description})
        if not card:
            return
        call_id = uuid4().hex[:8]
        print("[image] model described instead of calling; drawing it anyway",
              flush=True)
        await self._send_control(
            P.encode("tool", id=call_id, name="generate_image", args=card))
        asyncio.create_task(self._draw_image(call_id, card))

    async def _draw_image(self, card_id: str, card: dict) -> None:
        """Render a picture and post it back to its card.

        Runs outside the turn: generation takes about a minute, and the model
        that asked for it should be free to keep talking meanwhile.
        """
        from voice import images, sysmem

        async def update(**fields) -> None:
            await self._send_control(P.encode("card_update", id=card_id, **fields))

        if _IMAGE_LOCK.locked():
            await update(status="working",
                         detail="Waiting for the picture ahead of this one…")
        async with _IMAGE_LOCK:
            await self._draw_one(card_id, card, update)

    async def _draw_one(self, card_id: str, card: dict, update) -> None:
        from voice import images, sysmem

        snap = sysmem.snapshot(0)
        free = max(0, snap["system_total_bytes"] - snap["system_used_bytes"])
        pixels, low_ram, unload = images.plan(free)
        if not images.installed():
            await update(status="working", detail="Downloading the image model — 4.6 GB, once")
        else:
            await update(status="working", detail="Starting…")

        loop = asyncio.get_running_loop()

        def report(detail: str, frac: float) -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(update(status="working",
                                                   detail=detail, progress=frac)))

        out = os.path.join(images.images_dir(),
                           images.filename_for(card.get("subject", ""), card_id))
        released = False
        try:
            if unload and self._on_need_memory is not None:
                # Not enough room beside the language model. Give it back for
                # the duration rather than letting the machine swap, which would
                # cost far more than the reload does.
                await update(status="working", detail="Freeing memory…")
                await self._on_need_memory(True)
                released = True
            await asyncio.to_thread(
                images.generate, card["prompt"], out,
                pixels=pixels, low_ram=low_ram, progress=report, token=card_id)
            await update(status="done", path=out)
            print(f"[image] delivered {out}", flush=True)
        except images.Cancelled:
            print(f"[image] {card_id} cancelled by the user", flush=True)
        except Exception as exc:
            print(f"[image] failed: {exc}", flush=True)
            await update(status="failed", detail=str(exc)[:160])
        finally:
            if released and self._on_need_memory is not None:
                await self._on_need_memory(False)

    def rebind_llm(self, llm: LLM) -> None:
        """Point this conversation at a newly loaded model.

        A Session captures the LLM when it is built, so switching models without
        this would leave every open connection talking to the old weights — and
        holding the reference that stops them being freed. The KV cache belongs
        to the old model and cannot carry over, so the new one starts cold and
        re-prefills on the next turn.
        """
        self._llm = llm
        self._llm.reset()

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
