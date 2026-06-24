"""Image generation through mflux (FLUX.2 Klein 4B, 4-bit).

Run as a subprocess, deliberately. Diffusion peaks at 8–12 GB — more than the
language model — and doing that in-process would mean two MLX contexts fighting
over unified memory on the one thread the LLM is pinned to. A subprocess gets
its own allocator, and every byte is handed back when it exits, which is the
only way to be sure the conversation is not left slower afterwards.

The model is an ungated mirror on purpose: black-forest-labs/FLUX.1-schnell is
gated behind a Hugging Face login, and Fennel's whole install story is that
opening it is enough.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

import config
from voice import sysmem
from voice.store import APP_DIR

MODEL_REPO = "Runpod/FLUX.2-klein-4B-mflux-4bit"
BASE_MODEL = "flux2-klein-4b"
MODEL_BYTES = 4_620_000_000

#: What generation actually costs the machine, measured on an M2 as the rise in
#: memory in use while it runs.
#:
#: Not the figure mflux prints. It reports "Peak MLX memory: 7.96 GB" for the
#: same run that costs 3.1 GB of real memory — that number counts transient
#: allocations which never coexist as resident pages. Sizing the decision below
#: on it made Fennel unload the language model when there was ample room.
PEAK_LOWRAM_BYTES = 3_200_000_000
#: Full size is not separately measured; scaled by the ratio mflux reports
#: between the two (12.4 / 7.96), which is the best evidence available.
PEAK_FULL_BYTES = 5_000_000_000

_STEP = re.compile(r"(\d+)/(\d+)\s*\[")

#: Running renders, by card id, so dismissing a card can stop the work rather
#: than leaving a minute of computation running for a picture nobody wants.
_procs: dict = {}
#: Cards dismissed before their render began. A queued picture has no process
#: to kill, so cancelling it has to be remembered until its turn comes round —
#: otherwise dismissing the second of two just delayed it by a minute.
_cancelled: set = set()
_procs_lock = threading.Lock()


def cancel(token: str) -> bool:
    """Stop a render, whether it is running or still waiting its turn."""
    with _procs_lock:
        _cancelled.add(token)
        proc = _procs.get(token)
    if proc is not None and proc.poll() is None:
        proc.kill()
        print(f"[image] cancelled {token} (was running)", flush=True)
        return True
    print(f"[image] cancelled {token} (before it started)", flush=True)
    return False
Progress = Callable[[str, float], None]        # (detail, 0..1)


class Cancelled(Exception):
    """The card was dismissed while its picture was being drawn."""


def images_dir() -> str:
    """Where finished pictures live until the user wants one.

    The app's own folder, not Downloads: every picture landing in Downloads
    uninvited is clutter, and most of them are a look rather than a keeper. The
    card has a download button for the ones worth keeping.
    """
    path = os.path.join(APP_DIR, "images")
    os.makedirs(path, exist_ok=True)
    return path


def filename_for(title: str, card_id: str) -> str:
    """A name that says what it is, without colliding with anything."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "picture"
    return f"fennel-{slug}-{card_id}.png"


def installed() -> bool:
    """Whether the diffusion weights are already downloaded."""
    from voice.setup import _weights_on_disk
    return MODEL_REPO in _weights_on_disk()


def download(progress: Optional[Callable[[int, int], None]] = None) -> None:
    """Fetch the diffusion weights.

    Called when the model is chosen, not when the first picture is asked for:
    a minute's wait for a picture is expected, four gigabytes of download in the
    middle of it is not.
    """
    import threading as _t
    from huggingface_hub import snapshot_download
    from voice.setup import _cache_bytes

    stop = _t.Event()
    baseline = _cache_bytes()

    def watch() -> None:
        while not stop.wait(1.0):
            if progress:
                progress(max(0, _cache_bytes() - baseline), MODEL_BYTES)

    w = _t.Thread(target=watch, daemon=True)
    w.start()
    try:
        snapshot_download(MODEL_REPO)
    finally:
        stop.set()
        w.join(timeout=2)
    if progress:
        progress(MODEL_BYTES, MODEL_BYTES)
    print(f"[image] model downloaded: {MODEL_REPO}", flush=True)


def delete() -> int:
    """Remove the diffusion weights from the hub cache. Returns bytes freed."""
    from huggingface_hub import scan_cache_dir
    cache = scan_cache_dir()
    hashes = [r.commit_hash for c in cache.repos if c.repo_id == MODEL_REPO
              for r in c.revisions]
    if not hashes:
        return 0
    freed = sum(c.size_on_disk for c in cache.repos if c.repo_id == MODEL_REPO)
    cache.delete_revisions(*hashes).execute()
    print(f"[image] deleted the image model ({freed / 1e9:.1f} GB)", flush=True)
    return freed


def plan(free_bytes: int) -> tuple[int, bool, bool]:
    """(pixels, low_ram, must_unload_llm) for the memory actually available.

    Two gigabytes of headroom on top of the measured cost: the figures are an
    average of one machine's behaviour, and the penalty for being optimistic is
    swapping, which costs far more than the smaller picture would have.
    """
    if free_bytes >= PEAK_FULL_BYTES + 2 * 1024**3:
        return 1024, False, False
    if free_bytes >= PEAK_LOWRAM_BYTES + 2 * 1024**3:
        return 768, True, False
    return 768, True, True


def generate(prompt: str, out_path: str, *, pixels: int = 1024,
             low_ram: bool = False, steps: int = 4, seed: Optional[int] = None,
             progress: Optional[Progress] = None, token: str = "",
             timeout: float = 900.0) -> str:
    """Render `prompt` to `out_path`. Returns the path; raises on failure."""
    argv = [sys.executable, "-m", "mflux.models.flux2.cli.flux2_generate",
            "--model", MODEL_REPO, "--base-model", BASE_MODEL,
            "--prompt", prompt, "--steps", str(steps),
            "--height", str(pixels), "--width", str(pixels),
            "--output", out_path, "--no-metadata"]
    if low_ram:
        argv += ["--low-ram", "--vae-tiling", "--mlx-cache-limit-gb", "2"]
    if seed is not None:
        argv += ["--seed", str(seed)]

    if token:
        with _procs_lock:
            if token in _cancelled:
                _cancelled.discard(token)
                raise Cancelled()

    env = dict(os.environ, TQDM_MININTERVAL="0.5")
    started = time.monotonic()
    sysmem.mark_child_start()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            bufsize=1, env=env)
    if token:
        with _procs_lock:
            _procs[token] = proc
    tail: list[str] = []
    peak = [0]

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line)
            del tail[:-40]
            # Sample while it runs: the cost is gone by the time it exits, and
            # reading it afterwards reported zero.
            peak[0] = max(peak[0], sysmem.snapshot(0)["child_bytes"])
            if progress is None:
                continue
            if "Fetching" in line or "Downloading" in line:
                progress("Downloading the image model — 4.6 GB, once", 0.0)
            elif (m := _STEP.search(line)):
                done, total = int(m.group(1)), int(m.group(2))
                if total:
                    progress(f"Rendering — step {done} of {total}", done / total)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("image generation timed out")
    reader.join(timeout=2)
    sysmem.mark_child_end()
    if token:
        with _procs_lock:
            _procs.pop(token, None)
            _cancelled.discard(token)
    if proc.returncode is not None and proc.returncode < 0:
        raise Cancelled()

    if proc.returncode != 0 or not os.path.exists(out_path):
        why = "".join(tail).strip().splitlines()
        raise RuntimeError(why[-1] if why else f"mflux exited {proc.returncode}")
    print(f"[image] {pixels}px in {time.monotonic() - started:.0f}s, "
          f"peak {peak[0] / 1e9:.1f} GB -> {out_path}", flush=True)
    return out_path
