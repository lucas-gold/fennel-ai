"""What Fennel and the Mac are actually holding.

RSS is a poor answer to "how much is the model using": MLX maps weight files,
so pages count against the process only once touched, and a freshly loaded 7 GB
model can report 3 GB. MLX knows exactly what it allocated, so ask it — and ask
the kernel separately for the machine-wide figure, which is the number that
decides whether the next model will fit.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

import mlx.core as mx

_PAGE = 4096
_TOTAL = 0
_last: tuple[float, dict] = (0.0, {})


def _page_size_and_total() -> tuple[int, int]:
    global _PAGE, _TOTAL
    if _TOTAL:
        return _PAGE, _TOTAL
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize", "hw.pagesize"],
                                      text=True).split()
        _TOTAL, _PAGE = int(out[0]), int(out[1])
    except Exception:
        _TOTAL, _PAGE = 16 * 1024**3, 16384
    return _PAGE, _TOTAL


def _system_used() -> tuple[int, int]:
    """(used, total) bytes, counting what macOS counts under Memory Used:
    resident app pages, wired kernel pages and the compressor."""
    page, total = _page_size_and_total()
    try:
        out = subprocess.check_output(["vm_stat"], text=True)
    except Exception:
        return 0, total
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().rstrip(".")
        if v.isdigit():
            counts[k.strip()] = int(v)
    used_pages = (counts.get("Pages active", 0)
                  + counts.get("Pages wired down", 0)
                  + counts.get("Pages occupied by compressor", 0))
    return used_pages * page, total


def _rss() -> int:
    """This process's resident size. One fork per sample, so it is rate limited
    by the caller rather than polled tightly."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip()
        return int(out) * 1024
    except Exception:
        return 0


def snapshot(min_interval: float = 1.5) -> dict:
    """Current memory picture, cached briefly so a chatty caller cannot turn
    this into a fork bomb.

    `model` is what MLX holds: live tensors plus its reusable buffer pool. That
    is the figure worth showing next to a model name, because it is the one that
    changes when you switch.
    """
    global _last
    now = time.monotonic()
    if _last[1] and now - _last[0] < min_interval:
        return _last[1]
    used, total = _system_used()
    try:
        active, cache = mx.get_active_memory(), mx.get_cache_memory()
    except Exception:
        active = cache = 0
    snap = {
        "model_bytes": active + cache,
        "model_active_bytes": active,
        "process_bytes": _rss(),
        "system_used_bytes": used,
        "system_total_bytes": total,
    }
    _last = (now, snap)
    return snap


def human(n: Optional[int]) -> str:
    if not n:
        return "—"
    return f"{n / 1024**3:.1f} GB"
