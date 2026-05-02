"""Local WebSocket server.

Stage 2: full voice loop through `voice/session.py`. Text control frames carry
state/tokens/tool calls; binary frames carry audio — mic in (int16 mono 16 kHz,
512-sample frames), audio out (">II" header + int16 PCM @24 kHz). The typed
chat box still works and now also speaks its reply.
"""
from __future__ import annotations

import asyncio
from datetime import date

import numpy as np
import websockets

import config
import protocol as P
from voice import embed
from voice.briefing import Briefing, Retriever
from voice.llm import LLM
from voice.memory import Memory
from voice.session import Session
from voice.store import Store
from voice.stt import WhisperSTT
from voice.tools import system_prompt
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


async def _refresh_briefing(session: Session) -> None:
    """Fetch today's briefing and fold it into the primed prefix.

    Runs in the background on the first connection of the day: the network is
    slow and unreliable, and a voice assistant must never wait on it. Until it
    lands, the model simply runs without a briefing.
    """
    global _system, _system_day
    if _briefing.is_stale():
        await asyncio.to_thread(_briefing.build, embed.shared())
    # Re-prime whenever the prefix we *want* differs from the one that's primed,
    # not merely when the fetch was stale: toggling "daily updates" on mid-run
    # leaves a perfectly fresh briefing sitting outside the prefix otherwise.
    want = _build_system()
    if want == _system:
        return
    _system, _system_day = want, date.today()
    # Hand the session the new prefix BEFORE priming. A turn arriving during the
    # prime blocks on the LLM lock either way, but this way it wakes up using the
    # new prefix on a warm cache rather than the stale one.
    await session.apply_system(_system)
    await asyncio.to_thread(_llm.prime, _system)
    print("[briefing] folded into the primed prefix", flush=True)


async def handler(ws) -> None:
    async def send_control(s: str) -> None:
        await ws.send(s)

    async def send_audio(b: bytes) -> None:
        await ws.send(b)

    session = Session(send_control, send_audio, _stt, _llm, _tts,
                      _store, _memory, system=_current_system())
    await ws.send(P.encode("state", value="idle"))
    # Push stored settings immediately. Without this the app boots showing its
    # own defaults (all off) while the backend has them on — and the next Save
    # writes those stale defaults back over the real ones.
    await ws.send(P.encode("settings", daily_updates=_briefing.enabled,
                           location=_briefing.place,
                           web_search=_store.setting("web_search", "0") == "1"))
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
                        web_search=_store.setting("web_search", "0") == "1"))
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
        await session.close()


async def main() -> None:
    global _stt, _llm, _tts, _store, _memory, _briefing, _system, _system_day
    _store = Store()
    _briefing = Briefing(_store)
    # Embeddings power both news retrieval and conversational recall; loading is
    # lazy and optional, so a failure degrades to keyword search (embed.shared).
    _emb = embed.shared()
    _memory = Memory(_store, Retriever(_store, _emb), _emb)
    print("loading models "
          f"({config.LLM_MODEL.split('/')[-1]}, "
          f"{config.STT_MODEL.split('/')[-1]}, "
          f"{config.TTS_MODEL.split('/')[-1]}) …", flush=True)
    _llm = await asyncio.to_thread(LLM)
    _tts = await asyncio.to_thread(KokoroTTS)
    _stt = WhisperSTT()
    print("warming models …", flush=True)  # move cold-start cost off turn 1
    await asyncio.to_thread(_stt.warmup)
    await asyncio.to_thread(_tts.warmup)
    await asyncio.to_thread(_llm.warmup)
    # Fetch today's briefing here, before we start listening, so the common case
    # (backend and app started together) never races the prime against a turn.
    # The on-connect refresh then only fires when the day rolls over mid-run.
    if _briefing.is_stale():
        await asyncio.to_thread(_briefing.build, embed.shared())
    # Prefill tool schemas + day table + briefing now rather than during the
    # user's first sentence — ~1600-2200 tokens of stable prefix (D4).
    _system, _system_day = _build_system(), date.today()
    await asyncio.to_thread(_llm.prime, _system)
    print(f"my_ai backend listening on ws://{config.HOST}:{config.PORT}  "
          f"(tier={config.TIER})", flush=True)
    async with websockets.serve(handler, config.HOST, config.PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
