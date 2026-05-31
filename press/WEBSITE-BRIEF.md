# Fennel — brief for building the website

Everything here is verified against the shipping build. Where a number was
measured, the measurement is given; where something is **not yet true**, it says
so. Please don't upgrade the hedged claims into confident ones.

---

## 1. What it is

**Fennel is a voice companion for Apple Silicon Macs that runs entirely on your
own machine.** You talk to it, it talks back, and nothing you say leaves the
computer.

Product name: **Fennel**. Site/brand: **Fennel Garden** (`fennel.garden`).
The assistant refers to itself as Fennel.

One-line options, in descending order of how well they hold up:
- "A voice companion that never leaves your Mac."
- "Talk to it. It answers. Nothing is uploaded."
- "Local-first voice AI for Apple Silicon."

It is **free** and **open source (GPL-3.0-or-later)**.

---

## 2. Who it's for

People who want a conversational assistant but are not comfortable with a
microphone that streams to a datacenter. The pitch is not "cheaper than the
cloud" or "smarter than the cloud" — it is **yours**. It works on a plane, in a
basement, with the Wi-Fi off.

---

## 3. What it does

**Talk or type.** Tap the orb and speak, or type in the chat. It replies in
voice (Kokoro TTS) and text. You can interrupt it mid-sentence and it stops.

**Thirteen tools it can actually use**, grouped as they'd read on a page:

| Group | Tools |
|---|---|
| Your day | `set_reminder`, `add_event`, `agenda` (reads your Reminders + Calendar back), `set_timer` |
| Looking things up | `search_wikipedia` (free, no key), `search_web` (optional, your own Ollama key) |
| Your Mac | `open_app`, `run_shortcut`, `create_shortcut` (builds a real macOS Shortcut you approve), `open_link` |
| In conversation | `show_panel`, `recommend_song`, `set_fact` (durable memory) |

Reminders and calendar events are **real EventKit entries** — they show up in
Apple's Reminders and Calendar. Dismissing the card deletes the real entry, with
a six-second undo.

**It remembers.** Facts you tell it persist across conversations and restarts.
Chats are saved, resumable, and you can keep several.

**Optional daily briefing.** Off by default. Once a day it can fetch weather
(Open-Meteo) and headlines (BBC World, NPR, Ars Technica) so it knows what's
happening. Fixed source list — the request reveals nothing about you.

---

## 4. What actually makes it different

This is the section worth spending real estate on. Three things:

**1. Genuinely local, and precise about the exception.** Most "private AI"
marketing is vague. Fennel's claim is checkable: the only network requests it
ever makes are (a) the one-time model download, which it asks permission for
before making, (b) the daily briefing if you switch it on, and (c) Wikipedia or
web lookups if you switch those on. Each is a separate switch. With all of them
off, it makes zero requests — and the app says so in the settings panel, listing
the exact services by name.

**2. Built for conversation speed, not benchmark scores.** The whole system is
engineered around latency. Some of what that meant in practice:
- The unchanging part of the prompt is prefilled once and **cached to disk**,
  so startup restores it in **0.03 s** instead of recomputing for 14.6 s.
- Replies are cut into clauses and spoken as they generate, with the first
  fragment deliberately short so audio starts sooner.
- Barge-in cancels generation, playback and queued audio the moment you speak.

**3. It does real things, not just chat.** Reminders that appear in Reminders.
Shortcuts you can actually run. A timer that chimes whether or not you're
looking at the window.

---

## 5. Tech stack

Two local processes talking over a WebSocket on `127.0.0.1`.

**Frontend — native SwiftUI (macOS 14+)**
Audio capture and playback via `AVAudioEngine` with voice-processing I/O for
echo cancellation; EventKit for Reminders and Calendar; Keychain for the
optional API key.

**Backend — Python 3.12 + Apple MLX**

| Role | Model |
|---|---|
| Conversation | Qwen3-4B-Instruct-2507 (4-bit) |
| Speech recognition | Whisper small.en |
| Speech synthesis | Kokoro-82M (voice `af_heart`) |
| Voice activity detection | Silero VAD v5 (ONNX) |
| Memory + retrieval | bge-small-en-v1.5 |

Storage is SQLite with FTS5 full-text search plus vector embeddings for
semantic recall. The BERT encoder for embeddings is **hand-written against MLX**
(~120 lines) rather than pulled from a library, and validated against a
PyTorch reference to within 2e-7.

Everything ships inside the app bundle — a relocatable CPython, the ML stack,
and the backend source. Opening the app is the entire install.

---

## 6. Numbers you can quote

Measured on an M2 MacBook Air. **State the hardware** — these are honest
mid-range numbers, not a fast machine.

| | |
|---|---|
| Time to first spoken audio | ~1.5 s |
| Time to first token | ~0.6 s |
| Startup (models already downloaded) | ~17 s |
| Prompt cache restore | 0.03 s (vs 14.6 s to recompute) |
| Memory while running | ~3.5 GB |
| Download | 562 MB, plus ~3.5 GB of models on first launch |

Please don't round these into "instant" or "blazing fast". The honest framing is
"fast enough to talk to", which is the actual design goal.

---

## 7. Requirements and the install caveat

- **Apple Silicon Mac** (M1 or later). No Intel support — MLX is Apple-Silicon-only.
- **macOS 14 or later.**
- ~5 GB free disk, 16 GB RAM comfortable.
- Internet needed **once**, for the model download.

**⚠️ Not yet notarised.** The current build is ad-hoc signed, so macOS Gatekeeper
will refuse to open it on anyone else's Mac. Until an Apple Developer ID is set
up and the DMG is notarised, the site must either (a) not go live with a
download link, or (b) carry clear instructions for right-click → Open. Do not
write copy promising a clean one-click install until this is resolved — it would
be the first thing every visitor discovers is untrue.

---

## 8. Branding

**Logomark** — a fennel bulb: squat and ribbed, with a feathery spray of fronds.
`press/fennel-mark.svg` uses `currentColor`, so it takes the colour of its
context. `press/fennel-mark-512.png` and `press/fennel-icon-512.png` (the app
icon, white mark on the gradient tile) are also provided.

Two things about the mark that matter if it gets redrawn:
- The bulb is **wider than tall**. A round bulb on a straight stem reads as a
  Venus symbol — that was the first draft and it had to be fixed.
- There must be **several fronds**. A simplified one-stalk version tested *worse*
  at small sizes because it collapsed back into a symbol shape.

**Colour** — one accent gradient, used sparingly:
- `#6B70FA` → `#AB66F2` (135°, top-left to bottom-right)
- In the app it is reserved for the voice orb and the user's own chat bubbles;
  everything else stays neutral. A site that uses it everywhere will feel unlike
  the product.
- The app is dark-first. Backgrounds are near-black with translucent panels.

**Typography** — the app uses SF Pro, with SF Pro Rounded for headings. On the
web, `-apple-system` / `system-ui` with `ui-rounded` for headings gets close and
keeps it feeling native.

**Voice and tone** — calm, plain, specific. The app's own copy is the reference:
"Everything else in Fennel runs on this Mac." "Free, and it sends your search
terms — and nothing else — to Wikipedia." No exclamation marks, no "revolutionary",
no AI-hype vocabulary. Understatement is the brand.

---

## 9. Screenshots — you need to take these

I couldn't capture them (no screen-recording permission). Suggested set, in
order of usefulness for a landing page:

1. **The full window mid-conversation** — orb on the left, a real exchange on
   the right. The hero shot.
2. **The orb while listening** — it glows and swells with your voice.
3. **Cards on the home panel** — ask for a timer, a reminder and a Wikipedia
   lookup so three different card types are visible at once.
4. **The network settings popover** — this is the privacy story in one image:
   the switches, the "runs on this Mac" line, and the named services.
5. **The first-run consent screen** — asking permission before downloading.
   Reinforces the same point.

Dark mode, window at a comfortable size, and use a conversation that reads well —
whatever's on screen becomes marketing copy.

---

## 10. Suggested page structure

Apple-style: one idea per section, generous whitespace, a real screenshot for
each claim.

1. **Hero** — logomark, "Fennel", one line, download button, "Free and open
   source · Apple Silicon · macOS 14+". Hero screenshot underneath.
2. **The privacy claim**, stated precisely, with the settings screenshot.
3. **Talk to it** — the voice loop, barge-in, the orb.
4. **It does things** — the tool groups, with the cards screenshot.
5. **It remembers** — facts and saved chats.
6. **What's inside** — the model table. Technical readers will look for this and
   it builds credibility with everyone else.
7. **Download** — size, requirements, the Gatekeeper note while it stands.
8. **Footer** — GPL-3.0, link to source, third-party attributions (Wikipedia
   CC BY-SA, Open-Meteo CC BY 4.0, the RSS sources, Ollama).

---

## 11. Claims that are true, and claims that are not

**True, say freely:**
- Runs entirely on your Mac; no account, no subscription, no telemetry.
- Free and open source under GPL-3.0.
- Creates real reminders, calendar events and Shortcuts.
- Works offline once set up.
- Asks permission before its one download.

**Not true — do not write:**
- "Never connects to the internet." It downloads models once, and the optional
  features connect when enabled.
- "As capable as ChatGPT/Claude." It is a 4B model. It is good company and good
  at its tools; it is not a frontier model, and overselling this will be
  obvious within a minute of use.
- "Controls your smart home." It cannot. It can run a Shortcut you already have.
- "Instant." See the numbers above.
- Anything about iPhone, iPad or Windows. Mac only.

---

## 12. Assets in this folder

```
press/fennel-mark.svg        the logomark, currentColor, 120×120 viewBox
press/fennel-mark-512.png    rendered mark, transparent
press/fennel-icon-512.png    the app icon (white mark on gradient tile)
press/WEBSITE-BRIEF.md       this document
```

Screenshots are still needed from the user — see §9.
