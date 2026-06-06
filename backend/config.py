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
# The models the user can pick between on the startup screen. Kept deliberately
# boring: every one is a single-quant MLX repo with no subdirectories, an
# architecture mlx-lm already handles, and no reasoning block left open — so
# choosing between them costs no code beyond this table.
#
# `tools` records whether the model's chat template actually renders the
# `tools=` argument. Several well-known models silently ignore it, which drops
# all thirteen tools without an error, so it is measured rather than assumed
# (see scripts/vet-models.py) and shown as a warning on the picker.
#
# `bytes` is the download, measured from the repo tree. `ram` is roughly what
# the process holds with it loaded: weights plus ~1.2 GB for Whisper, Kokoro and
# the embedder, plus the app.
MODELS: list[dict] = [
    {"id": "mlx-community/Llama-3.2-3B-Instruct-4bit",
     "name": "Light", "detail": "Llama 3.2 · 3B",
     "focus": "The quickest to answer, and the smallest. Best on 16 GB, or "
              "when you want replies to feel instant more than thorough.",
     "bytes": 1_820_000_000, "ram": 3.1, "tools": True},
    {"id": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
     "name": "Everyday", "detail": "Qwen3 · 4B",
     "focus": "The default, and the one the persona and tools were tuned "
              "against. A good balance of speed and sense.",
     "bytes": 2_280_000_000, "ram": 3.5, "tools": True},
    {"id": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
     "name": "Code", "detail": "Qwen2.5 Coder · 7B",
     "focus": "Trained on code. Choose it for programming questions, shell "
              "commands and config files; it is plainer company than the rest.",
     "bytes": 4_300_000_000, "ram": 5.6, "tools": True},
    {"id": "mlx-community/Qwen3-8B-4bit",
     "name": "Balanced", "detail": "Qwen3 · 8B",
     "focus": "Noticeably better at reasoning and long questions than "
              "Everyday, and noticeably slower. Comfortable on 24 GB.",
     "bytes": 4_620_000_000, "ram": 5.9, "tools": True},
    {"id": "ailexleon/Rocinante-X-12B-v1-mlx-4Bit",
     "name": "Creative", "detail": "Rocinante X · 12B",
     "focus": "Tuned for prose and character writing, and far less likely to "
              "refuse. Warmer and looser; not the one for facts.",
     "bytes": 6_910_000_000, "ram": 8.2, "tools": True},
    {"id": "mlx-community/Hermes-4-14B-4bit",
     "name": "Agent", "detail": "Hermes 4 · 14B",
     "focus": "Built around tool use — the steadiest at reminders, calendar "
              "and search, and the best at multi-step requests. The largest "
              "here: fine on 24 GB, tight on 16 GB.",
     "bytes": 8_320_000_000, "ram": 9.6, "tools": True},
    {"id": "alexgusevski/Impish_Nemo_12B-mlx-4Bit",
     "name": "Unfiltered", "detail": "Impish Nemo · 12B",
     "focus": "The least restrained of the set, for fiction and roleplay.",
     "bytes": 6_910_000_000, "ram": 8.2, "tools": False},
]

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
            "focus": "Set by hand in config.py.", "bytes": 0, "ram": 0.0,
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
    "Emoji rarely, and at most one — they are silent when read aloud, and one "
    "in every reply reads as a tic. Vary which one; never lean on a favourite."
)
# Prose needs far more room than talk: 1024 tokens is ~750 words, which cut
# every scene off mid-sentence. Still a ceiling rather than none, because a
# model whose stop token is misconfigured will otherwise generate until the
# heat death of the session (see LLM._ensure_turn_end_stops).
LLM_MAX_TOKENS = 3072 if TIER == "large" else 1536

# mlx-lm defaults to greedy decoding (sampler=None -> argmax), which is why the
# same question produced a byte-identical answer every time, always the shortest
# safe phrasing, and always the same emoji. Sampling fixes all three.
# No repetition penalty on purpose: it distorts the repeated quotes and braces in
# tool-call JSON, and cross-turn repetition is a greedy problem, not a loop.
LLM_TEMP = 0.7
LLM_TOP_P = 0.92
# Drafting is the one task where 0.7 measurably hurts. Asked to write the same
# email twice at 0.7, one sample wished the *recipient* well at a wedding the
# sender was attending; at 0.3 neither sample lost the premise. Conversation
# keeps the higher temperature — dropping it globally is what made replies
# repetitive in the first place.
LLM_DRAFT_TEMP = 0.3

# Ceiling on MLX's reusable-buffer pool, or None to leave it unbounded.
#
# The cap exists because the pool grew to 3.96 GB beside 2.91 GB of live weights
# while priming the 4B, and a 3.5 GB app that swaps is a slow one. It is pure
# optimisation either way: capping trades allocation speed for headroom.
# Unbounded is the deliberate choice here — set this back to
#     int(max(0.5, min(1.5, _total_ram_gb() / 8)) * 1024**3)
# to restore the cap. Note it applies to whichever model is selected above.
MLX_CACHE_LIMIT_BYTES = None

# ── Tool calling / home screen (Stage 3) ───────────────────────────────────
# How many times a turn may go LLM → tool → LLM before we force a plain reply.
# 2 covers "remind me X and put Y on my calendar"; more invites runaway loops.
LLM_TOOL_ROUNDS = 2
# How long the conversation must be quiet before the rolling summary runs. It
# holds the LLM lock for seconds, so it waits for a genuine pause instead of
# firing the instant a turn ends and blocking whatever the user says next.
SUMMARY_IDLE_S = 25
# The app performs the real EventKit write and reports back. We wait this long
# so the spoken confirmation reflects what actually happened (including a
# permission denial) — EventKit writes take ~ms, so this only bites on failure.
TOOL_APP_TIMEOUT_S = 2.0
# After the web-search key is refused (quota or auth), stop calling out for this
# long rather than retrying into a wall. Free allowances reset, so it is a
# cooldown rather than a permanent switch-off.
WEB_QUOTA_COOLDOWN_S = 6 * 3600

# ── STT / TTS (Stage 2) ────────────────────────────────────────────────────
# small.en ~300ms vs turbo's ~2.2s here (D9 revisited: turbo's accuracy edge
# wasn't worth 7x the latency on clean English). base.en (~90ms) if you want
# it even snappier and can accept a bit more error.
STT_MODEL = "mlx-community/whisper-small.en-mlx"
TTS_MODEL = "mlx-community/Kokoro-82M-4bit"
TTS_VOICE = "af_heart"  # check per-voice CC-BY before shipping (SHIPPING.md)
TTS_SPEED = 1.15        # Kokoro speed; >1 speaks faster

# Clause splitter (D5): greedy first fragment, then ramp up.
# The ramp matters — going straight from 18 to 90 chars left an audible gap a
# second in: the 18-char clause is only ~1.1 s of speech but the 90-char one
# takes ~1.4 s to synthesise, so playback drained before it arrived. Each step
# must buy enough playing time to cover synthesising the next.
CLAUSE_FIRST_CHARS = 18   # smaller = first audio starts sooner
CLAUSE_SECOND_CHARS = 45
CLAUSE_REST_CHARS = 90

# ── Embeddings / retrieval (Stage 5) ───────────────────────────────────────
# MIT-licensed (D-DISTRIB), 33M params, 384 dims. Hand-rolled encoder in
# voice/embed.py so no extra dependency ships with it.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_MAX_TOKENS = 256

# ── Daily briefing (opt-in; the only networked feature) ────────────────────
# The briefing lives in the PRIMED PREFIX, so it costs nothing per turn — but a
# longer prefix does slow decode (measured 24 -> 21 tok/s at ~1300 tokens), so
# it is budgeted. ~2400 chars is roughly 600 tokens.
# Headline budget only — the weather block is exempt, since an hour-by-hour
# forecast is ~800 chars and would otherwise crowd out every headline.
BRIEFING_MAX_CHARS = 2400
# Everything fetched is archived for retrieval even if it didn't fit the prefix.
# Pruned so storage is bounded: ~150 KB/day of vectors, so a year is ~50 MB.
ARCHIVE_KEEP_DAYS = 120
# Cosine floor, and the gate that decides whether to retrieve at all. Measured
# separation on real feeds: on-topic queries score 0.55-0.66, off-topic 0.42-0.46.
# Below the floor we inject NOTHING — noise costs prefill and misleads the model.
RETRIEVAL_MIN_SCORE = 0.58
RETRIEVAL_TOP_K = 2
# Conversation recall is held to a higher bar than news: most turns genuinely
# have no relevant past, and a weak "match" was costing 60+ tokens of prefill
# per turn to inject things like "how are you".
RECALL_MIN_SCORE = 0.62
# Hard cap on retrieved context per turn. This is the number that keeps latency
# constant as the archive grows — never raise it to "fit more in".
RETRIEVAL_MAX_CHARS = 450

# ── VAD / endpointing (Stage 2) ────────────────────────────────────────────
# Latency hides in turn-taking, not the models — tune END_SILENCE_MS first (D2).
# Absolute so it resolves no matter the working directory the server is launched from.
VAD_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "silero_vad.onnx")
FRAME_SAMPLES = 512      # 32 ms @16 kHz — the Silero v5 window (== client frame)
VAD_THRESHOLD = 0.5
END_SILENCE_MS = 350     # silence before "you're done"; lower = snappier, too low cuts you off
MIN_SPEECH_MS = 200      # ignore blips shorter than this
# Barge-in while the assistant is talking is held to a much higher bar. Voice
# processing cancels most of the speaker feed, but the residue was enough to
# make it interrupt itself; requiring a high probability *sustained* over ~200 ms
# rejects leaked echo while a real interruption still lands in well under a
# second. Applies for the whole time audio is actually playing, not just while
# the backend is still generating it.
BARGE_IN_THRESHOLD = 0.85
BARGE_IN_MIN_MS = 200
PREROLL_FRAMES = 5       # ~160 ms kept before onset so the first word isn't clipped
