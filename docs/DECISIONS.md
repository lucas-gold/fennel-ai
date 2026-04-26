# Decisions

Why things are the way they are. The deep pipeline rationale (D1–D11) is
inherited from the reference spec in `~/Downloads/files/DECISIONS.md` and is
NOT re-litigated here — read that for the STT/LLM/TTS/VAD/memory reasoning.
This file records the decisions specific to `my_ai` that *changed* the
reference design.

---

## D-PLATFORM — Apple Silicon only, deliberately

MLX (the entire inference stack: mlx-lm, mlx-whisper, mlx-audio/Kokoro) is
Apple-Silicon-only, and SwiftUI is Mac-only. Cross-platform (Windows) was
considered and rejected: it would require abandoning **both** MLX (→
llama.cpp/ONNX with CPU/CUDA/DirectML, different models, all of D9 redone)
**and** Swift (→ Electron/Tauri). That is a different, ~2–3× larger project
with a hard PC-hardware-variance support problem. Mac-only keeps the whole
reasoned design intact and the scope coherent.

If Windows ever matters: a *separate future port*, not a compromise to the
Mac app now.

---

## D-DISTRIB — Direct download, App Store abandoned

Distribution is Developer ID + notarization, sold direct (Paddle/Stripe),
updated via Sparkle. The Mac App Store is not a target.

Consequence — this is the whole reason the architecture below is viable: the
App Store forces App Sandbox, which makes a bundled Python interpreter +
MLX subprocess painful (per-dylib signing, library-validation entitlements
that draw review scrutiny, no writable paths where Python expects them).
Dropping the App Store removes all of it, so a Python backend is fine.

---

## D-FRONTEND — Native SwiftUI frontend + Python backend (inverts old D7)

The reference D7 said "Python backend + browser frontend now, native Swift
(replacing Python) later." That is superseded. The permanent architecture is
**native SwiftUI frontend talking to a Python/MLX backend over a local
WebSocket.**

Reasoning:
- A hybrid where Swift is only the *frontend* does not fix the App Store
  sandbox problem (that's a *backend* Python problem) — but with the App
  Store abandoned (D-DISTRIB), that problem no longer exists, so keeping
  Python for the ML is the fast, sensible path.
- "Browser" in the old design never meant the internet — it was a localhost
  web UI chosen for free `getUserMedia` AEC. SwiftUI replaces it and gets AEC
  natively via `AVAudioEngine` `setVoiceProcessingEnabled(true)` — a strict
  upgrade to the audio path (resolves the reference D6 instead of working
  around it).
- Swift owns audio I/O + AEC + UI + EventKit; Python owns VAD/STT/LLM/TTS/
  memory. The reference `server.py` WebSocket protocol survives; only the
  *client* changes from browser to Swift.

---

## D-HOME — Reactive home screen = tool-calling made visual

The home screen is the reference ROADMAP Phase 2 (tool-calling loop) with a
SwiftUI face. The LLM emits structured tool calls; the app renders them:

- `setReminder` / `addEvent` → write real macOS entries via **EventKit**;
  mirror as home-screen cards.
- `showPanel` → a persistent visual card the model raises when a long-lived
  visual helps; user dismisses with ✕.
- `setFact` → writes to the memory facts table.

Tool results join the **volatile block, last in the prompt** (reference D4),
so they never invalidate the prefix cache. A voice orb reflects VAD state
(listening / thinking / speaking) + amplitude — the latency tape made
ambient.

---

## D-PYTHON-JOB — Employable ML Python lives in `training/`, not the app

The app's Python is inference/orchestration glue, which is not the "AI
Python" employers hire for. The hireable work (QLoRA fine-tuning, dataset
building, held-out evals — reference Phase 4) lives in a **separate
`training/` pipeline**, kept distinct from the shipped app.

---

## D-LATENCY — STT model reversed to small.en (measured, not assumed)

Reference D9 picked whisper-large-v3-turbo for accuracy, assuming "STT is fast."
Measured on this Mac it was **2.2 s** — the dominant turn latency by far, because
Whisper pads every clip to a 30 s window so cost is ~constant regardless of
utterance length. Benchmarked alternatives on a clean clip (all transcribed it
correctly): turbo 2291 ms, turbo-q4 2446 ms, **small.en 300 ms**, base.en 92 ms,
tiny.en 54 ms. Switched to `whisper-small.en-mlx`: 7x faster for no visible
accuracy loss on conversational English. base.en is the lever if we want more.

Also this pass: END_SILENCE_MS 500→350 (turn-taking), first clause 30→18 chars,
and startup warmup of all three models (moves cold-start off turn 1). Net
reply-after-you-stop ≈ 3.5 s → 1.3 s.
