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


def _weights_on_disk() -> dict[str, int]:
    """repo_id -> bytes of weights actually present.

    Presence in the cache index is not enough: fetching just a repo's config to
    inspect its chat template registers the repo with no weights behind it, and
    a picker built on that would offer to open a model it would then have to
    download. So a repo counts as installed only once real .safetensors files
    are there — and the size reported is what those files actually occupy.
    """
    out: dict[str, int] = {}
    try:
        cache = scan_cache_dir()
    except Exception:
        return out
    for repo in cache.repos:
        total = sum(f.size_on_disk
                    for rev in repo.revisions for f in rev.files
                    if f.file_name.endswith(".safetensors"))
        if total:
            out[repo.repo_id] = total
    return out


def installed(repo: str) -> bool:
    """Whether `repo` is genuinely usable offline, weights and all."""
    return repo in _weights_on_disk()


def catalogue() -> list[dict]:
    """The registry, annotated with what is on disk. Everything the startup
    picker needs, resolved here so the app holds no model knowledge of its own."""
    have = _weights_on_disk()
    return [dict({"hidden": False}, **m, installed=m["id"] in have,
                 on_disk=have.get(m["id"], 0)) for m in config.MODELS]


def delete(repo: str, in_use: Optional[str] = None) -> int:
    """Remove a downloaded model from the hub cache. Returns bytes freed.

    Refuses to touch anything outside the model registry, and refuses to delete
    `in_use` — a picker that can delete the model it is about to load is one
    that can brick the next launch. Before anything is loaded there is no such
    model, so during the startup picker every row is fair game.
    """
    if repo not in {m["id"] for m in config.MODELS}:
        raise ValueError(f"not a known model: {repo}")
    if in_use and repo == in_use:
        raise ValueError("that model is the one in use")
    try:
        cache = scan_cache_dir()
    except Exception as exc:
        raise RuntimeError(f"couldn't read the model cache: {exc}") from exc
    hashes = [r.commit_hash for c in cache.repos if c.repo_id == repo
              for r in c.revisions]
    if not hashes:
        return 0
    freed = sum(c.size_on_disk for c in cache.repos if c.repo_id == repo)
    cache.delete_revisions(*hashes).execute()
    print(f"[setup] deleted {repo} ({human(freed)})", flush=True)
    return freed


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
    label = f"{config.model_info(config.LLM_MODEL)['name']} model"
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
            label = {
                "llm": f"{config.model_info(config.LLM_MODEL)['name']} model",
                "stt": "speech recognition", "tts": "voice",
                "embed": "memory"}.get(key, key)
            report_now()
            snapshot_download(repo)
    finally:
        stop.set()
        watcher.join(timeout=2)
    progress("done", grand, grand)
