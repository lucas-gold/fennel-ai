"""Local WebSocket server.

Stage 2: full voice loop through `voice/session.py`. Text control frames carry
state/tokens/tool calls; binary frames carry audio — mic in (int16 mono 16 kHz,
512-sample frames), audio out (">II" header + int16 PCM @24 kHz). The typed
chat box still works and now also speaks its reply.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import date
from functools import partial

import numpy as np
import websockets

import config
import protocol as P
from voice import embed, setup as model_setup
from voice.briefing import Briefing, Retriever
from voice.llm import LLM
from voice.memory import Memory
from voice.session import Session
from voice.store import Store
from voice.stt import WhisperSTT
from voice.tools import system_prompt, tool_list
from voice.tts import KokoroTTS

_stt: WhisperSTT
_llm: LLM
_tts: KokoroTTS
_store: Store
_memory: Memory
_briefing: Briefing
_system: str
_system_day: date


def _build_system() -> str:
    """Persona + tools + day table, plus today's briefing when the user has
    opted in. All of it is stable for the whole day, which is exactly what makes
    it primeable — see D-BRIEFING."""
    base = system_prompt(config.LLM_SYSTEM)
    if brief := _briefing.cached():
        return f"{base}\n\n{brief}"
    return base


def _current_system() -> str:
    """The primed system prefix, re-primed if the app has been left running
    past midnight (the prompt's day table would otherwise be wrong)."""
    global _system, _system_day
    if date.today() != _system_day:
        _system, _system_day = _build_system(), date.today()
        _llm.prime(_system)
    return _system


def _feature_settings() -> dict[str, bool]:
    return {"web_search": _store.setting("web_search", "0") == "1"}


async def _refresh_briefing(session: Session) -> None:
    """Fetch today's briefing and fold it into the primed prefix.

    Runs in the background on the first connection of the day: the network is
    slow and unreliable, and a voice assistant must never wait on it. Until it
    lands, the model simply runs without a briefing.
    """
    global _system, _system_day
    if _briefing.is_stale():
        await asyncio.to_thread(_briefing.build, embed.shared())
    _llm.tools = tool_list(_feature_settings())
    # Re-prime whenever the prefix we *want* differs from the one that's primed,
    # not merely when the fetch was stale: toggling "daily updates" on mid-run
    # leaves a perfectly fresh briefing sitting outside the prefix otherwise.
    want = _build_system()
    want_tools = tool_list(_feature_settings())
    if want == _system and want_tools == _llm.tools:
        return
    _system, _system_day = want, date.today()
    # Tool availability lives in the primed prefix, so switching a feature on or
    # off means re-priming — the same path the daily briefing already takes.
    _llm.tools = want_tools
    # Hand the session the new prefix BEFORE priming. A turn arriving during the
    # prime blocks on the LLM lock either way, but this way it wakes up using the
    # new prefix on a warm cache rather than the stale one.
    await session.apply_system(_system)
    await asyncio.to_thread(_llm.prime, _system)
    print("[briefing] folded into the primed prefix", flush=True)


# First-run state, shared by every connection. The server starts listening
# BEFORE the models load so the app can show a consent screen and a progress
# bar; nothing touches the network until `_consent` is set from the UI.
_clients: set = set()
_consent = asyncio.Event()
_ready = asyncio.Event()
_setup_state: dict = {"phase": "checking"}   # fields only; "type" is added on send


async def _broadcast(msg: str) -> None:
    for ws in list(_clients):
        try:
            await ws.send(msg)
        except Exception:
            _clients.discard(ws)


def _set_setup(**fields) -> None:
    """Record setup state and push it to every connected client.

    Always called on the event loop — the download reporter runs on a worker
    thread and marshals through `call_soon_threadsafe` before getting here.
    """
    global _setup_state
    _setup_state = dict(fields)
    if fields.get("phase") == "ready":
        _ready.set()
    asyncio.get_running_loop().create_task(
        _broadcast(P.encode("setup", **_setup_state)))


async def handler(ws) -> None:
    async def send_control(s: str) -> None:
        await ws.send(s)

    async def send_audio(b: bytes) -> None:
        await ws.send(b)

    _clients.add(ws)
    await ws.send(P.encode("setup", **_setup_state))
    if not _ready.is_set():
        # Models aren't loaded yet: serve only the setup conversation until they
        # are, so the app can ask for consent and draw a progress bar instead of
        # showing a window that looks alive but answers nothing.
        #
        # Readiness is awaited as an Event *alongside* reading the socket. A
        # plain `async for` here only noticed the models were ready when the app
        # happened to send something — so after loading, the window showed an
        # empty chat that filled in only once the user typed.
        async def pump() -> None:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                if P.decode(raw).get("type") == "setup_consent":
                    _consent.set()

        pump_task = asyncio.create_task(pump())
        ready_task = asyncio.create_task(_ready.wait())
        done, _pending = await asyncio.wait(
            {pump_task, ready_task}, return_when=asyncio.FIRST_COMPLETED)
        # Await the cancellation, don't just request it: websockets refuses a
        # second concurrent recv, and the pump keeps its slot until the
        # CancelledError has actually been delivered.
        for t in (pump_task, ready_task):
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        if pump_task in done:          # socket closed before we were ready
            _clients.discard(ws)
            return

    session = Session(send_control, send_audio, _stt, _llm, _tts,
                      _store, _memory, system=_current_system())
    await ws.send(P.encode("state", value="idle"))
    # Push stored settings immediately. Without this the app boots showing its
    # own defaults (all off) while the backend has them on — and the next Save
    # writes those stale defaults back over the real ones.
    await ws.send(P.encode("settings", daily_updates=_briefing.enabled,
                           location=_briefing.place,
                           web_search=_store.setting("web_search", "0") == "1",
                           models=config.local_models()))
    await session.open_session()          # resume where the user left off
    asyncio.create_task(_refresh_briefing(session))  # never blocks the conversation
    try:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                # mic frame: int16 mono @16 kHz → float32 [-1, 1]
                frame = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                await session.feed_frame(frame)
            else:
                msg = P.decode(raw)
                if msg["type"] == "user_text":
                    await session.feed_text(msg.get("text", ""),
                                            speak=bool(msg.get("speak", False)))
                elif msg["type"] == "tool_result":
                    session.feed_tool_result(msg)
                elif msg["type"] == "settings":
                    _briefing.configure(enabled=msg.get("daily_updates"),
                                        place=msg.get("location"))
                    if (w := msg.get("web_search")) is not None:
                        _store.set_setting("web_search", "1" if w else "0")
                    asyncio.create_task(_refresh_briefing(session))
                    await ws.send(P.encode(
                        "settings", daily_updates=_briefing.enabled,
                        location=_briefing.place,
                        web_search=_store.setting("web_search", "0") == "1",
                        models=config.local_models()))
                elif msg["type"] == "session_list":
                    await session.send_sessions()
                elif msg["type"] == "session_open":
                    await session.open_session(int(msg["id"]))
                elif msg["type"] == "session_new":
                    await session.open_session(create=True)
                elif msg["type"] == "session_delete":
                    await session.delete_session(int(msg["id"]))
                elif msg["type"] == "ping":
                    await ws.send(P.encode("pong"))
    finally:
        _clients.discard(ws)
        await session.close()


async def main() -> None:
    global _stt, _llm, _tts, _store, _memory, _briefing, _system, _system_day
    _store = Store()
    _briefing = Briefing(_store)
    # Embeddings power both news retrieval and conversational recall; loading is
    # lazy and optional, so a failure degrades to keyword search (embed.shared).
    # NB: the embedder is NOT constructed here. Loading it downloads bge-small,
    # which would put ~130 MB on the wire before the user has been asked
    # anything — measured, and exactly the promise this screen exists to keep.
    # It is built in _prepare_models, after consent.

    # Listen FIRST. The app needs a connection to ask about downloading, and a
    # window that can't reach its backend is indistinguishable from a broken one.
    async with websockets.serve(handler, config.HOST, config.PORT, max_size=None):
        print(f"Fennel backend listening on ws://{config.HOST}:{config.PORT}  "
              f"(tier={config.TIER})", flush=True)
        await _prepare_models()
        await asyncio.Future()


async def _prepare_models() -> None:
    """Consent, download, load, warm, prime — reporting each step to the app."""
    global _stt, _llm, _tts, _memory, _system, _system_day

    if pending := model_setup.missing():
        size = model_setup.human(model_setup.total_bytes(pending))
        print(f"[setup] {len(pending)} model(s) missing, {size}", flush=True)
        _set_setup(phase="needs_consent", size=size,
                   detail="Fennel needs to download the models it runs on.")
        await _consent.wait()          # nothing has touched the network yet
        _set_setup(phase="downloading", progress=0.0, detail="Starting…", size=size)

        loop = asyncio.get_running_loop()

        def report(what: str, done: int, total: int) -> None:
            frac = 0.0 if total <= 0 else min(1.0, done / total)
            # partial, not kwargs: call_soon_threadsafe forwards positional
            # arguments only.
            loop.call_soon_threadsafe(partial(
                _set_setup, phase="downloading", progress=frac, size=size,
                detail=f"Downloading the {what} — "
                       f"{model_setup.human(done)} of {model_setup.human(total)}"))

        try:
            await asyncio.to_thread(model_setup.download, report)
        except Exception as exc:
            print(f"[setup] download failed: {exc}", flush=True)
            _set_setup(phase="failed",
                       detail=f"Download failed: {exc}. Check your connection "
                              "and reopen Fennel.")
            return

    # Estimate from the last successful start; the first run has no history, so
    # it gets a rough default and then records the real number for next time.
    eta = float(_store.setting("startup_seconds", "45") or 45)
    started = time.monotonic()
    # Weights go into unified memory one model at a time; naming each with its
    # size is more informative than a spinner, and explains where the wait goes.
    steps = [
        ("Qwen3-4B — the conversation", "2.3 GB"),
        ("Kokoro — the voice", "0.6 GB"),
        ("Whisper — speech recognition", "0.5 GB"),
        ("bge-small — memory and recall", "0.1 GB"),
    ]

    def loading(i: int) -> None:
        name, size = steps[i]
        done = sum(float(s.split()[0]) for _, s in steps[:i])
        _set_setup(phase="loading", eta=max(1.0, eta - (time.monotonic() - started)),
                   detail=f"Loading {name} — {size} into memory",
                   loaded=f"{done:.1f} GB of 3.5 GB in memory")

    loading(0)
    print("loading models "
          f"({config.LLM_MODEL.split('/')[-1]}, "
          f"{config.STT_MODEL.split('/')[-1]}, "
          f"{config.TTS_MODEL.split('/')[-1]}) …", flush=True)
    _llm = await asyncio.to_thread(LLM)
    loading(1)
    _tts = await asyncio.to_thread(KokoroTTS)
    loading(2)
    _stt = WhisperSTT()
    loading(3)
    # Embeddings power both news retrieval and conversational recall; loading is
    # lazy and optional, so a failure degrades to keyword search (embed.shared).
    _emb = embed.shared()
    _memory = Memory(_store, Retriever(_store, _emb), _emb)

    _set_setup(phase="loading", detail="Warming up — compiling GPU kernels",
               loaded="3.5 GB in memory",
               eta=max(1.0, eta - (time.monotonic() - started)))
    print("warming models …", flush=True)  # move cold-start cost off turn 1
    await asyncio.to_thread(_stt.warmup)
    await asyncio.to_thread(_tts.warmup)
    await asyncio.to_thread(_llm.warmup)

    if _briefing.is_stale():
        await asyncio.to_thread(_briefing.build, embed.shared())
    _llm.tools = tool_list(_feature_settings())
    # Prefill tool schemas + day table + briefing now rather than during the
    # user's first sentence — ~1600-2200 tokens of stable prefix (D4).
    _system, _system_day = _build_system(), date.today()

    # Priming happens before ready again. Backgrounding it only moved the wait
    # onto the user's first question, which is worse — but it is no longer a
    # wait: the primed KV state is restored from disk in ~0.06 s instead of the
    # ~15 s it takes to recompute (D-PRIMECACHE). Only the first launch after
    # the briefing changes pays the full cost.
    _set_setup(phase="loading", detail="Preparing the conversation…",
               loaded="3.5 GB in memory",
               eta=max(1.0, eta - (time.monotonic() - started)))
    await asyncio.to_thread(_llm.prime, _system)

    _store.set_setting("startup_seconds", f"{time.monotonic() - started:.0f}")
    _set_setup(phase="ready")
    print("Fennel ready.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
