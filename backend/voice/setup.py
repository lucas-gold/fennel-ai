"""First-run model download: ask first, then show what's happening.

Fennel's promise is that it runs on your machine, so the one moment it needs the
network is the moment that most deserves a prompt. Nothing here touches the
network until `download` is called, and `download` is only called after the app
reports the user pressed the button — including the size estimate below, which
is hardcoded rather than queried so that even the *estimate* costs no request.

Progress is measured from the cache directory on disk, the only measure that
survives huggingface_hub swapping download backends.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from huggingface_hub import scan_cache_dir, snapshot_download

import config

# Approximate download sizes, in bytes, for the consent screen. Rounded from the
# real repos; being a little over is kinder than being under.
_SIZES = {
    "llm": 2_300_000_000,
    "stt": 500_000_000,
    "tts": 650_000_000,
    "embed": 135_000_000,
}

Progress = Callable[[str, int, int], None]   # (what, done_bytes, total_bytes)


def _repos() -> list[tuple[str, str, int]]:
    """(key, repo_id, approx_bytes) for everything Fennel needs to run."""
    return [
        ("llm", config.LLM_MODEL, _SIZES["llm"]),
        ("stt", config.STT_MODEL, _SIZES["stt"]),
        ("tts", config.TTS_MODEL, _SIZES["tts"]),
        ("embed", config.EMBED_MODEL, _SIZES["embed"]),
    ]


def _cached_repo_ids() -> set[str]:
    try:
        return {r.repo_id for r in scan_cache_dir().repos}
    except Exception:
        return set()


def missing() -> list[tuple[str, str, int]]:
    """Which models still need downloading. Empty means we can start offline."""
    have = _cached_repo_ids()
    return [(k, r, n) for k, r, n in _repos() if r not in have]


def total_bytes(items: Optional[list] = None) -> int:
    return sum(n for _, _, n in (items if items is not None else missing()))


def human(n: int) -> str:
    return f"{n / 1_000_000_000:.1f} GB" if n >= 1_000_000_000 else f"{n // 1_000_000} MB"


def _cache_bytes() -> int:
    """Bytes of actual model data on disk.

    Only the `hub` subtree: huggingface_hub also keeps a transient xet chunk
    cache alongside it, and counting that reported 206 MB of progress for a
    133 MB model. Walking the tree is cheap next to a multi-gigabyte download.
    """
    root = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    root = os.path.join(root, "hub")
    total = 0
    for dirpath, _, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def download(progress: Progress) -> None:
    """Fetch every missing model, reporting bytes as they land.

    Progress is measured from the cache directory rather than huggingface_hub's
    tqdm hooks: its xet backend runs several bars at once ("Downloading bytes",
    "Reconstructing"), so summing their deltas reported 267 MB of progress for a
    133 MB model and pinned the bar at 100%.

    Raises on failure so the caller can surface it — a half-downloaded model
    that fails later at load time is a much worse experience than a clear
    "couldn't download" while the user is still watching the progress bar.
    """
    items = missing()
    grand = total_bytes(items)
    baseline = _cache_bytes()
    label = "language model"
    stop = threading.Event()

    def report_now() -> None:
        # Clamped: the size estimates are deliberately approximate, and a bar
        # reading "206 MB of 135 MB" looks broken even when all is well.
        progress(label, min(grand, max(0, _cache_bytes() - baseline)), grand)

    def watch() -> None:
        while not stop.wait(1.0):
            report_now()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        for key, repo, _approx in items:
            label = {"llm": "language model", "stt": "speech recognition",
                     "tts": "voice", "embed": "memory"}.get(key, key)
            report_now()
            snapshot_download(repo)
    finally:
        stop.set()
        watcher.join(timeout=2)
    progress("done", grand, grand)
