"""What Fennel and the Mac are holding.

RSS understates a model: MLX maps its weights, so pages only count against the
process once touched. MLX knows what it allocated, so ask it, and ask the kernel
separately for the machine-wide figure.
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


def _rss_of(pid: int) -> int:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)], text=True).strip()
        return int(out) * 1024
    except Exception:
        return 0


def _children_rss() -> int:
    """Resident size of everything this process spawned.

    Image generation runs in a subprocess, so its cost is not in our own RSS.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=,ppid=", "-A"], text=True)
    except Exception:
        return 0
    me = os.getpid()
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) == me:
            total += int(parts[0]) * 1024
    return total


def _child_cost(used_now: int) -> int:
    """What the drawing subprocess is costing.

    Its RSS is a floor. Take the machine's own rise since it started when that
    is larger — the figure Activity Monitor shows.
    """
    rss = _children_rss()
    if _child_baseline is None:
        return rss
    return max(rss, used_now - _child_baseline)


def _app_rss() -> int:
    """The SwiftUI app, a separate process, via FENNEL_PARENT_PID.

    Zero when the backend is run from a terminal with no app attached.
    """
    parent = os.environ.get("FENNEL_PARENT_PID", "")
    return _rss_of(int(parent)) if parent.isdigit() else 0


def _rss() -> int:
    """This process's resident size. One fork per sample, so callers rate
    limit it rather than polling tightly."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip()
        return int(out) * 1024
    except Exception:
        return 0


def mlx_active() -> int:
    """Live MLX tensors: the weights and caches in use.

    Excludes the reusable buffer pool, which is scratch space MLX hands back on
    demand — counting it overstates both the model and the memory in use.
    """
    try:
        return mx.get_active_memory()
    except Exception:
        return 0


def mlx_bytes() -> int:
    """Everything MLX holds, pool included."""
    try:
        return mx.get_active_memory() + mx.get_cache_memory()
    except Exception:
        return 0


#: System memory in use when a subprocess was launched, so its cost can be read
#: from the machine rather than its RSS, which understates MLX allocations.
_child_baseline: Optional[int] = None


def mark_child_start() -> None:
    global _child_baseline
    _child_baseline = _system_used()[0]


def mark_child_end() -> None:
    global _child_baseline
    _child_baseline = None


def snapshot(min_interval: float = 1.5) -> dict:
    """Current memory picture, cached briefly since each call forks `ps`."""
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
        "mlx_bytes": active + cache,
        "mlx_active_bytes": active,
        "process_bytes": _rss(),
        "app_bytes": _app_rss(),
        "child_bytes": _child_cost(used),
        "system_used_bytes": used,
        "system_total_bytes": total,
    }
    _last = (now, snap)
    return snap


def human(n: Optional[int]) -> str:
    if not n:
        return "—"
    return f"{n / 1024**3:.1f} GB"
