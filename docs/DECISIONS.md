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

---

## D-TOOLS — Native Hermes tool calls, split out of the stream

Tools use the model's own `<tool_call>` format via the tokenizer's `tools=`
argument, not a hand-rolled `⟦tool:…⟧` syntax. Qwen3 was trained on it, so
reliability comes free, and the template puts the signatures in the **system
block** — the stable prefix — so tool calling costs one prefill per session
rather than one per turn (D4).

Three consequences worth writing down:

- **Tool syntax must never reach TTS.** `ToolStream` splits the stream into
  prose and call blocks, holding back any trailing partial `<tool_ca…` so a
  half-arrived tag is never spoken. Calls fire the instant they close, so the
  home card appears *while the model is still talking* — the card, not the
  sentence, is the real feedback (measured: card at 1.7 s, speech at 2.7 s).
- **History stores the generation verbatim**, tags and all, instead of a
  structured `tool_calls` field. Re-rendering structured calls re-tokenizes
  with different whitespace and silently costs a re-prefill.
- **Skip the follow-up round when the model already spoke.** Qwen puts its call
  last, so prose in that pass was already a confirmation; saying it twice is
  the most annoying failure mode in a voice UI, and skipping removes a whole
  generation from the critical path. A *failed* tool still gets its round, so
  the model can correct itself out loud.

The real side effect lives in Swift (`EventKitBridge`), never in Python. The
backend only normalizes arguments into absolute local times; the app writes to
EventKit and reports back as `tool_result`, and the backend waits up to
`TOOL_APP_TIMEOUT_S` for that verdict — so "I couldn't, Reminders access is
off" is something the model actually knows rather than guesses.

---

## D-PREFIX — Prime the stable prefix at startup; the clock rides last

Adding tool schemas grew the prefix from ~25 to ~940 tokens. This M2 prefills at
only ~200 tok/s, so the first turn of every session suddenly cost **4.7 s** of
silence. Two fixes, both straight applications of D4:

1. `LLM.prime()` prefills that prefix once during startup warmup, and `reset()`
   now *trims back to* it instead of discarding it — so a new session (or a
   barge-in cache reset) keeps it. First turn: **4.88 s → 0.40 s**.
2. Priming only works if the prefix is byte-identical every session, so the
   system prompt can no longer contain the current time. It carries the date
   plus a 7-day table (stable all day); the **clock rides on each user message**
   — volatile content last, exactly where D4 wants it, and a past turn's stamp
   never changes so history stays cacheable. (Stage 4 widened that prefix into
   the `<context>` block that also carries recall — see D-MEMORY.)

The day table is not padding: a 4-bit 4B model handles clock arithmetic fine but
miscounts weekdays ("next Wednesday" landed five days late). Looking a date up
beats computing it.

---

## D-MIC — The engine has modes, because "tap to talk" has to be literal

The first audio engine started at launch with the voice-processing tap installed
and `startListening()` merely gated whether frames were *sent*. The mic was
therefore open — and the macOS recording indicator lit — for the entire session,
which is indefensible for an app whose pitch is that nothing leaves your Mac.

Fixed by giving `AudioEngine` three modes (`off` / `playback` / `voice`) and
building a **brand-new `AVAudioEngine` on every switch**. That looks wasteful and
isn't: disabling voice processing on a reused input node leaves the input stream
open anyway, whereas a freshly-built playback engine that never touches
`inputNode` is *provably* not recording. Playback still shares the engine with
capture while listening, so the AEC reference (D6) is intact.

Closing the mic waits for the player queue to drain, so tapping "stop" while the
assistant is mid-sentence doesn't cut it off.

---

## D-MUSIC — Recommend by handing off, not by embedding a player

`recommend_song` renders a card with Apple Music and Spotify **search links**.
No SDK, no OAuth, no bundled player: embedding either would mean an account,
network access on a launch path, and licence terms that don't fit a paid
Apache-2.0/MIT product.

Search links (not track IDs) are also the honest choice given the model: a local
4B will confidently invent a plausible-sounding title, and a search lands
somewhere useful anyway where a dead track ID would not. The links are buttons
the user presses — the app itself still never reaches the network.

---

## D-MEMORY — Three inputs, three depths, chosen by how often each changes

Memory is not one blob prepended to the prompt. Each input sits at the depth
that matches its volatility, because with prefix caching (D4) depth *is* cost —
anything placed above a change forces everything below it to re-prefill:

| input | where | re-prefills |
|---|---|---|
| tool schemas, persona, day table | primed system prefix | once per day |
| facts + rolling summary | `<context>` message above the window | only when the window is rebuilt |
| verbatim turns | the window | on chunked eviction |
| FTS recall + clock | glued to the current user message | never — that position is new anyway |

The middle row is the interesting one. Facts and the summary change rarely but
not never, so putting them above the window costs a re-prefill when they change
— and that is free, because we only rebuild them *when the window is rebuilt
anyway*. Eviction is likewise chunked (drop to 1x when we exceed 2x) rather than
sliding one turn at a time, which would invalidate the whole cached window on
every single turn.

Recall is FTS5, not embeddings: it ships with Python's SQLite, needs no model,
and stays honest offline. Its limit is real and worth stating — it matches
words, so "what should I order for dinner" does not retrieve "I'm allergic to
shellfish". **Facts are what covers that gap**, since they are always present
rather than retrieved, which is why `set_fact`'s description is emphatic that
claiming to remember without calling it means not remembering.

Summarising runs through `LLM.complete` on a throwaway KV cache. Sharing the
conversation's cache would evict its prefix and make the user's next turn pay a
full re-prefill for a background chore.

---

## D-SESSIONS — Persisted in the backend; one ongoing chat in the UI

Chat history lives in the backend's SQLite, not the app, because the LLM is its
main consumer: the window, the summary and recall are all prompt inputs, so
keeping them beside the prompt builder avoids shipping conversation state across
the WebSocket every turn. The app asks only for what it draws.

The UI deliberately under-sells multiplicity. One ongoing conversation is the
default and the tab strip stays hidden until a second chat is actually open;
older chats live behind a History menu instead of accumulating as tabs. Closing
a tab and deleting a chat are kept separate — one hides, one destroys.

---

## D-BRIEFING — Freshness is a prefix problem, not a training problem

The model's weights are frozen and stay frozen. "Updating the model with today's
news" would mean fine-tuning daily: expensive, lossy, and it degrades general
ability every pass. Retrieval puts the facts in the *prompt* instead.

The insight that makes it cheap: **the daily briefing is identical all day**, so
it is prefix material, not per-turn material. It goes through the same
`LLM.prime()` path as the tool schemas (D-PREFIX) — prefilled once at startup,
then free for every turn that day. Measured:

| | prime (once) | TTFT | decode |
|---|---|---|---|
| prefix alone (1644 tok) | 8.5 s | 0.41 s | 24 tok/s |
| + briefing (2961 tok) | 15.8 s | 0.48 s | 21 tok/s |

So the briefing is **budgeted, not exhaustive**. TTFT barely moves, but decode
slows ~12% because attention runs over a longer KV cache — that is the real
price, and it is why `BRIEFING_MAX_CHARS` exists. Everything fetched still lands
in the archive, where it costs nothing until retrieved.

It is also *replaced* daily, never appended, so the prefix is the same size in
year three as on day one. Only the archive grows (~150 KB/day of vectors), and
`ARCHIVE_KEEP_DAYS` bounds that.

**The gate is the load-bearing part.** Archive retrieval injects nothing unless
the query is topically close — measured separation on real feeds: on-topic
queries score 0.55–0.66 cosine, off-topic 0.42–0.46, so the floor sits at 0.50
with a minimum query length for turns like "thanks". Declining costs zero tokens,
which is what keeps latency flat no matter how large the archive grows. FTS
refines the ranking but never gates: it returns *something* for any query at all,
so on its own it would drag noise into every prompt.

Privacy is two separate switches, not one. The daily fetch hits a fixed source
list and reveals nothing about the user; live search would send their actual
question to a third party. Weather uses a city the user types, not CoreLocation.
Default is off, and offline remains the app's normal state.

---

## D-EMBED — A BERT encoder written against MLX, rather than a dependency

Retrieval needs embeddings, and every packaged option cost more than the code
did: `mlx-embeddings` pulls mlx-vlm + opencv + uvicorn, sentence-transformers
pulls its own stack. `voice/embed.py` is ~120 lines, adds no new dependency, and
runs on the GPU beside the LLM. bge-small-en-v1.5: 33M params, 384 dims, MIT
(the licence matters — D-DISTRIB).

Validated rather than assumed: `tools/check_embed.py` checks the vectors against
transformers/torch on identical weights (cosine 1.000, max abs diff 2e-7).
Retrieval that is quietly wrong is worse than no retrieval.

Two MLX details cost real debugging time and are worth remembering:
- **`mx.linalg.norm` is CPU-stream only.** It fails on the `asyncio.to_thread`
  workers we embed from; the L2 norm is computed with `rsqrt` instead.
- **`mx.load` returns lazy arrays**, and forcing them the first time needs the
  CPU stream — which the worker threads don't have. Every weight is `mx.eval`ed
  at construction, on the main thread.

And one that was worse: **concurrent MLX use from two threads crashes natively**,
no Python traceback, the process simply exits. Re-priming when the briefing lands
raced with a live generation. `LLM` now holds a re-entrant lock across
`stream_reply`, `complete` and `prime`.

---

## D-GATE — Every retrieval path needs a floor, including the old one

D-BRIEFING gated *news* retrieval on a measured cosine floor and left
conversation recall on raw FTS5. That was inconsistent, and the inconsistency
was expensive. FTS returns *something* for any query, so recall was injecting
"how are you" and "what else is new" into unrelated turns:

| turn | context block before | after |
|---|---|---|
| "tell me a joke" | 60 tok → 0.72 s TTFT | 14 tok → **0.34 s** |
| "what's the weather like in Toronto" | 86 tok → 0.87 s | 14 tok → **0.34 s** |
| "what's going on with Iran" | 201 tok → 1.42 s | 127 tok → **1.05 s** |

Messages are embedded on write (~5 ms) and recalled through the same
`gated_top_k` as news, at a *higher* floor (0.62 vs 0.58) because most turns
genuinely have no relevant past. The lesson generalises: **a retrieval path
without a relevance floor is a latency leak and a correctness leak at once** —
it pays prefill on every turn to feed the model noise.

---

## D-DUPLEX — Echo cancellation is not enough on its own

The assistant was interrupting itself through the speakers. Voice processing
cancels most of the feedback, but "most" fails badly when the penalty is the
assistant cutting itself off. Two fixes:

- While its own audio is audible, barge-in needs a much higher probability
  (0.85 vs 0.5) *sustained* over ~200 ms. Leaked echo is intermittent and
  doesn't clear that bar; a real interruption does, in well under a second.
- The guard now covers the whole time audio is *playing*, not just while the
  backend is generating. The app buffers whole clauses, so playback outlives
  generation by seconds — `_assistant_active` alone left the tail unprotected,
  which is exactly when self-interruption was happening.

`[audio]` now logs whether voice processing actually engaged, because a silent
AEC failure otherwise looks identical to a tuning problem.

---

## D-SEARCH — Wikipedia, and the honesty about what that is

Two corrections to what I said when deferring this.

**Qwen has no built-in internet access.** Tool calling is a *format* — the model
emits a request and something has to implement it. There is no hidden capability
to switch on.

**"No free search API" was too pessimistic.** There is no free *general web*
search, but Wikipedia's API is free, keyless, quota-free, and covers the large
class of questions a 4-bit 4B model gets wrong or is out of date on. One request
with `generator=search` + `prop=extracts` returns ranked hits and their intro
text together, in 0.2–0.6 s — fast enough that the "let me look that up" padding
is politeness rather than necessity. Content is CC BY-SA, so the source and link
travel with the answer.

It is not general web search: no local businesses, no breaking news (that is what
the daily briefing is for). Its own setting, separate from daily updates, because
it sends the user's actual question to a third party.

**A read tool must never skip the follow-up round.** D-TOOLS skips the second
generation when the model already spoke, which is right when prose *is* the
confirmation. For a tool whose result is the answer it is catastrophic: the first
search turn said "Let me look that up." and then ended. `ANSWERING_TOOLS` marks
the tools whose results must be read back.

---

## D-IDLE — Maintenance belongs in the pauses, not on the turn boundary

Two background jobs hold the LLM lock for seconds: the rolling summary, and
trimming the verbatim window (which changes the prompt at the front, so the next
turn pays a full re-prefill). Both were firing the instant a turn ended —
precisely when the user is most likely to speak next.

Measured over 22 turns: 21 of them at 0.37–0.45 s and **one at 2.20 s**, the
window trim. That single spike is what "sometimes it takes a second" actually is;
an average would have hidden it completely, which is why the benchmark prints
every turn rather than a mean.

Both now wait for `SUMMARY_IDLE_S` of quiet, and `_start_turn` cancels them if
the user comes back first. The trim additionally pre-warms the rebuilt prompt via
`LLM.warm()` — prefill without generation, and without the assistant header,
since the next real turn appends a user message before it.

The general rule: **anything that takes the model's lock must run in a pause and
be cancellable.** A background chore that blocks a reply is worse than no chore.

---

## D-MAC — Delegate to Shortcuts instead of integrating each service

HomeKit has no macOS framework available to third-party apps, so lights and
scenes cannot be driven directly. But Shortcuts *can* drive them — along with
sending messages, controlling media, and whatever else the user has built.

`run_shortcut` therefore delegates to the user's own Shortcuts library rather
than integrating services one at a time. One tool reaches every automation they
already own, the blast radius stays inside something they authored and can
inspect, and it needs no new entitlement per service. On a miss it returns the
available names so the assistant can say what *does* exist.

`open_app` is the same shape and resolves by bundle id first, then the usual
install directories, since `NSWorkspace` can't find everything by name alone.

Deliberately NOT auto-sending messages or email. Those are irreversible and
outward-facing; the right pattern is the one `open_link` already uses — put it
on screen and let the user press the button.

---

## D-AUTHOR — Creating Shortcuts: a fixed vocabulary, and the user presses Add

There is no `shortcuts create`, but `shortcuts sign` takes an *input* file — so a
shortcut can be authored as a plist, signed with Apple's own tool, and handed
over. Opening the signed file makes Shortcuts show its Add sheet with every
action listed, so nothing reaches the user's library without their approval.
That confirmation is what makes this safe to expose at all; the app never writes
to their library directly.

The real constraint is not signing, it is the action vocabulary. Shortcuts has
hundreds of actions with undocumented identifiers and parameter shapes, and a
4-bit 4B model asked to invent them produces plausible nonsense that fails
silently on the user's machine. So the model does not author actions — it picks
from eight known-good step types (`say`, `notify`, `show`, `open_url`, `wait`,
`set_volume`, `set_brightness`, `comment`), and anything outside that is rejected
before a file is written. Verified both directions: it built "Wind Down"
correctly, and declined "a shortcut that orders me a pizza" instead of inventing
a step.

Gotcha worth recording: `shortcuts sign` requires the **input** file to end in
`.shortcut`. Any other name fails with "isn't in the correct format", which reads
like a malformed plist and is not.

---

## D-NOKEY — Why search stays on Wikipedia, and what Ollama actually is

Ollama's web search was considered as a way to get general web search. It does
not fit, for one reason: it is a **hosted API that requires an account and key**
— their docs say "For access to Ollama's web search API, create an API key. A
free Ollama account is required."

Worth separating two things that share a name: Ollama-the-runtime runs models
locally for free; Ollama-the-web-search is a cloud service on their servers. The
"free and local" reputation belongs to the first and does not transfer to the
second.

So it cannot ship keyless. The options it leaves are the same ones every search
provider leaves:

1. every user makes an account and pastes a key — friction, but honest;
2. embed our key — our quota and our bill for every customer, extractable from
   the app bundle, and against the point of a local-first product.

The genuinely keyless options remain Wikipedia (shipped) and self-hosted SearXNG
(which means asking the user to run a server). Wikipedia stays the default; a
bring-your-own-key field is the upgrade path if general search is ever wanted,
and it works for Ollama, Brave or Tavily equally since they are all the same
shape.

---

## D-REPLAY — History must replay what the model did, not what the user saw

Stage 4 stored only the *visible* reply text for each assistant turn. That looked
right — it is what the chat shows — and it silently broke tool calling.

The failure is self-reinforcing and took a while to see. A tool turn generates
`<tool_call>…</tool_call>` plus prose; only the prose was stored. So when a
session was resumed (or the window rebuilt), the model was replayed dozens of
examples of *"user asks for a reminder → assistant describes a reminder in
prose, no tool call"* — a near-perfect few-shot demonstration of faking it. It
learned from its own laundered transcript. By the time the real conversation had
forty turns it had stopped calling tools almost entirely, while still cheerfully
narrating what it had "done":

> user: create a reminder to buy eggs tomorrow
> assistant: I'll set a reminder to buy eggs tomorrow. Here's the plan: …

Nothing in the logs said anything was wrong, because from the backend's point of
view the model simply chose not to call a tool.

Fix: `messages` carries `content` (what the UI shows) *and* `prompt_text` (what
the model is replayed) separately. A turn now persists every message it produced
— assistant generations with their tool calls intact, and the `tool` result
messages after them — with only the last assistant row carrying visible text.
Verified by the test that matters: tool turns, disconnect, reconnect, tool turns
again. 5/5, where the resumed half was the part that used to fail.

The general lesson: **the transcript shown to the user and the transcript
replayed to the model are different artefacts.** Conflating them means the model
trains itself, every session, on a version of events that never happened.

A belt-and-braces guard went into the system prompt too ("never say you have
done something unless you called the tool for it this turn"), because existing
conversations still carry the poisoned history.

**Two follow-ups, both learned the hard way.**

*Legacy rows are worse than useless as demonstrations.* Conversations written
before this fix have no `prompt_text`, so their assistant rows are laundered by
definition. `window()` now replays those as `(earlier reply)` — the user still
sees the real text in the chat, only the model's replay is elided. Isolated on a
real 79-message conversation, same question each time:

| replayed window | result |
|---|---|
| full history, 16 legacy messages | fabricated a reminder |
| no history | called the tool |
| history with assistant turns stubbed | called the tool |

*A contaminated conversation cannot be repaired.* Once the model has fabricated,
that fabrication is stored **faithfully** — it really is what it said — and no
format-based rule can distinguish it from legitimate history. The session then
sustains its own bad behaviour. The fix prevents new conversations from being
poisoned; it cannot rescue one that already is. Those have to be abandoned.

That asymmetry is the part worth remembering: a context bug that writes to
durable storage is not a bug you can simply fix, because the corrupted data keeps
teaching after the code is correct.

---

## D-TIMER — A silent parse failure, and why the chime moved out of the view

The timer card rendered its label and then did nothing. Two causes, both worth
recording because both are the quiet kind.

**Python's `isoformat()` includes microseconds.** The backend sent
`2026-08-25T00:33:16.995174`; the app's `parseDate` only accepted whole seconds,
returned nil, and the countdown was simply never built. Nothing logged, nothing
threw — the card just looked inert. Fixed at both ends: the backend truncates to
whole seconds, and the parser accepts fractional ones anyway. A parser that
fails silently on a format its own producer emits is a bug waiting for someone
to notice.

**The chime lived in the countdown view.** Cards sit in a `LazyVStack`, so the
view may not exist when the timer expires — a timer that only goes off while you
happen to be looking at it is not a timer. `ChatModel.startTimer` now owns it:
sleep until the deadline, then beep and bounce the Dock icon, regardless of what
is on screen. The view is display only.

**Alarms are not possible.** macOS exposes no alarm-clock API to third-party
apps, and Clock.app is not scriptable for it. A request for an alarm is handled
as a reminder at that time, which does fire a notification (`EKAlarm`), and the
model is told to say that is what it set rather than claim an alarm.

---

## D-ECHO — Three layers, because acoustics don't respect any single one

The assistant kept answering itself. There are now three defences, deliberately
independent, because each one fails differently:

1. **Voice-processing AEC** (D6) cancels most of the speaker feed at the driver.
2. **A stricter VAD gate** (D-DUPLEX) requires sustained high-confidence speech
   while our own audio is audible, so residual leakage doesn't trip barge-in.
3. **A transcript check** — the new one. If a turn was captured while we were
   speaking, its transcript is compared against what we actually said; if it is
   mostly our own words, the turn is discarded before it reaches the LLM.

The third exists because the first two are acoustic and probabilistic, while
this one is semantic and nearly free: we know exactly what we sent to TTS. Word
overlap rather than exact match, because STT mangles re-recorded audio, and the
bar scales with length — a three-word fragment needs ~total overlap so a genuine
one-word reply survives, while longer transcripts can be looser, since the user
is unlikely to independently produce eight of our words in a row. Verified
against eight cases including "mom" (ours, dropped) and "thanks" (not ours,
kept).

It only runs for audio captured while our voice was actually playing, so normal
conversation is never second-guessed.

---

## D-NAME — Fennel

The app is **Fennel**; the assistant answers to Fennel; the site is
**fennel.garden**. One word for the thing you talk to, the longer phrase for the
place it lives.

Consequences worth recording:

- Bundle id moved to `garden.fennel.app`, which macOS treats as a **new app** —
  microphone, Reminders and Calendar permissions are re-requested once.
- The data directory moved to `~/Library/Application Support/Fennel`. `Store`
  adopts a pre-rename database on first run, WAL sidecars included, rather than
  silently starting empty.
- The logomark is a SwiftUI `Shape`, not an asset, and `scripts/make-icon.swift`
  renders the `.icns` from the same geometry — one source of truth, so the icon
  can't drift from the in-app mark.
- Naming the assistant in the persona string was not enough: buried in a
  ~2100-token system block, the model invented its own name. It needed to be the
  first sentence, with an explicit "never invent another name."

---

## D-MIGRATE — I deleted the user's database. What went wrong, and the rule.

The rename to Fennel shipped with a migration that carried the old database to
the new location. It destroyed the data instead. Both mistakes are mine and both
are the ordinary kind:

**1. It moved instead of copying.** `os.replace` on the legacy file, so there was
no fallback the moment anything downstream went wrong.

**2. It was aimed by a mutable global.** The trigger was `path == DB_PATH`. Test
harnesses reassign `voice.store.DB_PATH` to a scratch file precisely so they
never touch real data — which meant the condition became true *for the scratch
path*, the migration fired into it, and the real database was moved there. The
scratch file was then deleted in routine cleanup, along with the user's ~97
messages, facts and settings. No Time Machine, no local snapshots, no copy.

The bitter part: the `DB_PATH` indirection existed specifically to keep tests off
the real database. It is what pointed the loaded gun at it.

Two rules, both now enforced in `_adopt_legacy`:

- **A migration copies. It never moves.** The original costs a few megabytes and
  buys a second chance. Use sqlite3's `backup()` so the WAL is checkpointed in.
- **Never let a destructive path be chosen by something tests are expected to
  reassign.** The real location is frozen in `_REAL_DB` at import; the migration
  compares against that and no-ops for every other path. There is a regression
  test for exactly this: pointing `DB_PATH` at a scratch file must not adopt.

---

## D-MARK — The logo, and why simplifying it made it worse

The first mark read as a Venus symbol: a round bulb, one straight central stem,
symmetric arms. Three changes fixed it, and the third was a surprise:

1. **The bulb is wider than tall.** Fennel is squat with a flat base; a circle on
   a stem is a symbol, an ovoid is a vegetable.
2. **Ribs inside the bulb.** The overlapping sheaths are what make it fennel
   rather than an onion, and they survive down to 32 px.
3. **Several fronds, not one stalk.** A "simplified for small sizes" variant —
   one stalk, one crossing frond — tested *worse* at 32 px than the detailed
   one: stripping the fronds collapsed it straight back into a symbol.
   Multiplicity is what reads as a plant, so there is no simplified variant.

Rendered and inspected at 512, 64, 32 and 16 px rather than judged at design
size, which is the only way point 3 shows up. At 16 px it mushes, as any
detailed mark does, but the silhouette still reads as a plant in a pot.

The mark lives in `FennelMark.swift` as `Shape`s grouped by stroke weight, and
`scripts/make-icon.swift` builds the `.icns` from the same coordinates — one
source of truth, so the icon can never drift from what the app draws.

---

## D-VOICE — It was greedy decoding, not the persona

Three complaints — it repeats itself, it won't open up, it overuses the same
emoji — turned out to be one cause. `mlx_lm.stream_generate` defaults to
`sampler=None`, which is **argmax**. Greedy decoding means the same question
produces a byte-identical answer every time, always the shortest safe phrasing,
and always the single highest-probability emoji. No amount of persona wording
fixes an argmax.

`temp=0.7, top_p=0.92`. Deliberately **no repetition penalty**: it distorts the
repeated quotes and braces in tool-call JSON, and the complaint was cross-turn
sameness, which is a greedy artefact rather than a within-reply loop.

The risk was that temperature would break tool calls. Measured: 4/4 fired with
well-formed arguments, and 0 emoji across those four replies. Variety improved
but is not dramatic — two of three fresh chats still answered "how are you"
near-identically, because the distribution is genuinely peaked there. Raising
temperature further trades tool reliability for variety, and tool reliability
wins.

The persona was rewritten alongside it, but as a smaller lever: "you are
talking, not writing", follow the thread rather than closing it with "let me
know if you need anything else", and never reuse an opener. Emoji guidance moved
from "at most one" to "almost never", with the reason stated — they don't
survive being read aloud, and one that appears every time reads as a tic.

---

## D-LICENCE — GPL-3.0, because the speech pipeline decided it

Auditing what Fennel actually bundles turned up one thing that outranks every
other licensing question: **espeak-ng is GPL-3.0**, and it arrives through
`misaki[en]` → `espeakng-loader` + `phonemizer-fork`, which ships a real
`libespeak-ng.dylib` inside the venv. Kokoro's pipeline builds an
`EspeakFallback` whenever it can import it.

That makes the bundled app a combined work with GPL-3.0 code, so Fennel is
GPL-3.0-or-later, and distributing a binary means publishing this source.

It was a genuine choice, and it was measured before being made. Without espeak,
misaki drops out-of-vocabulary words silently (`unk=""`): 5 of ~49 words across
six test sentences, and they were exactly the ones that matter — Okonkwo,
Siobhan, Krzysztof, Nopa, protoplanetary. Permissive licensing was available at
the price of a speech engine that skips names. Quality won.

Everything else is permissive and verified rather than assumed — declared
licences read from installed package metadata and the Hugging Face API, not from
memory: Qwen3-4B, Whisper small.en and Kokoro-82M are Apache-2.0; Silero VAD and
bge-small are MIT.

Obligations now met in the build: GPL-3.0 and Apache-2.0 texts are copied into
`Contents/Resources` and shown in-app under Settings → Licences, because both
licences require the text to accompany the binary rather than merely exist in a
repository. Open-Meteo is CC BY 4.0 and is credited by name in Settings;
Wikipedia extracts carry the article title and a link.

Two things left for whoever ships this: Kokoro's per-voice provenance (the model
is Apache-2.0, individual voices derive from varied data — Fennel uses `af_heart`
only), and RSS terms if feed content is ever republished rather than read
locally.

---

## D-BUNDLE — One app, no terminal; and the part money can't skip

Fennel now ships as a single `.app` that starts its own backend
(`BackendProcess.swift`), so opening it is the whole install.

**The runtime is copied, not the venv.** A venv only symlinks to its
interpreter — here, uv's CPython under `~/.local/share/uv` — so copying
`.venv` produces a bundle that works on this Mac and nowhere else. uv's CPython
is built to be relocatable, so `scripts/bundle-app.sh` copies *that* and drops
site-packages inside it. Verified by running the copy from a scratch directory
with every import the pipeline needs.

**Nothing could be trimmed.** torch alone is 503 MB and the obvious cut, but
exercising the full pipeline shows torch, spacy, numba, llvmlite, sympy and
scipy all load — misaki's English G2P and mlx-audio pull them in. 1.4 GB app,
570 MB compressed.

**Models are not bundled.** They are ~3 GB and would roughly triple the
download, so they fetch on first launch behind a first-run overlay — a blank
window while several gigabytes arrive just looks broken. This is the one moment
Fennel needs the network; after it, nothing.

**The app must kill its child.** A Python process that outlives the app keeps
port 8420, and the next launch silently talks to a stale backend — which, given
how much of this project's debugging has been "the running code isn't the code
I edited", is worth the SIGTERM-then-SIGKILL.

**Gatekeeper is the honest blocker.** The build is ad-hoc signed, which is fine
locally and refused on anyone else's Mac. Distribution needs a Developer ID
certificate (a paid Apple Developer account), the hardened runtime, and
notarization — `scripts/notarize.sh` does it, but no script can supply the
account. Python additionally needs the JIT and library-validation entitlements
or it crashes under the hardened runtime.

---

## D-CONSENT — Ask before the first byte, and prove it

The app downloaded ~3.5 GB of models on first launch without asking. For
something whose whole claim is that it runs on your machine, the one moment it
needs the network is the moment that most deserves a prompt.

The server now **listens before it loads**, so the app can connect, be told
`needs_consent`, and draw a screen — a window that cannot reach its backend is
indistinguishable from a broken one. Nothing touches the network until the app
reports the button was pressed, including the size estimate, which is hardcoded
rather than queried so the *estimate* costs no request either.

Testing it against an empty `HF_HOME` caught the thing worth catching: 35 files
appeared in the cache before consent, because `embed.shared()` was constructed
eagerly in `main()` and pulled bge-small down. Now zero. The lesson is that a
consent gate is only as good as the eager initialisation upstream of it, and the
only way to know is to point the cache somewhere empty and count files.

Progress is measured by **walking the cache directory**, not from
huggingface_hub's tqdm hooks. Its xet backend runs several bars concurrently
("Downloading bytes", "Reconstructing"), so summing their deltas reported 267 MB
of progress for a 133 MB model and pinned the bar at 100%. Disk is the only
measure that survives the library changing backends, and it is clamped to the
estimate so the bar never reads "206 MB of 135 MB".


---

## D-NOW — Don't ask a 4B model to read a table against a clock

The briefing carries all 24 hourly forecast rows, and the `<context>` block
carries the current time. Asking the model to join them did not work: the
weather *at 3pm* was answered correctly every time, and the weather *now* came
back with the overnight low every time. It is not a prompting problem — it is
arithmetic, and arithmetic belongs in code.

`Memory._weather_now` looks up the row for the current hour and states it
outright in the per-turn context. The hourly rows stay in the prefix so "will it
rain later" still works; only *now* is resolved for it.

**Two fixes shipped inert before this one landed**, both for the same reason:
the briefing's freshness fingerprint was `day|place`, so a code change to *what
build() stores* left yesterday's shape in place and the new field was simply
never written. The fingerprint now includes a `SCHEMA` version. Any change to
what a cached artefact contains needs the cache key to know about it.

---

## D-GATE-WAIT — Waiting on a socket is not waiting on a condition

The setup gate held new connections in `async for raw in ws` until the models
were ready, checking readiness after each message. Nothing sends messages during
startup, so the handler only noticed it was ready when the user typed — which is
exactly what the "opens to an empty chat, then fills in a few seconds later"
report was. Readiness is an `asyncio.Event` now, awaited *alongside* the socket
read.

Cancelling the reader is not enough on its own: websockets refuses a second
concurrent `recv`, and the cancelled task holds its slot until the
`CancelledError` has actually been delivered. The cancellation has to be
awaited, or the real handler dies with a `ConcurrencyError` the moment setup
finishes.
