"""Every tunable knob. RAM-based tiering affects KV depth only (reference D11)."""
from __future__ import annotations

import os
import subprocess

HOST = "127.0.0.1"
PORT = 8420


def _total_ram_gb() -> float:
    """Physical RAM in GB. macOS: `sysctl hw.memsize`, with a safe fallback."""
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
        return int(out.strip()) / (1024**3)
    except Exception:
        return 16.0  # assume a comfortable default rather than crash


def select_tier() -> str:
    """'small' or 'large'. Force with HER_TIER. Tier sets KV growth only."""
    forced = os.environ.get("HER_TIER")
    if forced in {"small", "large"}:
        return forced
    return "large" if _total_ram_gb() >= 12 else "small"


TIER = select_tier()

# Only KV-cache growth differs by tier (reference D11); models are identical.
VERBATIM_TURNS = 8 if TIER == "large" else 4
MAX_TOKENS = 1024 if TIER == "large" else 512

# ── LLM (Stage 1) ──────────────────────────────────────────────────────────
# Verified real repo (the design's "Qwen3.5-4B-VL" was an unverified guess).
# Text-only for now; swap to a VL variant at the video phase — a config change,
# per docs/DECISIONS.md D9.
LLM_MODEL = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
LLM_SYSTEM = (
    "You are a warm, concise companion. Keep replies short, natural, and easy "
    "to speak aloud."
)
LLM_MAX_TOKENS = MAX_TOKENS

# ── STT / TTS (Stage 2) ────────────────────────────────────────────────────
# small.en ~300ms vs turbo's ~2.2s here (D9 revisited: turbo's accuracy edge
# wasn't worth 7x the latency on clean English). base.en (~90ms) if you want
# it even snappier and can accept a bit more error.
STT_MODEL = "mlx-community/whisper-small.en-mlx"
TTS_MODEL = "mlx-community/Kokoro-82M-4bit"
TTS_VOICE = "af_heart"  # check per-voice CC-BY before shipping (SHIPPING.md)
TTS_SPEED = 1.15        # Kokoro speed; >1 speaks faster

# Clause splitter (D5): greedy first fragment, longer later chunks.
CLAUSE_FIRST_CHARS = 18   # smaller = first audio starts sooner
CLAUSE_REST_CHARS = 90

# ── VAD / endpointing (Stage 2) ────────────────────────────────────────────
# Latency hides in turn-taking, not the models — tune END_SILENCE_MS first (D2).
# Absolute so it resolves no matter the working directory the server is launched from.
VAD_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "silero_vad.onnx")
FRAME_SAMPLES = 512      # 32 ms @16 kHz — the Silero v5 window (== client frame)
VAD_THRESHOLD = 0.5
END_SILENCE_MS = 350     # silence before "you're done"; lower = snappier, too low cuts you off
MIN_SPEECH_MS = 200      # ignore blips shorter than this
PREROLL_FRAMES = 5       # ~160 ms kept before onset so the first word isn't clipped
