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
