# Roadmap

Ordered. Each stage is independently demoable and assumes the one before it
works. Deep per-component notes live in the reference spec
(`~/Downloads/files/ROADMAP.md`); this is the build order for the two-process
`my_ai` architecture.

---

## Stage 0 — Scaffold both sides + the wire contract

- Python `backend/`: venv, `requirements.txt`, `server.py` WebSocket server
  that accepts a connection and echoes a control message. `config.py` with
  RAM tiering (`ProcessInfo`-equivalent via `psutil`/`os`).
- Swift `app/`: SwiftUI project that connects to `ws://127.0.0.1:<port>` and
  renders an empty home screen + chat box.
- **Define the protocol** once, here: JSON control frames (turn state, tokens,
  tool calls) + binary audio frames (mic in: 16 kHz int16; audio out: `>II`
  header + 24 kHz int16 PCM).

Success: Swift app connects, round-trips a text ping.

## Stage 1 — Text chat, end to end  ✓ DONE

- Python: `voice/llm.py` with `mlx-lm` streaming Qwen3-4B, explicit prefix-cache
  reuse (D4 — verified: turn 2 reused 28/45 prompt tokens). Model
  `mlx-community/Qwen3-4B-Instruct-2507-4bit` (~2.3 GB, first-run download).
- Swift: chat UI streams tokens as they arrive (no change needed from Stage 0).
- Toolchain: backend Python via **uv** (Homebrew python@3.12 is broken on
  macOS 26 — see `scripts/setup-venv.sh`).

Verified: `server.py` streamed a real reply over the WS contract end to end.

## Stage 2 — Voice

**Python backend ✓ DONE (verified end to end):**
- `vad.py` — Silero v5 on onnxruntime (D10, no torch); 64-sample context prepend
  was the key fix. Endpointer with preroll + `END_SILENCE_MS`.
- `stt.py` — `mlx-whisper` turbo, array input (no ffmpeg).
- `tts.py` — Kokoro via `mlx-audio` (needs `misaki`), greedy clause splitter (D5).
- `session.py` — orchestrator with epoch counter + barge-in (D3): LLM stop-event,
  generator close, cache reset on interrupt, partial reply committed with `—`.
- Binary audio framing in `protocol.py`; `server.py` handles mic-in / audio-out.
- Debt: `mlx-audio` pulls torch (~2.5 GB) — revisit (kokoro-onnx?) at Stage 5.

**Swift audio engine ✓ WRITTEN — ◄ needs live verification:**
- `AudioEngine.swift` — `AVAudioEngine` capture with `setVoiceProcessingEnabled(true)`
  (native AEC) → 512-sample int16 @16 kHz frames; 24 kHz PCM playback, turn-aware +
  barge-in stop. Binary WS frames in `Net.swift`. Voice orb + mic toggle.
- Packaged as a `.app` via `scripts/build-app.sh` (Info.plist mic usage + ad-hoc
  sign) — a bare `swift run` binary can't get microphone TCC.

Still to verify live (needs a mic + speakers): (1) mic permission prompt appears
and capture works; (2) AEC actually prevents self-interruption (D6); (3) barge-in
stops playback + LLM within ~100 ms. Run: `./scripts/build-app.sh` then open the
app with the backend running.

## Stage 3 — Reactive home + tools + EventKit

- Python: tool-calling loop emitting `setReminder`/`addEvent`/`showPanel`/
  `setFact` over WS (reference Phase 2).
- Swift: `HomeStore` (@Observable), dismissible cards, EventKit permission +
  real Reminders/Calendar writes.

## Stage 4 — Memory

- Python: `memory.py` — rolling summary + SQLite FTS5 recall + facts table
  (reference D8). `setFact` tool wired to it.

## Stage 5 — Ship

- First-launch model downloader: resumable, checksummed, cancellable.
- Licenses panel (full LICENSE + NOTICE text — Apache 2.0 requires it to
  travel with the app; check Kokoro per-voice CC-BY).
- Developer ID signing + notarization; Sparkle update channel; Paddle/Stripe.

## Later

- **Video** (reference Phase 5): implement `vision.py` frame gate; switch LLM
  to Qwen3-VL-8B. Vision is context, not a turn.
- **Persona LoRA + JSON-tool adapter** (reference Phase 4): in `training/`,
  the separate Python pipeline.
