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
- **Direct download only.** Developer ID + notarisation; not the App Store.

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

For development, the two processes are started separately:

```bash
./scripts/setup-venv.sh                       # one-time: uv + Python 3.12 + deps
backend/.venv/bin/python backend/server.py    # loads models (first run downloads ~3.5 GB)
```
Then, in another terminal:
```bash
cd app && swift run                           # SwiftUI app; type to chat locally
```

The app falls back to whatever backend is already listening on port 8420, so a
hand-started server and `swift run` keep working side by side.

## Building the app

`bundle-app.sh` produces a self-contained `Fennel.app` — the Python runtime and
the backend live inside the bundle and the app starts the backend itself, so
opening it is the whole install. It also writes a `.dmg` to hand out.

```bash
./scripts/bundle-app.sh                       # -> dist/Fennel.app + dist/Fennel.dmg
```

Takes about a minute, most of it copying the 1.4 GB runtime. Two notes:

- **Quit the running app first** (`pkill -f "Fennel.app/Contents/MacOS/Fennel"`),
  or you will bundle while the old copy still holds files.
- **The icon is not rebuilt automatically.** After changing the geometry in
  `FennelMark.swift`, regenerate it from those same coordinates first:
  ```bash
  swift scripts/make-icon.swift app/Resources
  ```

Models are deliberately *not* bundled: they are ~3.5 GB and would more than
triple the download, so the app fetches them on first launch — after asking.

### Building one other people can open

Ad-hoc signing is the default and is fine locally, but Gatekeeper will refuse it
on anyone else's Mac. A distributable build needs a Developer ID certificate
(a paid Apple Developer account) and notarisation:

```bash
DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)" ./scripts/bundle-app.sh
./scripts/notarize.sh                         # submits, waits, staples
```

One-time notarisation setup:

```bash
xcrun notarytool store-credentials fennel \
  --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC_PASSWORD
```

`bundle-app.sh` prints `NOT distributable yet` whenever it signed ad-hoc, so it
is always obvious which kind of build you are holding.

## Licence

Fennel is **GPL-3.0-or-later** — see `LICENSE`.

If you distribute a built Fennel.app, you must make this source available to
whoever receives it (GPL-3.0 §6).

Full component list, roles and licences: [`THIRD-PARTY.md`](THIRD-PARTY.md),
also readable in the app under Settings → Licences.
