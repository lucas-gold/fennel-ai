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

## Stage 3 — Reactive home + tools + EventKit  ✓ DONE

- Python `voice/tools.py` — four tools (`set_reminder`, `add_event`,
  `show_panel`, `set_fact`) as JSON Schema handed to the chat template, so the
  signatures live in the *stable* prefix (one prefill per session, not per turn).
  `ToolStream` splits `<tool_call>` blocks out of the stream before TTS sees
  them; `normalize()` turns model arguments into absolute local times.
- `session.py` — LLM → tool → LLM loop, capped at `LLM_TOOL_ROUNDS`. Calls fire
  the instant they close, so the card lands while the model is still talking.
- Swift `HomeCards.swift` + `EventKitBridge.swift` — dismissible cards and real
  Reminders/Calendar writes; the app reports the outcome back as `tool_result`
  so the spoken confirmation is truthful about failures.
- `recommend_song` — a card with Apple Music / Spotify search buttons (D-MUSIC).
- ✕ on a reminder/event deletes the real EventKit entry too, with a 6 s Undo.
- `set_fact` is session-scoped for now; Stage 4 gives it a durable store.

Also fixed here: the mic was open for the whole session (D-MIC), and the clause
splitter jumped 18→90 chars, which starved playback about a second in — measured
0.01 s of slack before the next clause arrived, now 0.82 s with a 45-char step.

## Stage 4 — Memory + persistent chats  ✓ DONE

- `store.py` — SQLite (WAL) in `~/Library/Application Support/my_ai`: sessions,
  messages, facts, summaries, and an FTS5 index over messages for recall.
- `memory.py` — the three prompt inputs, each placed at the depth that matches
  how often it changes (D-MEMORY): facts + summary in a `<context>` message
  ahead of the window, recall + clock on the current user message, verbatim
  turns evicted in chunks.
- Chats persist across restarts and reconnects; the app gets `sessions` /
  `session_opened` and can start, switch, close and delete them (D-SESSIONS).
- Rolling summary runs on a throwaway KV cache via `LLM.complete`, off the
  turn's critical path, so it can't evict the live conversation's prefix.
- New tools: `agenda` (reads Reminders + Calendar back — the first tool whose
  answer comes *from* the app via `tool_result.data`), `set_timer` (on-screen
  countdown), `open_link` (a button, never an automatic open).

## Stage 4.5 — Daily briefing + retrieval (opt-in)  ✓ DONE

- `embed.py` — bge-small BERT encoder written directly against MLX, validated
  against torch (D-EMBED). Also upgrades conversational recall from keyword-only.
- `feeds.py` — the only networked code: Open-Meteo weather + RSS headlines,
  stdlib-only, fixed source list, hard timeouts, failure never fatal.
- `briefing.py` — builds the day's briefing into the *primed prefix* so it costs
  nothing per turn (D-BRIEFING), archives everything fetched for retrieval, and
  gates retrieval on a measured cosine floor so latency stays flat as it grows.
- Off by default; one switch in the app, with the weather city typed by hand
  rather than read from CoreLocation.

- `search_web` — Wikipedia: free, keyless, 0.2–0.6 s (D-SEARCH).
- Mac actions: `open_app`, `run_shortcut`, `create_shortcut` (D-MAC, D-AUTHOR).

### Still open here
- **General web search** (beyond Wikipedia) needs an API key from someone —
  Ollama's included, see D-NOKEY. Wants a bring-your-own-key field, not code.

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
