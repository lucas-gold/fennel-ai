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
