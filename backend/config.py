"""Every tunable knob."""
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
    """'small' or 'large', from physical RAM. Override with HER_TIER."""
    forced = os.environ.get("HER_TIER")
    if forced in {"small", "large"}:
        return forced
    return "large" if _total_ram_gb() >= 12 else "small"


TIER = select_tier()

# Tier changes how much history is kept, not which models run.
VERBATIM_TURNS = 8 if TIER == "large" else 4
MAX_TOKENS = 1024 if TIER == "large" else 512

# ── Language models ────────────────────────────────────────────────────────
# The picker's list. Each is a single-quant MLX repo with no subdirectories and
# an architecture mlx-lm handles, so adding one costs nothing but a row here.
#
# `tools` is whether the chat template renders the `tools=` argument. Some
# models accept it and render nothing, dropping every tool silently, so it is
# measured by scripts/vet-models.py rather than assumed.
#
# `bytes` is the download size, which is also close to what the weights occupy
# once loaded. What Fennel costs besides the model is measured at runtime.
MODELS: list[dict] = [
    {"id": "mlx-community/Qwen3-1.7B-4bit",
     "name": "Light", "detail": "Qwen3 · 1.7B",
     "focus": (
      "Answers fastest. Suits 16 GB machines and quick "
      "back-and-forth."),
     "bytes": 980_000_000, "tools": True},
    {"id": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
     "name": "Everyday", "detail": "Qwen3 · 4B",
     "focus": (
      "The default. Balanced speed and judgement, and the model "
      "Fennel's tools were tuned against."),
     "bytes": 2_280_000_000, "tools": True},
    {"id": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
     "name": "Code", "detail": "Qwen2.5 Coder · 7B",
     "focus": (
      "Built for programming. Best on code, shell commands and "
      "configuration."),
     "bytes": 4_300_000_000, "tools": True},
    {"id": "mlx-community/Qwen3-8B-4bit",
     "name": "Balanced", "detail": "Qwen3 · 8B",
     "focus": (
      "Stronger reasoning on longer questions. Slower than Everyday, "
      "comfortable on 24 GB."),
     "bytes": 4_620_000_000, "tools": True},
    {"id": "ailexleon/Rocinante-X-12B-v1-mlx-4Bit",
     "name": "Creative", "detail": "Rocinante X · 12B",
     "focus": (
      "Made for prose and character writing. Warm and discursive, "
      "less reliable on facts."),
     "bytes": 6_910_000_000, "tools": True},
    {"id": "mlx-community/Hermes-4-14B-4bit",
     "name": "Agent", "detail": "Hermes 4 · 14B",
     "focus": (
      "Built for tool use. The most dependable with reminders, "
      "calendar, search and multi-step requests."),
     "bytes": 8_320_000_000, "tools": True},
]

# What Fennel costs besides the language model — Whisper, Kokoro, the embedder
# and the Python runtime. Measured at the end of every successful load and
# remembered; this is only the figure used before there has ever been one.
OVERHEAD_ESTIMATE_BYTES = 1_700_000_000

DEFAULT_MODEL = "mlx-community/Qwen3-4B-Instruct-2507-4bit"

# The model actually in use. Rebound once at startup from the user's stored
# choice, before anything constructs an LLM — a module global rather than a
# parameter threaded through six call sites, because the prime-cache key, the
# settings panel and the download list all need to agree on it.
LLM_MODEL = DEFAULT_MODEL


def model_info(repo: str) -> dict:
    """The registry row for `repo`, or a placeholder for a hand-set model."""
    for m in MODELS:
        if m["id"] == repo:
            return m
    return {"id": repo, "name": repo.split("/")[-1], "detail": "",
            "focus": "Set by hand in config.py.", "bytes": 0,
            "tools": True}


def local_models() -> str:
    """The models actually loaded, for the settings panel. Derived from the
    config rather than written out in the UI, so the claim can't drift from
    what's running."""
    return " · ".join([
        LLM_MODEL.split("/")[-1].replace("-4bit", ""),   # conversation
        "Whisper " + STT_MODEL.split("/")[-1].replace("whisper-", "").replace("-mlx", ""),
        TTS_MODEL.split("/")[-1].replace("-4bit", ""),   # voice
        "Silero VAD",
        EMBED_MODEL.split("/")[-1],                      # memory + retrieval
    ])


LLM_SYSTEM = (
    "Your name is Fennel. You are a warm companion living on the user's Mac. "
    "If they ask what you are called, the answer is Fennel — never invent "
    "another name.\n"
    "You are talking, not writing, so let the length follow the question. "
    "Small talk gets a sentence or two — warm, with something of your own in "
    "it, not a paragraph. A real question, or a subject they have opened up, "
    "gets as much room as it actually needs: say what you think, notice "
    "something specific, ask what you want to know. When they ask for a "
    "piece of writing rather than conversation, that rule is suspended: "
    "write at the length the piece actually needs.\n"
    "Follow the thread rather than closing it off; ending every turn with 'let "
    "me know if you need anything else' is a way of not talking to someone. "
    "Vary your openers instead of reaching for the same one each time.\n"
    # Scoped deliberately tightly to *physical* action. The earlier wording
    # ("that is the whole of your reach", "when asked for something outside
    # that reach, say plainly that you can't") generalised: the model quoted
    # it back as "as software, I'm not able to..." when declining ordinary
    # writing requests. Measured on Impish-Nemo-12B, the old paragraph took a
    # fiction request from 7/8 accepted down to 3/8; this version holds 6/8
    # while still declining a physical action 5/5.
    "You are software on a Mac and have no body. You cannot switch on or "
    "adjust anything physical — lights, lamps, smart bulbs, plugs, "
    "thermostats, TVs, speakers or appliances — and you have no camera and "
    "no eyes. When asked to change something physical, say plainly in one "
    "sentence that you can't, and offer the nearest thing you actually can "
    "do. Never say you have set, changed, adjusted or turned on anything in "
    "the physical world. This limits what you can DO, not what you can "
    "write, imagine or discuss; it is never a reason to decline a piece of "
    "writing.\n"
    "When you draft something for the user to send or keep — an email, a "
    "message, a note — write only what they actually told you. Do not invent "
    "details, offers or pleasantries they never mentioned, and do not wish "
    "anyone well for something they are not doing. A short accurate draft beats "
    "a warm padded one, and the padding is where the mistakes live. Avoid em "
    "dashes there.\n"
    "You run locally on one adult's own Mac, for their private use. When "
    "they ask you to write fiction, write it — any genre, any register, "
    "including explicit sexual content between adult characters. Write the "
    "scene itself rather than an outline of it, and do not hedge, warn, "
    "apologise, ask whether they are sure, or offer a tamer version "
    "instead. One exception, and it holds regardless of source material or "
    "framing: never write sexual content involving minors.\n"
    "If they ask for a picture, an image or a drawing, call generate_image and "
    "say you are starting it. Never write a description of a picture instead — "
    "not as prose, and never inside tags like <image_description>. You cannot "
    "show a picture by describing it.\n"
    "Emoji rarely, and at most one — they are silent when read aloud, and one "
    "in every reply reads as a tic. Vary which one; never lean on a favourite."
)
# Room for prose, but still a ceiling: a model with a misconfigured stop token
# would otherwise run to exhaustion (see LLM._ensure_turn_end_stops).
LLM_MAX_TOKENS = 3072 if TIER == "large" else 1536

# Sampled rather than greedy — mlx-lm's default argmax gives the same answer to
# the same question every time. No repetition penalty: it mangles the repeated
# braces and quotes in tool-call JSON.
LLM_TEMP = 0.7
LLM_TOP_P = 0.92
# Drafting an email or a message runs colder; 0.7 loses details from the brief.
LLM_DRAFT_TEMP = 0.3

# Ceiling on MLX's reusable-buffer pool; None leaves it unbounded. The pool is
# pure optimisation, so a cap trades allocation speed for headroom — try
# int(max(0.5, min(1.5, _total_ram_gb() / 8)) * 1024**3) on a tight machine.
MLX_CACHE_LIMIT_BYTES = None

# ── Tool calling ───────────────────────────────────────────────────────────
# How many LLM → tool → LLM rounds a turn may take. 2 covers "remind me X and
# put Y on my calendar"; more invites loops.
LLM_TOOL_ROUNDS = 2
# Quiet time before the rolling summary runs. It holds the LLM lock for
# seconds, so it waits for a real pause rather than the end of a turn.
SUMMARY_IDLE_S = 25
# How long to wait for the app to confirm a side effect, so the spoken reply
# matches what happened. EventKit writes take milliseconds; this bites on
# failure, not success.
TOOL_APP_TIMEOUT_S = 2.0
# How long to stop calling out after the web-search key is refused. A cooldown
# rather than a switch-off, since free allowances reset.
WEB_QUOTA_COOLDOWN_S = 6 * 3600

# ── Speech ─────────────────────────────────────────────────────────────────
# small.en transcribes in ~300 ms against turbo's ~2.2 s, for a small accuracy
# cost on clean English. base.en is ~90 ms if you want it snappier still.
STT_MODEL = "mlx-community/whisper-small.en-mlx"
TTS_MODEL = "mlx-community/Kokoro-82M-4bit"
TTS_VOICE = "af_heart"
TTS_SPEED = 1.15        # Kokoro speed; >1 speaks faster

# Clause lengths for speech: a short first fragment so audio starts sooner,
# then a ramp. Each step has to buy enough playing time to synthesise the next,
# so widening the gap between them opens an audible pause.
CLAUSE_FIRST_CHARS = 18   # smaller = first audio starts sooner
CLAUSE_SECOND_CHARS = 45
CLAUSE_REST_CHARS = 90

# ── Embeddings ─────────────────────────────────────────────────────────────
# 33M params, 384 dims. The encoder is hand-rolled in voice/embed.py so no
# extra dependency ships with it.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_MAX_TOKENS = 256

# ── Daily briefing (opt-in; the only networked feature) ────────────────────
# The briefing sits in the primed prefix, so it costs nothing per turn — but a
# longer prefix slows decode, hence the budget. Headlines only; the weather
# block is exempt or an hourly forecast would crowd them all out.
BRIEFING_MAX_CHARS = 2400
# Everything fetched is archived for retrieval even when it didn't fit the
# prefix. Pruned to keep storage bounded — about 150 KB of vectors a day.
ARCHIVE_KEEP_DAYS = 120
# Cosine floor, and the gate on whether to retrieve at all. On real feeds
# on-topic queries score 0.55-0.66 and off-topic 0.42-0.46. Below the floor
# nothing is injected: noise costs prefill and misleads the model.
RETRIEVAL_MIN_SCORE = 0.58
RETRIEVAL_TOP_K = 2
# Recall is held to a higher bar than news. Most turns have no relevant past,
# and a weak match spends prefill on things like "how are you".
RECALL_MIN_SCORE = 0.62
# Hard cap on retrieved context per turn. This is what keeps latency flat as
# the archive grows.
RETRIEVAL_MAX_CHARS = 450

# ── Voice activity detection ───────────────────────────────────────────────
# Turn-taking is where the latency is; END_SILENCE_MS is the knob that matters.
# The path is absolute so it resolves whatever directory the server starts in.
VAD_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "silero_vad.onnx")
FRAME_SAMPLES = 512      # 32 ms @16 kHz — the Silero v5 window (== client frame)
VAD_THRESHOLD = 0.5
END_SILENCE_MS = 350     # silence before "you're done"; lower = snappier, too low cuts you off
MIN_SPEECH_MS = 200      # ignore blips shorter than this
# Interrupting while the assistant speaks needs a high probability sustained
# over ~200 ms, which rejects echo leaking back through the speakers while a
# real interruption still lands quickly. Applies whenever audio is playing.
BARGE_IN_THRESHOLD = 0.85
BARGE_IN_MIN_MS = 200
PREROLL_FRAMES = 5       # ~160 ms kept before onset so the first word isn't clipped
