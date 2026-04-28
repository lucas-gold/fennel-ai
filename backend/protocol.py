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
import struct
from typing import Any

import numpy as np

# Audio-out frame: ">II" header (turn, seq) + int16 PCM @24 kHz.
_AUDIO_HEADER = struct.Struct(">II")

# ── client → server ────────────────────────────────────────────────────────
# {"type": "user_text", "text": "...", "speak": bool}   user typed a message
# {"type": "tool_result", "id": "...", "ok": bool, "error": "..."}
#     Stage 3: the app finished (or failed) the real EventKit write for the
#     tool call with this id. The backend waits briefly for this so the spoken
#     confirmation matches reality.
# {"type": "session_list"}                 ask for the chat list
# {"type": "session_open", "id": N}        resume a saved chat
# {"type": "session_new"}                  start a fresh chat
# {"type": "session_delete", "id": N}      delete a chat and its messages
# {"type": "ping"}                         liveness check
#
# ── server → client ────────────────────────────────────────────────────────
# {"type": "state", "value": "idle|listening|thinking|speaking"}
# {"type": "stt", "text": "..."}                what the mic was heard to say
# {"type": "token", "turn": N, "text": "..."}   one streamed LLM token/chunk
# {"type": "turn_end", "turn": N}               reply complete
# {"type": "tool", "id": "...", "name": "...", "args": {...}}
#     Stage 3: drives the home UI. `name` is one of set_reminder / add_event /
#     show_panel / set_fact; `args` is already normalized (absolute ISO times).
#     The app renders a card, performs the side effect, and replies tool_result.
#     A read-style tool (agenda) gets its answer back via tool_result "data".
# {"type": "sessions", "items": [{id,title,updated,count}], "current": N}
# {"type": "session_opened", "id": N, "messages": [{"role","text"}]}
# {"type": "pong"}


def encode(type_: str, **fields: Any) -> str:
    return json.dumps({"type": type_, **fields})


def decode(raw: str) -> dict[str, Any]:
    msg = json.loads(raw)
    if "type" not in msg:
        raise ValueError(f"control frame missing 'type': {raw!r}")
    return msg


def pack_audio(turn: int, seq: int, pcm_int16: np.ndarray) -> bytes:
    """Audio-out frame: header + int16 PCM. The Swift client mirrors this."""
    return _AUDIO_HEADER.pack(turn, seq) + pcm_int16.astype("<i2").tobytes()


def unpack_audio(data: bytes) -> tuple[int, int, np.ndarray]:
    turn, seq = _AUDIO_HEADER.unpack_from(data, 0)
    pcm = np.frombuffer(data, dtype="<i2", offset=_AUDIO_HEADER.size)
    return turn, seq, pcm
