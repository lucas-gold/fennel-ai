"""Wire protocol between the Swift app and the Python backend.

Two channels over one local WebSocket (offline, 127.0.0.1):

  - Control frames: JSON text messages, shapes below.
  - Audio frames:   binary. Mic in = 16 kHz int16 PCM, 512-sample frames.
                    Audio out = b">II" header (turn, seq) + 24 kHz int16 PCM.
                    (Audio is Stage 2; only control frames exist in Stage 0.)

Keep this file the single source of truth for message shapes. Both sides
must agree; the Swift `Protocol.swift` mirrors it.
"""
from __future__ import annotations

import json
from typing import Any

# ── client → server ────────────────────────────────────────────────────────
# {"type": "user_text", "text": "..."}   user typed a message
# {"type": "ping"}                         liveness check
#
# ── server → client ────────────────────────────────────────────────────────
# {"type": "state", "value": "idle|listening|thinking|speaking"}
# {"type": "token", "turn": N, "text": "..."}   one streamed LLM token/chunk
# {"type": "turn_end", "turn": N}               reply complete
# {"type": "tool", "name": "...", "args": {...}} Stage 3: drives home UI
# {"type": "pong"}


def encode(type_: str, **fields: Any) -> str:
    return json.dumps({"type": type_, **fields})


def decode(raw: str) -> dict[str, Any]:
    msg = json.loads(raw)
    if "type" not in msg:
        raise ValueError(f"control frame missing 'type': {raw!r}")
    return msg
