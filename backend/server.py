"""Local WebSocket server. Stage 0: echoes user text back as a streamed reply
so the Swift client can prove the streaming-chat contract before MLX exists.

Stage 1 replaces the echo in `_handle_user_text` with real mlx-lm streaming
from `her/llm.py`. Nothing else here should need to change.
"""
from __future__ import annotations

import asyncio

import websockets

import config
import protocol as P

_turn = 0


async def _handle_user_text(ws, text: str) -> None:
    """Stage 0 stand-in for the LLM: stream the text back word by word."""
    global _turn
    _turn += 1
    turn = _turn

    await ws.send(P.encode("state", value="thinking"))
    reply = f"(echo) {text}"
    for word in reply.split(" "):
        await ws.send(P.encode("token", turn=turn, text=word + " "))
        await asyncio.sleep(0.02)  # simulate token cadence
    await ws.send(P.encode("turn_end", turn=turn))
    await ws.send(P.encode("state", value="idle"))


async def handler(ws) -> None:
    await ws.send(P.encode("state", value="idle"))
    async for raw in ws:
        if isinstance(raw, bytes):
            continue  # audio frames arrive in Stage 2
        msg = P.decode(raw)
        if msg["type"] == "user_text":
            await _handle_user_text(ws, msg.get("text", ""))
        elif msg["type"] == "ping":
            await ws.send(P.encode("pong"))


async def main() -> None:
    print(f"my_ai backend listening on ws://{config.HOST}:{config.PORT}  (tier={config.TIER})")
    async with websockets.serve(handler, config.HOST, config.PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
