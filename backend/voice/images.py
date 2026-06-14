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
from voice.store import APP_DIR

MODEL_REPO = "Runpod/FLUX.2-klein-4B-mflux-4bit"
BASE_MODEL = "flux2-klein-4b"
MODEL_BYTES = 4_620_000_000

#: Peak MLX memory measured on an M2, at the two sizes we use.
PEAK_FULL_BYTES = int(12.4 * 1024**3)
PEAK_LOWRAM_BYTES = int(8.0 * 1024**3)

_STEP = re.compile(r"(\d+)/(\d+)\s*\[")
Progress = Callable[[str, float], None]        # (detail, 0..1)


def images_dir() -> str:
    path = os.path.join(APP_DIR, "images")
    os.makedirs(path, exist_ok=True)
    return path


def installed() -> bool:
    """Whether the diffusion weights are already downloaded."""
    from voice.setup import _weights_on_disk
    return MODEL_REPO in _weights_on_disk()


def plan(free_bytes: int) -> tuple[int, bool, bool]:
    """(pixels, low_ram, must_unload_llm) for the memory actually available.

    Full size needs ~12.4 GB and low-RAM 768px needs ~8.0 GB, both measured. If
    neither fits beside the language model, the caller unloads it first — an
    image is worth a reload, a swap to disk is not.
    """
    if free_bytes >= PEAK_FULL_BYTES + 1024**3:
        return 1024, False, False
    if free_bytes >= PEAK_LOWRAM_BYTES + 1024**3:
        return 768, True, False
    return 768, True, True


def generate(prompt: str, out_path: str, *, pixels: int = 1024,
             low_ram: bool = False, steps: int = 4, seed: Optional[int] = None,
             progress: Optional[Progress] = None,
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

    env = dict(os.environ, TQDM_MININTERVAL="0.5")
    started = time.monotonic()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            bufsize=1, env=env)
    tail: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line)
            del tail[:-40]
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

    if proc.returncode != 0 or not os.path.exists(out_path):
        why = "".join(tail).strip().splitlines()
        raise RuntimeError(why[-1] if why else f"mflux exited {proc.returncode}")
    print(f"[image] {pixels}px in {time.monotonic() - started:.0f}s -> {out_path}",
          flush=True)
    return out_path
