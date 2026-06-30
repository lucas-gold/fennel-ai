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

#: A template that renders tools mentions the variable it is given them in.
#: Checking for Fennel's own tool names instead was wrong — a template never
#: contains those, it loops over whatever it is handed.
_TOOL_MARKERS = ("tools", "tool_call")

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


#: What to fetch for the language model, and nothing else.
#:
#: Some repos ship every quantisation in one tree — 2-bit, 4-bit, 6-bit and
#: 8-bit side by side — and an unrestricted snapshot_download takes all of them:
#: 94.7 GB to use 16. fnmatch anchors at the start of the path, so
#: "model*.safetensors" matches the weights at the root and not "8-bit/model…".
#: Only the language model is restricted; the other three are single-quant and
#: keep their weights under names these patterns would miss.
_LLM_PATTERNS = ["*.json", "*.jinja", "*.txt", "*.py",
                 "model*.safetensors", "tokenizer.model"]


def _llm_bytes() -> int:
    """How big the chosen language model is, for the progress bar.

    The registry first, then anything the user added by hand — the probe
    measured that one — and only then the generic constant. A fixed 2.3 GB
    scaled the bar to the wrong total for every model but one: it stalled short
    of the end on a bigger model and sat at 100% through the rest of a smaller
    one's download.
    """
    known = config.model_info(config.LLM_MODEL).get("bytes")
    if known:
        return known
    for row in custom_models():
        if row["id"] == config.LLM_MODEL and row.get("bytes"):
            return int(row["bytes"])
    return _SIZES["llm"]


def _repos() -> list[tuple[str, str, int, Optional[list]]]:
    """(key, repo_id, approx_bytes, allow_patterns) for everything Fennel needs."""
    return [
        # The language model's size comes from the registry, not the constant:
        # a fixed 2.3 GB meant the bar was scaled to the wrong total for every
        # model but one — stalling short of the end on a bigger model and
        # sitting at 100% for the rest of a smaller one's download.
        ("llm", config.LLM_MODEL, _llm_bytes(), _LLM_PATTERNS),
        ("stt", config.STT_MODEL, _SIZES["stt"], None),
        ("tts", config.TTS_MODEL, _SIZES["tts"], None),
        ("embed", config.EMBED_MODEL, _SIZES["embed"], None),
    ]


#: Not every model ships safetensors. Whisper's MLX build is a single
#: weights.npz, and checking only for safetensors reported it as absent — which
#: would have re-downloaded half a gigabyte on every launch.
_WEIGHTS = (".safetensors", ".npz", ".bin", ".pth", ".gguf")


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
                    if f.file_name.endswith(_WEIGHTS))
        if total:
            out[repo.repo_id] = total
    return out


#: Models the user pasted in themselves, kept in the settings table so they
#: survive a restart. Stored as the repo id plus whatever the probe learned, so
#: the picker can describe a custom row without going back to the network.
_CUSTOM_KEY = "custom_models"


def _store():
    from voice.store import Store
    return Store()


def custom_models() -> list[dict]:
    import json as _json
    try:
        rows = _json.loads(_store().setting(_CUSTOM_KEY, "") or "[]")
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def add_custom(row: dict) -> None:
    import json as _json
    rows = [r for r in custom_models() if r["id"] != row["id"]]
    rows.append(row)
    _store().set_setting(_CUSTOM_KEY, _json.dumps(rows))


def forget_custom(repo: str) -> None:
    import json as _json
    rows = [r for r in custom_models() if r["id"] != repo]
    _store().set_setting(_CUSTOM_KEY, _json.dumps(rows))


def probe(repo: str) -> dict:
    """Look a repo over before committing to it.

    Everything that goes wrong with an unfamiliar model is visible in its
    config and its chat template — a few kilobytes — so it is worth reading
    them before a five-gigabyte download rather than after. Reports findings
    rather than refusing: the user can proceed knowing what is wrong.
    """
    import json as _json
    import pathlib
    import urllib.request

    import mlx_lm

    repo = repo.strip().strip("/")
    if repo.startswith("http"):
        repo = "/".join(repo.split("huggingface.co/")[-1].split("/")[:2])
    if repo.count("/") != 1:
        return {"ok": False, "id": repo,
                "problems": ["That is not a Hugging Face path — it should look "
                             "like owner/model-name."]}

    def fetch(name: str) -> str:
        try:
            with urllib.request.urlopen(
                    f"https://huggingface.co/{repo}/raw/main/{name}", timeout=20) as r:
                return r.read().decode()
        except Exception:
            return ""

    raw_cfg = fetch("config.json")
    if not raw_cfg:
        return {"ok": False, "id": repo,
                "problems": ["No config.json there — check the path, or the "
                             "model may be private or gated."]}
    try:
        cfg = _json.loads(raw_cfg)
    except Exception:
        return {"ok": False, "id": repo, "problems": ["Its config.json is unreadable."]}

    problems: list[str] = []
    warnings: list[str] = []

    # Resolve the type the way mlx-lm does before deciding it is unsupported.
    # Several families are served by another family's implementation — Mistral
    # by llama.py, for one — so checking the filenames alone declared working
    # models unsupported.
    from mlx_lm.utils import MODEL_REMAPPING

    supported = {p.stem for p in
                 pathlib.Path(mlx_lm.__file__).parent.joinpath("models").glob("*.py")}
    mtype = cfg.get("model_type", "")
    resolved = MODEL_REMAPPING.get(mtype, mtype)
    if resolved not in supported:
        problems.append(f"mlx-lm has no support for '{mtype}' models.")

    quant = cfg.get("quantization") or cfg.get("quantization_config") or {}
    bits = quant.get("bits")
    if not quant:
        warnings.append("Not quantised — it will be much larger than a 4-bit build.")

    tpl = fetch("chat_template.jinja") or fetch("tokenizer_config.json")
    if "chat_template" not in tpl and "im_start" not in tpl and "message" not in tpl:
        problems.append("No chat template, so there is no way to hold a "
                        "conversation with it.")
    if "<think>" in tpl:
        warnings.append("It reasons before answering, which the voice loop "
                        "cannot afford; Fennel will suppress it where it can.")
    tools = sum(1 for marker in _TOOL_MARKERS if marker in tpl)
    if not tools:
        warnings.append("Its template ignores tool definitions — reminders, "
                        "timers, the agenda and search will not work.")

    size, subdirs = _repo_size(repo)
    if subdirs:
        warnings.append(f"Ships several builds in one repo ({', '.join(subdirs[:4])}); "
                        "only the top-level weights are fetched.")

    return {
        "ok": not problems,
        "id": repo,
        "name": repo.split("/")[-1],
        "detail": (f"{mtype} · {bits}-bit" if bits else mtype),
        "bytes": size,
        "tools": bool(tools),
        "problems": problems,
        "warnings": warnings,
    }


def _repo_size(repo: str) -> tuple[int, list]:
    """Bytes of top-level weights, and any quantisation subfolders."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true",
                timeout=20) as r:
            tree = _json.load(r)
    except Exception:
        return 0, []
    total = 0
    subdirs = set()
    for f in tree:
        if f.get("type") != "file":
            continue
        path = f["path"]
        size = (f.get("lfs") or {}).get("size") or f.get("size", 0)
        if "/" in path:
            if path.endswith((".safetensors", ".npz", ".bin")):
                subdirs.add(path.split("/")[0])
            continue
        total += size
    return total, sorted(subdirs)


#: How much of a model's expected weight has to be present before it counts as
#: downloaded. Not 100%: the patterns above skip files the published size
#: includes, so a complete fetch lands a little short of the repo's own total.
#: A finished download measures ~99% of the published total (Everyday: 2.26
#: of 2.28 GB), so this is generous rather than tight.
_COMPLETE_ENOUGH = 0.9


def complete(repo: str, expected: int, on_disk: Optional[int] = None) -> bool:
    """Whether enough of `repo` is on disk to call it downloaded.

    Presence of any weight file is not enough. An interrupted download leaves
    real files behind, and treating those as a finished model meant the picker
    skipped fetching the rest — so the first request for a picture paid for it
    instead, mid-conversation, which is exactly what downloading at selection
    was meant to prevent.
    """
    if on_disk is None:
        on_disk = _weights_on_disk().get(repo, 0)
    if not on_disk:
        return False
    if expected <= 0:
        return True          # nothing to compare against; presence must do
    return on_disk >= expected * _COMPLETE_ENOUGH


def installed(repo: str) -> bool:
    """Whether `repo` is genuinely usable offline, weights and all."""
    expected = 0
    for row in list(config.MODELS) + custom_models():
        if row["id"] == repo:
            expected = row.get("bytes", 0)
            break
    return complete(repo, expected)


def catalogue() -> list[dict]:
    """The registry, annotated with what is on disk. Everything the startup
    picker needs, resolved here so the app holds no model knowledge of its own."""
    have = _weights_on_disk()
    rows = [dict(m, installed=complete(m["id"], m.get("bytes", 0), have.get(m["id"], 0)),
                 on_disk=have.get(m["id"], 0), custom=False) for m in config.MODELS]
    # Anything the user added by hand goes after the curated list, in the order
    # they were added.
    rows += [dict(m, installed=complete(m["id"], m.get("bytes", 0), have.get(m["id"], 0)),
                  on_disk=have.get(m["id"], 0), custom=True) for m in custom_models()]
    return rows


def delete(repo: str, in_use: Optional[str] = None) -> int:
    """Remove a downloaded model from the hub cache. Returns bytes freed.

    Refuses to touch anything outside the model registry, and refuses to delete
    `in_use` — a picker that can delete the model it is about to load is one
    that can brick the next launch. Before anything is loaded there is no such
    model, so during the startup picker every row is fair game.
    """
    known = {m["id"] for m in config.MODELS} | {m["id"] for m in custom_models()}
    if repo not in known:
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
    """Which models still need downloading. Empty means we can start offline.

    Weights, not the cache index. A repo whose blobs have been deleted — or one
    only ever fetched for its config — is still listed in the index, and taking
    that as proof of presence meant the download step was skipped and the model
    fetched later, at load time, without ever passing the consent screen.
    """
    have = _weights_on_disk()
    return [(k, r, n, pat) for k, r, n, pat in _repos()
            if not complete(r, n, have.get(r, 0))]


def total_bytes(items: Optional[list] = None) -> int:
    return sum(n for _, _, n, _pat in (items if items is not None else missing()))


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


#: Repos whose cache has already been cleared and re-fetched this session, so a
#: model that is simply smaller than its published size cannot be wiped and
#: re-downloaded on a loop.
_repaired: set = set()


def _clear_repo(repo: str) -> None:
    """Drop a repo from the hub cache so the next download starts clean."""
    try:
        cache = scan_cache_dir()
        hashes = [r.commit_hash for c in cache.repos if c.repo_id == repo
                  for r in c.revisions]
        if hashes:
            cache.delete_revisions(*hashes).execute()
            print(f"[setup] cleared a partial download of {repo}", flush=True)
    except Exception as exc:
        print(f"[setup] couldn't clear {repo}: {exc}", flush=True)


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
        for key, repo, _approx, patterns in items:
            # An interrupted download leaves a file that the hub believes is
            # finished — it trusts its own metadata rather than the size on
            # disk, so asking again returns instantly and repairs nothing. The
            # only way to complete it is to throw the partial away first. Once
            # per session, so a model that is merely smaller than its published
            # total cannot be wiped and re-fetched forever.
            on_disk = _weights_on_disk().get(repo, 0)
            if on_disk and not complete(repo, _approx, on_disk) \
                    and repo not in _repaired:
                _repaired.add(repo)
                _clear_repo(repo)
            label = {
                "llm": f"{config.model_info(config.LLM_MODEL)['name']} model",
                "stt": "speech recognition", "tts": "voice",
                "embed": "memory"}.get(key, key)
            report_now()
            snapshot_download(repo, allow_patterns=patterns)
    finally:
        stop.set()
        watcher.join(timeout=2)
    progress("done", grand, grand)
