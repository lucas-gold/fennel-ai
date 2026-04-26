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
from voice.llm import LLM
from voice.session import Session
from voice.stt import WhisperSTT
from voice.tools import system_prompt
from voice.tts import KokoroTTS

_stt: WhisperSTT
_llm: LLM
_tts: KokoroTTS
_system: str
_system_day: date


def _current_system() -> str:
    """The primed system prefix, re-primed if the app has been left running
    past midnight (the prompt's day table would otherwise be wrong)."""
    global _system, _system_day
    if date.today() != _system_day:
        _system, _system_day = system_prompt(config.LLM_SYSTEM), date.today()
        _llm.prime(_system)
    return _system


async def handler(ws) -> None:
    async def send_control(s: str) -> None:
        await ws.send(s)

    async def send_audio(b: bytes) -> None:
        await ws.send(b)

    session = Session(send_control, send_audio, _stt, _llm, _tts,
                      system=_current_system())
    await ws.send(P.encode("state", value="idle"))
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
                elif msg["type"] == "ping":
                    await ws.send(P.encode("pong"))
    finally:
        await session.close()


async def main() -> None:
    global _stt, _llm, _tts, _system, _system_day
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
    # Prefill the tool schemas + day table now rather than during the user's
    # first sentence — it is ~640 tokens of stable prefix (D4).
    _system, _system_day = system_prompt(config.LLM_SYSTEM), date.today()
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
