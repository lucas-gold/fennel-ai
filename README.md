# Fennel

A local text and speech-to-speech AI companion for Apple Silicon Macs. Running entirely on-device. No account, and no network needed after the first model download.

## Architecture — two local processes

A native SwiftUI app owns audio I/O and echo cancellation; Python owns the ML.
They talk over a WebSocket on 127.0.0.1.

```
┌────────────────────────────┐          ┌─────────────────────────────┐
│  SwiftUI app               │  16 kHz  │  Python backend             │
│                            │  int16   │                             │
│  AVAudioEngine + AEC       ├─frames──►│  vad  →  stt  →  llm  →  tts│
│  mic capture / playback    │◄─24 kHz──┤  Silero whisper mlx-lm Kokoro│
│  chat · cards · voice orb  │   PCM    │                             │
│  EventKit (reminders/cal)  │◄─tools───┤  memory (SQLite + FTS5)     │
└────────────────────────────┘  local   └─────────────────────────────┘
                                  WS
```

- **Swift** — audio capture and echo cancellation, playback, all UI, and
  EventKit for real Reminders and Calendar entries.
- **Python** — VAD endpointing, speech recognition, the language model with
  prefix-cache reuse, speech synthesis, memory, and image generation.

Audio in is 512-sample int16 frames at 16 kHz; audio out is a `>II` header
(turn, seq) followed by int16 PCM at 24 kHz. Control frames are JSON — see
[`backend/protocol.py`](backend/protocol.py) for every message shape.

## Models

The LLM is chosen at launch, from Light (Qwen3 1.7B, 1.0 GB) up to Agent
(Hermes 4 14B, 8.3 GB), or any MLX repo from Hugging Face pasted in by hand.
One runs at a time. Speech recognition, synthesis and embeddings are fixed and
add about 1.2 GB; image generation is optional and adds 4.6 GB.

Models are downloaded on first use rather than bundled, after the app asks.
`scripts/vet-models.py` checks a model loads, holds a conversation and renders
the tool definitions before it is added to the list in `config.py`.

## Platform

- **Apple Silicon (M1 or later), macOS 14 or later.** MLX and SwiftUI are both Apple-only.

## Layout

```
backend/                   Python + MLX
  server.py                WebSocket server, model loading, the picker
  config.py                tunables, the model registry, the persona
  protocol.py              every control frame, both directions
  voice/
    session.py             the turn: endpoint → STT → LLM → clauses → TTS
    llm.py                 mlx-lm streaming and the primed prefix
    tools.py               tool schemas, the stream splitter, normalisation
    store.py memory.py     SQLite, recall, rolling summaries
    setup.py images.py     downloads, model probing, image generation
    vad.py stt.py tts.py   Silero, Whisper, Kokoro
app/                       SwiftUI package
  Sources/myai/            audio engine, WS client, chat, cards, model picker
scripts/                   build, bundle, notarise, vet models
```

## Running it

In development the two processes start separately:

```bash
./scripts/setup-venv.sh                       # one-time: uv + Python 3.12 + deps
backend/.venv/bin/python backend/server.py    # loads models
```

Then, in another terminal:

```bash
cd app && swift run
```

The app falls back to whatever backend is already listening on port 8420, so a
hand-started server and `swift run` work side by side.

## Building the app

`bundle-app.sh` produces a self-contained `Fennel.app` with the Python runtime
and backend inside it, so opening the app is the whole install. It also writes
a `.dmg`.

```bash
./scripts/bundle-app.sh                       # -> dist/Fennel.app + dist/Fennel.dmg
```

Takes about a minute, most of it copying the runtime. Two notes:

- `bundle-app.sh` prints `NOT distributable yet` whenever it signed ad-hoc, so it
  is always clear which kind of build you are holding. (Right-click -> open required)
- **Quit the running app first** (`pkill -f "Fennel.app/Contents/MacOS/Fennel"`),
  or you will bundle while the old copy still holds files.
- **The icon is not rebuilt automatically.** After changing the geometry in
  `FennelMark.swift`:
  ```bash
  swift scripts/make-icon.swift app/Resources
  ```

## Licence

Fennel is **GPL-3.0-or-later** — see [`LICENSE`](LICENSE).

If you distribute a built Fennel.app you must make this source available to
whoever receives it (GPL-3.0 §6).

Full component list, roles and licences: [`THIRD-PARTY.md`](THIRD-PARTY.md),
also readable in the app under Settings → Licences.
