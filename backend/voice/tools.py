"""Tool-calling layer (Stage 3 / D-HOME).

Three pieces:

  1. `TOOLS` — JSON-Schema function signatures handed to the tokenizer's chat
     template. Qwen renders them into the system block, so they sit in the
     STABLE prefix and cost one prefill per session, never per turn (D4).
  2. `ToolStream` — splits the streamed reply into speakable prose and
     `<tool_call>` blocks, so tool syntax is never spoken or shown.
  3. `normalize` — validates/normalizes arguments into a card payload the app
     can render and act on. The actual side effect (EventKit) happens in Swift;
     the backend only ever normalizes and describes.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

# The model emits Hermes-style calls: <tool_call>{"name":…,"arguments":{…}}</tool_call>
_OPEN, _CLOSE = "<tool_call>", "</tool_call>"

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "Create a reminder in the user's Reminders app. Only when they "
                "actually ask to be reminded, or ask you to add a task. If they "
                "merely mention something they ought to do, just talk with them "
                "— do not create a reminder and do not offer one. Never invent a "
                "time they did not give: leave `due` out instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "What to be reminded of."},
                    "due": {
                        "type": "string",
                        "description": "When, as ISO 8601 local time, e.g. 2026-08-24T18:00. Omit if unspecified.",
                    },
                    "notes": {"type": "string", "description": "Optional extra detail."},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_event",
            "description": (
                "Add an event to the user's calendar. Use for anything with a "
                "specific time they will attend: meetings, appointments, plans."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {
                        "type": "string",
                        "description": "Start, ISO 8601 local time, e.g. 2026-08-24T15:30.",
                    },
                    "end": {
                        "type": "string",
                        "description": "End, ISO 8601. Defaults to one hour after start.",
                    },
                    "location": {"type": "string"},
                },
                "required": ["title", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agenda",
            "description": (
                "Look up what the user already has scheduled — their reminders "
                "and calendar events. Use it whenever they ask what's on, what's "
                "next, whether they're free, or what they have to do. Read the "
                "result back conversationally; do not guess if you haven't looked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "range": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "week"],
                        "description": "How far ahead to look. Defaults to today.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": (
                "Start a countdown timer that runs on screen and chimes when it "
                "finishes. Use for short waits — cooking, a break, 'in ten "
                "minutes'. For anything hours away or on a specific date, use "
                "set_reminder instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "Length in minutes; may be fractional."},
                    "label": {"type": "string", "description": "What it's for, e.g. 'pasta'."},
                },
                "required": ["minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Look a topic up on Wikipedia when you don't know it, aren't "
                "sure, or your knowledge may be out of date — people, places, "
                "science, history, definitions. It cannot find local businesses "
                "or breaking news. Call it and wait; you are given the article "
                "text, and then you answer from it and say it came from "
                "Wikipedia. Do not answer from memory once you've decided to look."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms, not a full sentence."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": (
                "Launch an app on the user's Mac by name — Spotify, Notes, "
                "Safari, Xcode. Use when they ask you to open or start one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "App name as it appears in Applications."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shortcut",
            "description": (
                "Run one of the user's macOS Shortcuts by name. This is how you "
                "reach anything the Shortcuts app can do — smart lights and "
                "other HomeKit scenes, sending a text, playing a playlist, "
                "whatever they have set up. Only call it when they name a "
                "shortcut or clearly refer to one; you are told if it doesn't "
                "exist, along with what does."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The shortcut's exact name."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_link",
            "description": (
                "Put a link on screen as a button the user can click — a website, "
                "or a page in a service like Apple Music or YouTube. Use when "
                "they ask you to open or pull up something. The user does the "
                "clicking, so say what it is rather than claiming you opened it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full http(s) URL."},
                    "label": {"type": "string", "description": "Short button text."},
                },
                "required": ["url", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_panel",
            "description": (
                "Display a card on the user's home screen and keep it there. "
                "You DO have a screen — use this whenever the user asks to see, "
                "show, or be given a list, steps, options, or a summary, and "
                "whenever something is worth keeping in front of them. Put the "
                "content in the card; then say one short sentence out loud."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "Short paragraph."},
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional bullet list.",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_song",
            "description": (
                "Show a song you are recommending as a playable card, with "
                "buttons that open it in Apple Music or Spotify. Use this "
                "whenever you name a specific song the user might want to hear. "
                "One call per song; call it up to three times for a short list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Song title only."},
                    "artist": {"type": "string"},
                    "why": {"type": "string", "description": "One short line on why it fits."},
                },
                "required": ["title", "artist"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_fact",
            "description": (
                "Store something about the user that should outlast this "
                "conversation: an allergy, a preference, a name, a routine, a "
                "person or pet in their life. Call it whenever they tell you to "
                "remember something, or state a lasting fact about themselves. "
                "This is the only way you remember anything — saying you will "
                "keep it in mind without calling this means you won't."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short slug, e.g. coffee_order."},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# Tools whose *result* is the answer, not a confirmation. The turn must always
# run another LLM round after these, even if the model already said something —
# skipping it (which is right for side-effecting tools) leaves the user with
# "let me look that up" and then silence.
ANSWERING_TOOLS = {"search_web", "agenda", "run_shortcut"}


# ── streaming split ────────────────────────────────────────────────────────


def _held(buf: str, tag: str) -> int:
    """Length of the trailing slice of `buf` that could still grow into `tag`."""
    for n in range(min(len(buf), len(tag) - 1), 0, -1):
        if buf.endswith(tag[:n]):
            return n
    return 0


class ToolStream:
    """Feed streamed chunks; get back only the prose that should be spoken,
    plus any tool calls that completed in this chunk (fired immediately so the
    home card appears while the model is still talking)."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_call = False
        self.raw = ""       # everything the model produced, verbatim

    def feed(self, chunk: str) -> tuple[str, list[dict]]:
        self.raw += chunk
        self._buf += chunk
        prose: list[str] = []
        calls: list[dict] = []
        while True:
            if self._in_call:
                i = self._buf.find(_CLOSE)
                if i == -1:
                    break
                body, self._buf = self._buf[:i], self._buf[i + len(_CLOSE):]
                self._in_call = False
                if (call := _parse_call(body)) is not None:
                    calls.append(call)
            else:
                i = self._buf.find(_OPEN)
                if i == -1:
                    # Hold back a partial "<tool_ca…" so it is never spoken.
                    keep = _held(self._buf, _OPEN)
                    prose.append(self._buf[:len(self._buf) - keep] if keep else self._buf)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                prose.append(self._buf[:i])
                self._buf = self._buf[i + len(_OPEN):]
                self._in_call = True
        return "".join(prose), calls

    def flush(self) -> tuple[str, list[dict]]:
        """End of generation: emit the tail (a truncated call is discarded)."""
        if self._in_call:
            body, self._buf, self._in_call = self._buf, "", False
            call = _parse_call(body)
            return "", [call] if call else []
        out, self._buf = self._buf, ""
        return out, []


def _parse_call(body: str) -> Optional[dict]:
    try:
        obj = json.loads(body.strip())
    except json.JSONDecodeError:
        print(f"[tool] unparseable call: {body.strip()[:120]!r}", flush=True)
        return None
    name = obj.get("name")
    if name not in TOOL_NAMES:
        print(f"[tool] unknown tool: {name!r}", flush=True)
        return None
    args = obj.get("arguments")
    return {"name": name, "args": args if isinstance(args, dict) else {}}


# ── argument normalization ─────────────────────────────────────────────────


def _parse_dt(value: Any) -> Optional[datetime]:
    """Lenient ISO 8601. The model is reliable about the shape but not the
    trimmings (trailing Z, a space instead of T, a missing seconds field)."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace(" ", "T").rstrip("Z")
    s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s)   # drop any offset; we mean local time
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def human_time(dt: datetime, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    clock = dt.strftime("%-I:%M %p").lower().replace(":00 ", " ")
    days = (dt.date() - now.date()).days
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if 0 < days < 7:
        return f"{dt.strftime('%A')} at {clock}"
    return f"{dt.strftime('%b %-d')} at {clock}"


def normalize(name: str, args: dict) -> tuple[dict, dict]:
    """→ (card_args, tool_result). `card_args` is what the app renders and acts
    on; `tool_result` is what the model reads before it speaks, so it must
    describe what will *actually* happen, not what was asked for."""
    now = datetime.now()

    if name == "set_reminder":
        title = str(args.get("title", "")).strip() or "Reminder"
        due = _parse_dt(args.get("due"))
        card = {"title": title, "notes": str(args.get("notes", "")).strip() or None,
                "due": due.isoformat() if due else None}
        result = {"ok": True, "title": title,
                  "due": human_time(due, now) if due else "no time set"}
        return card, result

    if name == "add_event":
        title = str(args.get("title", "")).strip() or "Event"
        start = _parse_dt(args.get("start"))
        if start is None:
            return {}, {"ok": False,
                        "error": "start time missing or unparseable; ask the user when"}
        end = _parse_dt(args.get("end")) or start + timedelta(hours=1)
        if end <= start:
            end = start + timedelta(hours=1)
        card = {"title": title, "start": start.isoformat(), "end": end.isoformat(),
                "location": str(args.get("location", "")).strip() or None}
        result = {"ok": True, "title": title, "start": human_time(start, now)}
        return card, result

    if name == "show_panel":
        title = str(args.get("title", "")).strip() or "Note"
        items = args.get("items")
        card = {"title": title,
                "body": str(args.get("body", "")).strip() or None,
                "items": [str(i) for i in items] if isinstance(items, list) else None}
        return card, {"ok": True, "shown": title}

    if name == "agenda":
        rng = str(args.get("range", "today")).strip().lower()
        if rng not in {"today", "tomorrow", "week"}:
            rng = "today"
        # The real answer comes back from the app, which owns EventKit; this is
        # only the placeholder the app's `data` reply fills in.
        return {"range": rng}, {"ok": True, "range": rng}

    if name == "set_timer":
        try:
            minutes = float(args.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0.0
        if not 0 < minutes <= 24 * 60:
            return {}, {"ok": False, "error": "minutes must be between 0 and 1440"}
        label = str(args.get("label", "")).strip() or "Timer"
        ends = datetime.now() + timedelta(minutes=minutes)
        card = {"label": label, "minutes": minutes, "ends": ends.isoformat()}
        pretty = (f"{int(minutes)} min" if minutes == int(minutes)
                  else f"{minutes:g} min")
        return card, {"ok": True, "label": label, "length": pretty}

    if name == "search_web":
        query = str(args.get("query", "")).strip()
        if not query:
            return {}, {"ok": False, "error": "a search query is required"}
        # The fetch itself happens in Session._run_tool: it's the network, so it
        # needs the online setting checked and a thread to run on.
        return {"query": query}, {"ok": True, "query": query}

    if name in ("open_app", "run_shortcut"):
        label = str(args.get("name", "")).strip()
        if not label:
            return {}, {"ok": False, "error": "a name is required"}
        # The app does the launching; it knows what's installed and owns the
        # automation permission. Backend just normalizes.
        return {"name": label}, {"ok": True, "name": label}

    if name == "open_link":
        url = str(args.get("url", "")).strip()
        # http(s) only: the model picks this URL, so anything that could reach a
        # file:// path or a custom scheme handler stays out of reach.
        if not re.match(r"^https?://[^\s]+$", url):
            return {}, {"ok": False, "error": "only http(s) links can be shown"}
        label = str(args.get("label", "")).strip() or url
        return {"url": url, "label": label}, {"ok": True, "shown": label}

    if name == "recommend_song":
        title = str(args.get("title", "")).strip()
        artist = str(args.get("artist", "")).strip()
        if not title or not artist:
            return {}, {"ok": False, "error": "both title and artist are required"}
        card = {"title": title, "artist": artist,
                "why": str(args.get("why", "")).strip() or None}
        return card, {"ok": True, "shown": f"{title} by {artist}"}

    if name == "set_fact":
        key = str(args.get("key", "")).strip()
        value = str(args.get("value", "")).strip()
        if not key or not value:
            return {}, {"ok": False, "error": "key and value are both required"}
        return {"key": key, "value": value}, {"ok": True, "remembered": f"{key}: {value}"}

    return {}, {"ok": False, "error": f"unknown tool {name}"}


def system_prompt(base: str, now: Optional[datetime] = None) -> str:
    """Base persona + dates + tool etiquette. Deliberately free of the clock so
    it is byte-identical all day: the server prefills it once at startup and
    every session reuses it (see LLM.prime), instead of paying ~4.7 s each.

    The explicit day table is not padding: a 4-bit 4B model reliably gets clock
    arithmetic right but miscounts weekdays ("next Wednesday" landed five days
    late), and looking the date up beats computing it."""
    now = now or datetime.now()
    days = "\n".join(
        f"  {(now + timedelta(days=i)).strftime('%A %Y-%m-%d')}"
        f"{'  (today)' if i == 0 else '  (tomorrow)' if i == 1 else ''}"
        for i in range(8)
    )
    return (
        f"{base}\n\n"
        "Dates:\n"
        f"{days}\n"
        "Each user message may open with a <context> block holding the current "
        "time and things you remember from past conversations. It is written "
        "for you, not spoken by the user: use it freely, but never quote it, "
        "read it aloud, or mention that you were given it. Resolve dates "
        "against the table above and always pass absolute ISO 8601 local times "
        "to tools.\n\n"
        "Tools are extra abilities, not your only ones — answer normally when "
        "the user just wants an answer, and call a tool only when they want "
        "that action taken. Most turns need no tool at all. Prefer plain "
        "conversation; never steer a chat toward setting a reminder, and don't "
        "reach for the tool you last used out of habit — pick the one that fits "
        "this turn, or none. Do not announce a tool before calling it: call it, "
        "then confirm once, in one short spoken sentence. You are told whether "
        "it actually worked. Never read out tool syntax, JSON, or raw timestamps."
    )
