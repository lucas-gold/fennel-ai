"""Wire protocol between the Swift app and the Python backend.

Two channels over one local WebSocket on 127.0.0.1:

  - Control frames: JSON text, listed below.
  - Audio frames:   binary. Mic in is 16 kHz int16 PCM in 512-sample frames;
                    audio out is a ">II" header (turn, seq) then 24 kHz int16.

Both sides must agree on these shapes; Protocol.swift mirrors this file.
"""
from __future__ import annotations

import json
import struct
from typing import Any

import numpy as np

_AUDIO_HEADER = struct.Struct(">II")

# ── client → server ────────────────────────────────────────────────────────
# {"type": "user_text", "text": "...", "speak": bool}
# {"type": "tool_result", "id": "...", "ok": bool, "error": "...", "data": {...}}
#     The app has finished (or failed) the real side effect for this tool call.
#     The backend waits briefly so the spoken confirmation matches what
#     happened. A read-style tool such as agenda returns its answer in "data".
# {"type": "settings", "daily_updates": bool, "location": "...",
#           "lookups": bool, "web_key": "..."}
# {"type": "setup_consent"}                allow the first model download
# {"type": "model_select", "id": "..."}    load this model
# {"type": "model_reopen"}                 show the picker again
# {"type": "model_cancel"}                 abandon a load in progress
# {"type": "model_unload"}                 free the resident model
# {"type": "model_delete", "id": "..."}    delete a model's weights
# {"type": "model_probe",  "id": "..."}    inspect a repo without downloading
# {"type": "model_add",    "id": "..."}    keep a probed repo in the picker
# {"type": "image_toggle", "enabled": bool}
# {"type": "image_delete"}                 delete the image model's weights
# {"type": "card_cancel", "id": "..."}     stop a picture being drawn
# {"type": "card_forget", "id": "..."}     drop a dismissed card for good
# {"type": "session_list" | "session_new"}
# {"type": "session_open" | "session_delete", "id": N}
# {"type": "ping"}
#
# ── server → client ────────────────────────────────────────────────────────
# {"type": "state", "value": "idle|listening|thinking|speaking"}
# {"type": "stt", "text": "..."}                what the mic was heard to say
# {"type": "token", "turn": N, "text": "..."}   one chunk of the reply
# {"type": "split", "turn": N}                  end this bubble; a tool follows
# {"type": "turn_end", "turn": N}               reply complete
# {"type": "busy", "text": "..."}               a turn arrived mid-generation
# {"type": "cancel"}                            drop any audio still queued
# {"type": "tool", "id": "...", "name": "...", "args": {...}}
#     Raises a card and asks the app to perform the side effect. `args` is
#     normalized, with absolute ISO times.
# {"type": "card_update", "id": "...", "status": "...", ...}
#     Progress or outcome for a card already on screen.
# {"type": "setup", "phase": "...", ...}
#     Startup and the model picker: checking, choose_model, needs_consent,
#     downloading, loading, ready, failed.
# {"type": "memory", "llm_bytes": N, "system_used_bytes": N, ...}
# {"type": "settings", ...}                     mirrors the client frame
# {"type": "model_probe", "ok": bool, "problems": [...], "warnings": [...]}
# {"type": "sessions", "items": [{id,title,updated,count}]}
# {"type": "session_opened", "id": N, "messages": [...], "cards": [...]}
# {"type": "pong"}


def encode(type_: str, **fields: Any) -> str:
    return json.dumps({"type": type_, **fields})


def decode(raw: str) -> dict[str, Any]:
    msg = json.loads(raw)
    if "type" not in msg:
        raise ValueError(f"control frame missing 'type': {raw!r}")
    return msg


def pack_audio(turn: int, seq: int, pcm_int16: np.ndarray) -> bytes:
    """Header + int16 PCM."""
    return _AUDIO_HEADER.pack(turn, seq) + pcm_int16.astype("<i2").tobytes()


def unpack_audio(data: bytes) -> tuple[int, int, np.ndarray]:
    turn, seq = _AUDIO_HEADER.unpack_from(data, 0)
    pcm = np.frombuffer(data, dtype="<i2", offset=_AUDIO_HEADER.size)
    return turn, seq, pcm
