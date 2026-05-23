# Fennel

A local speech-to-speech companion for Apple Silicon Macs. Text or voice,
running entirely on-device. No account, no network required after the
first model download. 

## Architecture — two local processes

UI is a **native SwiftUI app**. It owns audio I/O and echo cancellation; Python owns all the ML. They talk over a local WebSocket — fully offline, nothing leaves the machine.

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

## Platform & distribution

- **Apple Silicon macOS only.** MLX and SwiftUI are both Apple-only.
- **Direct download only** 

## Layout

```
README.md                  you are here
docs/DECISIONS.md          why things are the way they are
backend/                   Python + MLX
  server.py                WebSocket server, model bootstrap
  config.py                tunables + RAM tiering
  voice/
    vad.py  stt.py  llm.py  tts.py  session.py  memory.py  vision.py
  requirements.txt
app/                       Swift package / Xcode project (SwiftUI)
  Sources/…                audio engine, WS client, home UI, EventKit
```

## Running it

```bash
./scripts/setup-venv.sh                       # one-time: uv + Python 3.12 + deps
backend/.venv/bin/python backend/server.py    # loads Qwen (first run downloads ~2.3 GB)
```
Then, in another terminal:
```bash
cd app && swift run                           # SwiftUI app; type to chat locally
```

## Licence

Fennel is **GPL-3.0-or-later** — see `LICENSE`.

If you distribute a built Fennel.app, you must make this source available to
whoever receives it (GPL-3.0 §6).

Full component list, roles and licences: [`THIRD-PARTY.md`](THIRD-PARTY.md),
also readable in the app under Settings → Licences.
