"""Local WebSocket server.

Stage 2: full voice loop through `voice/session.py`. Text control frames carry
state/tokens/tool calls; binary frames carry audio — mic in (int16 mono 16 kHz,
512-sample frames), audio out (">II" header + int16 PCM @24 kHz). The typed
chat box still works and now also speaks its reply.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import traceback
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
    """Which optional tools to advertise. The web tool needs a key as well as
    the switch — offering a tool that can only fail teaches the model to fail."""
    on = _store.setting("lookups", "0") == "1"
    return {"lookups": on, "web_key": on and bool(_store.setting("web_key", ""))}


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
# Live Sessions, so a model swap can repoint them; each holds a direct
# reference to the LLM it was built with.
_sessions: set = set()
_consent = asyncio.Event()
_ready = asyncio.Event()
# The startup model picker. Shown every launch, because which model is
# loaded is the single biggest thing about a session and picking it is
# cheaper than discovering it. `_chosen_model` is set from the UI.
_model_chosen = asyncio.Event()
_chosen_model: str = ""
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
                m = P.decode(raw)
                kind = m.get("type")
                if kind == "setup_consent":
                    _consent.set()
                elif kind == "model_select":
                    global _chosen_model
                    _chosen_model = str(m.get("id", ""))
                    _model_chosen.set()
                elif kind == "model_delete":
                    # Deleting is allowed while the picker is up because nothing
                    # is loaded yet; once a model is in memory it is protected.
                    try:
                        model_setup.delete(
                            str(m.get("id", "")),
                            in_use=config.LLM_MODEL if _ready.is_set() else None)
                        note = ""
                    except Exception as exc:
                        note = str(exc)
                    _set_setup(phase="choose_model",
                               models=model_setup.catalogue(),
                               current=_store.setting("llm_model", "")
                                       or config.DEFAULT_MODEL,
                               note=note)

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
    _sessions.add(session)
    await ws.send(P.encode("state", value="idle"))
    # Push stored settings immediately. Without this the app boots showing its
    # own defaults (all off) while the backend has them on — and the next Save
    # writes those stale defaults back over the real ones.
    await ws.send(P.encode("settings", daily_updates=_briefing.enabled,
                           location=_briefing.place,
                           lookups=_store.setting("lookups", "0") == "1",
                           has_web_key=bool(_store.setting("web_key", "")),
                           web_paused=bool(_store.setting("web_quota_hit", "")),
                           models=config.local_models(),
                        model_name=config.model_info(config.LLM_MODEL)["name"],
                        model_id=config.LLM_MODEL))
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
                    if (w := msg.get("lookups")) is not None:
                        _store.set_setting("lookups", "1" if w else "0")
                    # The key arrives from the app's Keychain. "" clears it, and
                    # a new key clears the quota pause so it is tried again.
                    if (k := msg.get("web_key")) is not None:
                        _store.set_setting("web_key", str(k).strip())
                        _store.set_setting("web_quota_hit", "")
                    asyncio.create_task(_refresh_briefing(session))
                    await ws.send(P.encode(
                        "settings", daily_updates=_briefing.enabled,
                        location=_briefing.place,
                        lookups=_store.setting("lookups", "0") == "1",
                        has_web_key=bool(_store.setting("web_key", "")),
                        web_paused=bool(_store.setting("web_quota_hit", "")),
                        models=config.local_models(),
                        model_name=config.model_info(config.LLM_MODEL)["name"],
                        model_id=config.LLM_MODEL))
                elif msg["type"] == "session_list":
                    await session.send_sessions()
                elif msg["type"] == "session_open":
                    await session.open_session(int(msg["id"]))
                elif msg["type"] == "session_new":
                    await session.open_session(create=True)
                elif msg["type"] == "session_delete":
                    await session.delete_session(int(msg["id"]))
                elif msg["type"] == "model_select":
                    # The same frames the startup gate handles, because the
                    # picker can be reopened from the composer once the app is
                    # running and its buttons must keep working there too.
                    global _chosen_model
                    _chosen_model = str(msg.get("id", ""))
                    _model_chosen.set()
                elif msg["type"] == "model_delete":
                    try:
                        model_setup.delete(str(msg.get("id", "")),
                                           in_use=config.LLM_MODEL)
                        note = ""
                    except Exception as exc:
                        note = str(exc)
                    _set_setup(phase="choose_model",
                               models=model_setup.catalogue(),
                               current=config.LLM_MODEL, note=note)
                elif msg["type"] == "model_reopen":
                    # Back to the picker without leaving the app. Nothing is
                    # unloaded yet: choosing the same model again should cost
                    # nothing at all, so the swap only happens once a different
                    # one is actually confirmed.
                    asyncio.create_task(_switch_model())
                elif msg["type"] == "ping":
                    await ws.send(P.encode("pong"))
    finally:
        _clients.discard(ws)
        _sessions.discard(session)
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
        asyncio.create_task(_watch_parent())
        try:
            await _prepare_models()
        except Exception as exc:
            # Keep serving. Startup failures used to propagate out of
            # asyncio.run and kill the process, and a dead backend is
            # indistinguishable from a slow one: the window sat on "Any moment
            # now…" forever. Staying up long enough to say what broke is the
            # whole difference between a bug report and a mystery.
            traceback.print_exc()
            _set_setup(phase="failed",
                       detail=f"Couldn't start the model: {exc}")
        await asyncio.Future()


async def _watch_parent() -> None:
    """Exit if the app that launched us goes away.

    A force-quit or crash skips `BackendProcess.stop()`, leaving an orphaned
    backend holding port 8420 — and the next launch then fails to bind and sits
    on the loading screen forever. Only armed when the app passes its pid, so a
    backend started by hand from a terminal is unaffected.
    """
    parent = os.environ.get("FENNEL_PARENT_PID")
    if not parent or not parent.isdigit():
        return
    pid = int(parent)
    while True:
        await asyncio.sleep(2)
        try:
            os.kill(pid, 0)          # signal 0 just tests for existence
        except OSError:
            print("[backend] the app exited; shutting down", flush=True)
            os._exit(0)


async def _switch_model() -> None:
    """Reopen the picker mid-session, and swap the model if a different one is
    chosen.

    Deliberately lazy: the running model stays in memory while the picker is up,
    so reopening it and choosing the same row costs nothing. Only a genuine
    change unloads, and it unloads *before* loading the replacement — on a 24 GB
    machine two sets of weights do not fit at once.
    """
    global _llm, _chosen_model, _system, _system_day

    if not _ready.is_set():
        return                       # already in setup; nothing to reopen
    was = config.LLM_MODEL
    _model_chosen.clear()
    _chosen_model = ""
    _ready.clear()
    _set_setup(phase="choose_model", models=model_setup.catalogue(),
               current=was, note="")
    await _model_chosen.wait()
    pick = _chosen_model or was

    if pick == was:
        print("[setup] same model kept; nothing reloaded", flush=True)
        _set_setup(phase="ready")
        return

    config.LLM_MODEL = pick
    _store.set_setting("llm_model", pick)
    chosen = config.model_info(pick)
    print(f"[setup] switching to {chosen['name']} ({pick})", flush=True)

    try:
        if model_setup.missing():
            size = model_setup.human(model_setup.total_bytes())
            _consent.clear()
            _set_setup(phase="needs_consent", size=size,
                       detail=f"{chosen['name']} needs downloading first.")
            await _consent.wait()
            _set_setup(phase="downloading", progress=0.0, detail="Starting…",
                       size=size)
            loop = asyncio.get_running_loop()

            def report(what: str, done: int, total: int) -> None:
                frac = 0.0 if total <= 0 else min(1.0, done / total)
                loop.call_soon_threadsafe(partial(
                    _set_setup, phase="downloading", progress=frac, size=size,
                    detail=f"Downloading the {what} — "
                           f"{model_setup.human(done)} of "
                           f"{model_setup.human(total)}"))

            await asyncio.to_thread(model_setup.download, report)

        # Old weights out before new weights in.
        _set_setup(phase="loading", detail=f"Unloading {config.model_info(was)['name']}",
                   loaded="")
        old, _llm = _llm, None
        await asyncio.to_thread(old.unload)
        del old

        _set_setup(phase="loading",
                   detail=f"Loading {chosen['detail'] or chosen['name']}",
                   loaded="")
        _llm = await asyncio.to_thread(LLM, pick)
        _llm.tools = tool_list(_feature_settings())
        _set_setup(phase="loading", detail="Warming up — compiling GPU kernels")
        await asyncio.to_thread(_llm.warmup)
        _system, _system_day = _build_system(), date.today()
        _set_setup(phase="loading", detail="Preparing the conversation…")
        await asyncio.to_thread(_llm.prime, _system)

        # Every open conversation is still pointing at the model that just left.
        for sess in list(_sessions):
            sess.rebind_llm(_llm)
    except Exception as exc:
        traceback.print_exc()
        _set_setup(phase="failed",
                   detail=f"Couldn't switch model: {exc}. Reopen Fennel to try "
                          "again.")
        return

    _set_setup(phase="ready")
    print(f"Fennel ready ({chosen['name']}).", flush=True)


async def _prepare_models() -> None:
    """Choose, consent, download, load, warm, prime — reporting each step."""
    global _stt, _llm, _tts, _memory, _system, _system_day

    # Which model, before anything else: the download list, the prime-cache key
    # and the settings panel all read config.LLM_MODEL, so it is settled first
    # and only then does anything look at the disk or the network.
    stored = _store.setting("llm_model", "") or config.DEFAULT_MODEL
    _set_setup(phase="choose_model", models=model_setup.catalogue(),
               current=stored, note="")
    await _model_chosen.wait()
    config.LLM_MODEL = _chosen_model or stored
    _store.set_setting("llm_model", config.LLM_MODEL)
    chosen = config.model_info(config.LLM_MODEL)
    print(f"[setup] model: {chosen['name']} ({config.LLM_MODEL})", flush=True)

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
    # Per model: a 3B and a 14B are a minute apart, so one shared estimate was
    # wrong for both. First run of a given model has no history and gets a
    # rough guess scaled by its size.
    eta_key = f"startup_seconds:{config.LLM_MODEL}"
    eta = float(_store.setting(eta_key, "") or max(20.0, chosen["ram"] * 6))
    started = time.monotonic()
    # Weights go into unified memory one model at a time; naming each with its
    # size is more informative than a spinner, and explains where the wait goes.
    llm_gb = chosen["bytes"] / 1e9 or 2.3
    steps = [
        (f"{chosen['detail'] or chosen['name']} — the conversation", llm_gb),
        ("Kokoro — the voice", 0.6),
        ("Whisper — speech recognition", 0.5),
        ("bge-small — memory and recall", 0.1),
    ]
    total_gb = sum(g for _, g in steps)

    def loading(i: int) -> None:
        name, size = steps[i]
        done = sum(g for _, g in steps[:i])
        _set_setup(phase="loading", eta=max(1.0, eta - (time.monotonic() - started)),
                   detail=f"Loading {name} — {size:.1f} GB into memory",
                   loaded=f"{done:.1f} GB of {total_gb:.1f} GB in memory")

    loading(0)
    print("loading models "
          f"({config.LLM_MODEL.split('/')[-1]}, "
          f"{config.STT_MODEL.split('/')[-1]}, "
          f"{config.TTS_MODEL.split('/')[-1]}) …", flush=True)
    _llm = await asyncio.to_thread(LLM, config.LLM_MODEL)
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
               loaded=f"{total_gb:.1f} GB in memory",
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
               loaded=f"{total_gb:.1f} GB in memory",
               eta=max(1.0, eta - (time.monotonic() - started)))
    await asyncio.to_thread(_llm.prime, _system)

    _store.set_setting(eta_key, f"{time.monotonic() - started:.0f}")
    _set_setup(phase="ready")
    print("Fennel ready.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
