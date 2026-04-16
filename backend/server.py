"""Local WebSocket server.

Stage 1: real mlx-lm streaming from `her/llm.py`. One LLM instance holds the
KV cache; a single local user talks to it at a time, so per-connection we reset
the conversation. Audio (VAD/STT/TTS) and the session orchestrator arrive in
Stage 2.
"""
from __future__ import annotations

import asyncio

import websockets

import config
import protocol as P
from her.llm import LLM

_llm: LLM | None = None
_turn = 0


async def _handle_user_text(ws, messages: list[dict], text: str) -> None:
    global _turn
    assert _llm is not None
    _turn += 1
    turn = _turn

    messages.append({"role": "user", "content": text})
    await ws.send(P.encode("state", value="thinking"))

    reply = ""
    async for chunk in _llm.astream(messages):
        reply += chunk
        await ws.send(P.encode("token", turn=turn, text=chunk))

    messages.append({"role": "assistant", "content": reply})
    await ws.send(P.encode("turn_end", turn=turn))
    await ws.send(P.encode("state", value="idle"))


async def handler(ws) -> None:
    assert _llm is not None
    _llm.reset()
    messages: list[dict] = [{"role": "system", "content": config.LLM_SYSTEM}]
    await ws.send(P.encode("state", value="idle"))
    async for raw in ws:
        if isinstance(raw, bytes):
            continue  # audio frames arrive in Stage 2
        msg = P.decode(raw)
        if msg["type"] == "user_text":
            await _handle_user_text(ws, messages, msg.get("text", ""))
        elif msg["type"] == "ping":
            await ws.send(P.encode("pong"))


async def main() -> None:
    global _llm
    print(f"loading LLM {config.LLM_MODEL} …", flush=True)
    _llm = await asyncio.to_thread(LLM)
    print(f"my_ai backend listening on ws://{config.HOST}:{config.PORT}  "
          f"(tier={config.TIER})", flush=True)
    async with websockets.serve(handler, config.HOST, config.PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
