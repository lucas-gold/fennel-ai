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

## Stage 1 — Text chat, end to end  ◄ NEXT

- Python: `llm.py` with `mlx-lm` (Qwen3.5-4B), streaming tokens over WS, with
  explicit prefix-cache reuse (reference D4).
- Swift: chat UI streams tokens as they arrive.

Success: type in the Swift app, see a streamed local reply. Proves MLX + the
WS contract before any audio.

## Stage 2 — Voice

- Swift: `AVAudioEngine` capture with `setVoiceProcessingEnabled(true)` (AEC)
  → 16 kHz int16 frames over WS; playback of returned PCM; voice-orb
  visualizer driven by VAD state.
- Python: `vad.py` (Silero ONNX via onnxruntime — reference D10), `stt.py`
  (`mlx-whisper`), `tts.py` (Kokoro via `mlx-audio` + clause splitter D5),
  `session.py` orchestrator with the epoch counter + barge-in (reference D3).

Success: speak, get interrupted-able spoken replies. Verify AEC prevents
self-interruption. This is "feels alive."

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
