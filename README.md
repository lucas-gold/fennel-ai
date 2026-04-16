# my_ai

A local speech-to-speech companion for Apple Silicon Macs. Text or voice,
running entirely on-device. No API key, no account, no network after the
first model download. Ships as a paid, direct-download Mac app.

Video is deliberately left out of v1; the seam is cut on the Python side
(`backend/her/vision.py`) for later.

## Architecture — two local processes

Unlike the original design (browser UI + Python), the UI is a **native
SwiftUI app**. It owns audio I/O and echo cancellation; Python owns all the
ML. They talk over a local WebSocket — fully offline, nothing leaves the
machine.

```
┌────────────────────────────┐          ┌─────────────────────────────┐
│  SwiftUI app (frontend)    │          │  Python backend (the brains)│
│                            │  16 kHz  │                             │
│  AVAudioEngine + AEC       │  int16   │  vad  → stt → llm → tts     │
│  (setVoiceProcessingEnabled)├─frames──►│  Silero mlx-  mlx-  Kokoro  │
│  mic capture / playback    │◄─24 kHz──┤        whisper lm  (mlx-audio)│
│  home screen · chat · orb  │   PCM    │                             │
│  EventKit (reminders/cal)  │◄─tools───┤  memory (SQLite FTS5)       │
└────────────────────────────┘  local   └─────────────────────────────┘
                                  WS
```

Split of responsibilities:
- **Swift** — audio capture + AEC, playback, all UI (home cards, chat, voice
  orb), and EventKit (real macOS Reminders/Calendar).
- **Python** — VAD endpointing, STT, LLM (with prefix-cache reuse), TTS,
  memory. Same latency/streaming/barge-in engine as the reference design.

Audio in: Swift captures via `AVAudioEngine`, resamples to 16 kHz, sends
512-sample int16 frames. `setVoiceProcessingEnabled(true)` provides the AEC
that the browser's `getUserMedia` used to give for free.
Audio out: `>II` header (turn, seq) + int16 PCM at 24 kHz.

## The four non-negotiables (inherited, unchanged)

1. **Latency is the product.** Under 300 ms time-to-first-audio. Never wait
   for a stage to *complete* when you could consume its stream.
2. **Barge-in cancels everything** within ~100 ms; history records only what
   was actually spoken.
3. **Fully offline** after first-launch download. Nothing in the hot path
   touches a network.
4. **Apache 2.0 / MIT only.** Paid product — see `docs/DECISIONS.md`.

## Platform & distribution

- **Apple Silicon macOS only.** MLX and SwiftUI are both Apple-only; that is
  a deliberate, accepted constraint (see D-PLATFORM).
- **Direct download only** (Developer ID + notarization, Sparkle updates,
  Paddle/Stripe). The Mac App Store is **not** a target — dropping it removes
  the App Sandbox constraint and lets the app keep a Python backend.

## Layout (planned)

```
README.md                  you are here
docs/DECISIONS.md          why things are the way they are
docs/ROADMAP.md            staged build order
backend/                   Python + MLX
  server.py                WebSocket server, model bootstrap
  config.py                tunables + RAM tiering
  her/
    vad.py  stt.py  llm.py  tts.py  session.py  memory.py  vision.py
  requirements.txt
app/                       Swift package / Xcode project (SwiftUI)
  Sources/…                audio engine, WS client, home UI, EventKit
training/                  SEPARATE: Phase-4 LoRA fine-tuning + evals (Python)
```

## Running it (Stage 1)

```bash
./scripts/setup-venv.sh                       # one-time: uv + Python 3.12 + deps
backend/.venv/bin/python backend/server.py    # loads Qwen (first run downloads ~2.3 GB)
```
Then, in another terminal:
```bash
cd app && swift run                           # SwiftUI app; type to chat locally
```

## Status

Fresh build from the reference design docs in `~/Downloads/files/`. **Stages 0–1
done**: two-process transport + native LLM text chat with prefix-cache reuse
(D4), verified end to end. Build order in `docs/ROADMAP.md`; Stage 2 (voice:
VAD/STT/TTS + AEC) is next.
