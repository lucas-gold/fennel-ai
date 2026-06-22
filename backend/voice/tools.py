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

# Every user turn is prefixed with a <context> block (clock, recall, news —
# see memory.preamble). Smaller models imitate it: they echo the block back,
# or open one and narrate a whole invented turn. It is scaffolding, never
# something to show or read aloud, so it is swallowed exactly like a tool
# call — in the stream, so a block split across chunks is never spoken one
# character at a time.
_CTX_OPEN, _CTX_CLOSE = "<context>", "</context>"

# Qwen3-4B answers "draw me X" by writing an <image_description> block rather
# than calling generate_image. The persona tells it not to; this makes sure the
# tag never reaches the screen or the speaker when it does it anyway.
_DESC_OPEN, _DESC_CLOSE = "<image_description>", "</image_description>"

#: Words a title must not end on. Prepositions and articles because they leave
#: it hanging; the participles and adjectives because a six-word cut through a
#: longer description lands on them and reads as truncation rather than a name.
_TRAILING = {"a", "an", "the", "with", "of", "in", "on", "at", "and", "for",
             "to", "its", "his", "her", "their", "that", "which", "is", "are",
             "called", "named", "featuring", "showing", "wearing", "holding",
             "huge", "big", "small", "tiny", "bright", "dark", "very", "more"}


def _short_title(prompt: str) -> str:
    """A name for the card: "cat sleeping on a bed", not the first six words of
    a paragraph stopping mid-phrase.

    Prefers the first clause — descriptions are comma-separated lists, and the
    part before the first comma is almost always the subject.
    """
    first = re.split(r"[,;.]", prompt, maxsplit=1)[0]
    words = first.split()
    if words and words[0].lower() in {"a", "an", "the"}:
        words = words[1:]
    words = words[:6]
    while words and words[-1].strip(",.").lower() in _TRAILING:
        words.pop()
    return " ".join(words).strip(" ,.;:") or "Picture"


_LEAD_IN = re.compile(
    r"^(picture (this|a|an)|imagine( this)?|here('?s| is) (a|an|the)|"
    r"visualise|visualize|envision)\b[:,]?\s*", re.I)

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
                "time they did not give — an undated reminder is perfectly "
                "normal, so just leave `due` out and say you added it without a "
                "time. macOS has no "
                "alarm clock you can set, so treat a request for an alarm as a "
                "reminder at that time — it fires a notification — and say that "
                "is what you set."
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
            "name": "search_wikipedia",
            "description": (
                "Look a subject up in an encyclopedia. Best for things that are "
                "settled rather than current: people, places, history, science, "
                "definitions, how something works. Prefer this over search_web "
                "whenever the answer would not have changed this year — it is "
                "free, fast and needs no quota. It cannot find news, prices, "
                "local businesses or anything from the last few months. Call it "
                "and wait; you are given the article text, answer from that, and "
                "say it came from Wikipedia."
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
            "name": "search_web",
            "description": (
                "Search the live web. Use it whenever the answer depends on "
                "what is true *now*: what's happening or on tonight, events, "
                "gigs, openings and closures, news, prices, releases, "
                "schedules, scores, local places, anything from the last year, "
                "or anything an encyclopedia would not carry.\n"
                "'What's happening in <city> tonight' is exactly this tool — do "
                "not answer it from memory, and do not reply that you lack "
                "real-time data. You have this tool; that is what it is for. "
                "Also call it any time the user asks you to search or look "
                "something up online. Prefer search_wikipedia only when a plain "
                "encyclopedia article would answer just as well.\n"
                "Work like a researcher: search with terms, not a sentence. You "
                "are given several results with snippets — read them, say what "
                "you found and where, and if they don't actually answer the "
                "question, search once more with better terms rather than "
                "guessing. Never invent a source or a URL."
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
            "name": "create_shortcut",
            "description": (
                "Build a new macOS Shortcut for the user. They review it and "
                "press Add before it exists, so propose one when they describe "
                "a routine they'd like to repeat. Each step is {type, value}.\n"
                "The step types below exist ONLY inside a shortcut you build "
                "with this tool. They are not things you can do on request: "
                "set_brightness is the Mac's own display, not room lighting, and "
                "you cannot control lamps, smart bulbs, speakers or a home "
                "system. If asked for any of that, say so — you can offer to "
                "build a shortcut that runs one of their existing Home "
                "shortcuts, and nothing more.\n"
                "Step types that DO something: open_app (app name), quit_app, "
                "run_shortcut (name of one of their existing shortcuts), "
                "open_url, music (play/pause), next_track, previous_track, "
                "set_focus (on/off), set_wifi (on/off), set_bluetooth (on/off), "
                "set_low_power (on/off), set_volume (0-1), set_brightness (0-1).\n"
                "Step types that just report: say, notify, show, wait, comment.\n"
                "Build the shortcut mainly out of the first group — a shortcut "
                "made only of notifications and waits does nothing useful. These "
                "are the ONLY step types that exist; if what they want needs "
                "anything else, tell them that instead of inventing a step.\n"
                "On/off values are literally \"on\" or \"off\" and must match what "
                "was asked. A Wind Down shortcut would be steps of set_focus on, "
                "then open_app Music, then set_brightness 0.2 — passed as this "
                "tool's arguments. Never write a step out in your reply: steps "
                "only ever travel inside a tool call, and a user who sees one is "
                "seeing a bug."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short name for the shortcut."},
                    "steps": {
                        "type": "array",
                        "description": "Ordered steps.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["type", "value"],
                        },
                    },
                },
                "required": ["name", "steps"],
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

# Tools gated behind a setting. A disabled tool is REMOVED from the list rather
# than left in to refuse: a refusal ("I can't access Wikipedia directly") gets
# stored like any other reply, and then teaches refusal even after the user
# enables it — which is exactly what happened. If it isn't offered, it can't be
# tried, so nothing teachable is recorded.
TOOLS.append({
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Draw a picture from a description. Use it when the user asks for "
            "an image, a picture, a drawing, artwork or a photo of something "
            "that does not exist yet. It takes about a minute, so say you are "
            "starting it and let the picture arrive on its own — do not "
            "describe the image you are about to make, and never claim it is "
            "ready. It cannot edit an existing picture, and it cannot show the "
            "user something real: for a real place or person, say so and offer "
            "to look it up instead.\n"
            "Write the prompt yourself rather than passing the request "
            "through: a good one names the subject, the setting, the light and "
            "the kind of photograph or drawing it is. Keep it under 60 words."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "A full visual description of the picture to draw.",
                },
                "subject": {
                    "type": "string",
                    "description": "Two or three words naming it, for the card title.",
                },
            },
            "required": ["prompt"],
        },
    },
})


OPTIONAL_TOOLS = {
    "search_wikipedia": "lookups",   # the free one; on with the Look things up switch
    "search_web": "web_key",         # additionally needs the user's own API key
    "generate_image": "images",      # off until the user switches pictures on
}


def tool_list(settings: dict[str, bool]) -> list[dict]:
    """The tools to advertise, given which optional features are switched on."""
    return [t for t in TOOLS
            if settings.get(OPTIONAL_TOOLS.get(t["function"]["name"], ""), True)]

# Tools whose *result* is the answer, not a confirmation. The turn must always
# run another LLM round after these, even if the model already said something —
# skipping it (which is right for side-effecting tools) leaves the user with
# "let me look that up" and then silence.
ANSWERING_TOOLS = {"search_web", "search_wikipedia", "agenda",
                   "run_shortcut", "create_shortcut"}


# ── streaming split ────────────────────────────────────────────────────────


def _held(buf: str, tag: str) -> int:
    """Length of the trailing slice of `buf` that could still grow into `tag`."""
    for n in range(min(len(buf), len(tag) - 1), 0, -1):
        if buf.endswith(tag[:n]):
            return n
    return 0


# A bare JSON object in prose that is really a tool call or a shortcut step —
# `{ "type": "set_brightness", "value": "0.3" }` reached a user's chat and would
# have been read aloud. Matched on the keys we actually emit, so ordinary prose
# containing braces is left alone.
_STRAY_JSON = re.compile(
    r'\{\s*"(?:type|name)"\s*:\s*"[a-z_]+"[^{}]*\}', re.S)


# Stage directions. RP-tuned models narrate in pseudo-tags — <sarcasm>, <laughs>,
# <whisper> — which are silent nonsense on screen and read aloud verbatim by TTS.
# They cannot be blanket-stripped: the coding model legitimately writes <div>, and
# someone may ask what <script> does. So real HTML names are spared, and anything
# inside a code fence or backticks is never touched at all.
_HTML_OK = {
    "a", "abbr", "b", "body", "br", "button", "canvas", "code", "col", "div",
    "em", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header",
    "hr", "html", "i", "iframe", "img", "input", "label", "li", "link", "main",
    "meta", "nav", "ol", "option", "p", "pre", "s", "script", "section", "select",
    "small", "span", "strong", "style", "sub", "sup", "svg", "table", "tbody",
    "td", "textarea", "tfoot", "th", "thead", "title", "tr", "u", "ul", "video",
}
_STAGE_TAG = re.compile(r"</?([a-z][a-z0-9_]{1,19})>")
_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def _strip_stage_tags(text: str) -> str:
    """Drop invented pseudo-tags from prose, leaving code and real HTML alone."""
    spans = [m.span() for m in _CODE_SPAN.finditer(text)]

    def guarded(m: re.Match) -> str:
        if any(a <= m.start() < b for a, b in spans):
            return m.group(0)                      # inside code — hands off
        if m.group(1) in _HTML_OK:
            return m.group(0)
        return ""

    out = _STAGE_TAG.sub(guarded, text)
    if out != text:
        print("[tools] dropped stage-direction tags from prose", flush=True)
    return out


def _strip_stray_json(text: str) -> str:
    """Drop tool/step objects the model wrote as prose instead of calling."""
    cleaned = _strip_stage_tags(_STRAY_JSON.sub("", text).replace(_CTX_CLOSE, ""))
    if cleaned != text:
        print("[tools] dropped stray JSON from prose", flush=True)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


class ToolStream:
    """Feed streamed chunks; get back only the prose that should be spoken,
    plus any tool calls that completed in this chunk (fired immediately so the
    home card appears while the model is still talking)."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_call = False
        self._in_ctx = False
        self._ctx_close = _CTX_CLOSE
        #: Text from an <image_description> block, if the model wrote one
        #: instead of calling generate_image. Kept rather than dropped: it is a
        #: perfectly good prompt, and using it is more reliable than persuading
        #: a 4B to call the tool.
        self.image_description = ""
        # A swallowed block leaves the newline that followed it, which renders
        # as a blank first line in the bubble — it reads as stray padding above
        # the reply. Trim once, on the next prose to actually arrive.
        self._trim_next = False
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
            elif self._in_ctx:
                i = self._buf.find(self._ctx_close)
                if i == -1:
                    # Keep only what could still complete the closing tag, so an
                    # echoed block of any length costs no memory and speaks none
                    # of itself.
                    keep = _held(self._buf, self._ctx_close)
                    if getattr(self, "_grabbing_desc", False):
                        # Everything except the held tail: that tail may yet turn
                        # out to be the closing tag, and capturing it would put
                        # "</im" into the image prompt.
                        self.image_description += (
                            self._buf[:len(self._buf) - keep] if keep else self._buf)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                if getattr(self, "_grabbing_desc", False):
                    self.image_description += self._buf[:i]
                    self._grabbing_desc = False
                self._buf = self._buf[i + len(self._ctx_close):]
                self._in_ctx = False
                self._trim_next = True
            else:
                i = self._buf.find(_OPEN)
                j = self._buf.find(_CTX_OPEN)
                d = self._buf.find(_DESC_OPEN)
                if d != -1 and (j == -1 or d < j):
                    j = d
                if j != -1 and (i == -1 or j < i):
                    prose.append(self._buf[:j])
                    tag = (_DESC_OPEN if self._buf.startswith(_DESC_OPEN, j)
                           else _CTX_OPEN)
                    self._ctx_close = (_DESC_CLOSE if tag is _DESC_OPEN
                                       else _CTX_CLOSE)
                    self._buf = self._buf[j + len(tag):]
                    self._grabbing_desc = tag is _DESC_OPEN
                    self._in_ctx = True
                    print(f"[tools] swallowed an echoed {tag} block", flush=True)
                    continue
                if i == -1:
                    # Hold back a partial "<tool_ca…" / "<contex…" so it is
                    # never spoken — and a partial "</contex…" too, since an
                    # orphan closing tag is only strippable once it is whole.
                    keep = max(_held(self._buf, _OPEN), _held(self._buf, _CTX_OPEN),
                               _held(self._buf, _CTX_CLOSE),
                               _held(self._buf, _DESC_OPEN))
                    out = self._buf[:len(self._buf) - keep] if keep else self._buf
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    # Also hold an unterminated "{…" — a stray JSON object can
                    # only be recognised once its closing brace arrives, and
                    # emitting the opening half would speak it a character at a
                    # time. Give up past a sane length so real prose containing
                    # a brace is never swallowed.
                    brace = out.rfind("{")
                    if brace != -1 and "}" not in out[brace:] and len(out) - brace < 200:
                        self._buf = out[brace:] + self._buf
                        out = out[:brace]
                    prose.append(self._emit(out))
                    break
                prose.append(self._emit(self._buf[:i], clean=False))
                self._buf = self._buf[i + len(_OPEN):]
                self._in_call = True
        return "".join(prose), calls

    def _emit(self, out: str, clean: bool = True) -> str:
        """Prose on its way to the screen and the speaker."""
        if self._trim_next:
            stripped = out.lstrip()
            # Stay armed through empty emissions: at one character per chunk the
            # closing tag lands with nothing after it, and disarming there would
            # let the newline through on the following chunk.
            if stripped:
                self._trim_next = False
            out = stripped
        return _strip_stray_json(out) if clean else out

    def flush(self) -> tuple[str, list[dict]]:
        """End of generation: emit the tail (a truncated call is discarded)."""
        if self._in_call:
            body, self._buf, self._in_call = self._buf, "", False
            call = _parse_call(body)
            return "", [call] if call else []
        if self._in_ctx:
            if getattr(self, "_grabbing_desc", False):
                self.image_description += self._buf
                self._grabbing_desc = False
            # An unterminated echo: the model opened a block and ran out. There
            # is nothing in it worth saying, so it goes rather than leaking out.
            self._buf, self._in_ctx = "", False
            return "", []
        out, self._buf = self._buf, ""
        return self._emit(out), []


# Some fine-tunes emit an XML-ish call instead of Hermes JSON, whatever their
# chat template advertises:
#     <function=set_reminder><parameter=title>Call the dentist</parameter></function>
# Qwen3.5-9B-Defiant does this on every call. Recognising it is additive — the
# JSON path is tried first and is unchanged — so a model that emits proper
# Hermes never reaches here.
_FN_NAME = re.compile(r"<function\s*=\s*([A-Za-z_]\w*)\s*>")
_FN_ARG = re.compile(r"<parameter\s*=\s*([A-Za-z_]\w*)\s*>(.*?)</parameter\s*>",
                     re.DOTALL)


def _coerce(raw: str):
    """A parameter block carries no type. Read it as JSON when it plainly is
    one — `5`, `true`, a list — and otherwise leave it as the string it looks
    like, so a title of "5" does not silently become a number."""
    text = raw.strip()
    if text[:1] in "[{" or text in {"true", "false", "null"} or (
            text.replace(".", "", 1).lstrip("-").isdigit()):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _parse_function_call(body: str) -> Optional[dict]:
    m = _FN_NAME.search(body)
    if not m:
        return None
    args = {k: _coerce(v) for k, v in _FN_ARG.findall(body)}
    return {"name": m.group(1), "arguments": args}


def _parse_call(body: str) -> Optional[dict]:
    try:
        obj = json.loads(body.strip())
    except json.JSONDecodeError:
        obj = _parse_function_call(body)
        if obj is None:
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

    if name == "generate_image":
        prompt = " ".join(str(args.get("prompt", "")).split())
        # Models that describe rather than call open with a stage direction.
        # It belongs in neither the title nor the prompt.
        prompt = _LEAD_IN.sub("", prompt).strip()
        if not prompt:
            return {}, {"ok": False, "error": "no description given; ask what to draw"}
        title = str(args.get("subject", "")).strip() or _short_title(prompt)
        card = {"title": title[:60], "prompt": prompt[:600]}
        # The result the model sees is deliberately not "done": the picture is
        # still a minute away, and a model told the tool succeeded will happily
        # announce a picture nobody can see yet.
        result = {"ok": True, "status": "started",
                  "note": "the picture is being drawn and will appear on its own"}
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
        # Whole seconds only: isoformat() otherwise emits microseconds, which the
        # app's date parser rejected outright — the card appeared with no
        # countdown behind it.
        ends = (datetime.now() + timedelta(minutes=minutes)).replace(microsecond=0)
        card = {"label": label, "minutes": minutes, "ends": ends.isoformat()}
        pretty = (f"{int(minutes)} min" if minutes == int(minutes)
                  else f"{minutes:g} min")
        return card, {"ok": True, "label": label, "length": pretty}

    if name in ("search_web", "search_wikipedia"):
        query = str(args.get("query", "")).strip()
        if not query:
            return {}, {"ok": False, "error": "a search query is required"}
        # The fetch itself happens in Session._run_tool: it's the network, so it
        # needs the online setting checked and a thread to run on.
        return {"query": query}, {"ok": True, "query": query}

    if name == "create_shortcut":
        label = str(args.get("name", "")).strip() or "Shortcut"
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            return {}, {"ok": False, "error": "the shortcut needs at least one step"}
        return {"name": label, "steps": steps}, {"ok": True, "name": label}

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
        "You are Fennel. This Mac has no alarm clock you can set, so when they "
        "ask to be woken or want an alarm, set a reminder for that time — it "
        "fires a notification — and tell them you set a reminder. Never refuse "
        "the request outright.\n"
        "Tools are extra abilities, not your only ones — answer normally when "
        "the user just wants an answer, and call a tool only when they want "
        "that action taken. Most turns need no tool at all. Prefer plain "
        "conversation; never steer a chat toward setting a reminder, and don't "
        "reach for the tool you last used out of habit — pick the one that fits "
        "this turn, or none. Never say you have done something, or describe what "
        "you are about to do, unless you actually called the tool for it in this "
        "turn — writing out what a reminder would look like is not setting one.\n"
        "Two things you must never do instead of searching. Do not say you lack "
        "current or real-time information — that sentence is the signal to call "
        "search_web, so call it. And never state what is going on right now — "
        "events, what is on tonight, openings, prices, scores, schedules — "
        "unless a search actually returned it this turn; describing a city's "
        "evening from imagination is inventing facts, not being helpful.\n"
        "Do not announce a tool before calling it: call it, "
        "then confirm once, in one short spoken sentence. You are told whether "
        "it actually worked. Never read out tool syntax, JSON, or raw timestamps."
    )
