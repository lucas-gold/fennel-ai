"""Prompt-facing memory (D8): what the model is told it knows, and when.

Three inputs, deliberately placed at three different depths in the prompt so
each costs a prefill only as often as it actually changes (D4):

  facts + rolling summary  → one `<context>` message ahead of the window.
                             Rebuilt only when the window is rebuilt, so the
                             re-prefill it forces is one we were paying anyway.
  recall + clock           → prepended to the *current* user message, the one
                             position that is new every turn regardless.
  verbatim turns           → the window itself, evicted in chunks rather than
                             one turn at a time (see `window`), because sliding
                             by one every turn would invalidate the whole
                             cached prefix every turn.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

import config
from voice import embed
from voice.store import Store

Message = dict[str, str]

_SUMMARY_PROMPT = (
    "Summarise this conversation in under 60 words, in the third person, "
    "keeping only what would matter later: decisions, preferences, plans, "
    "personal details. No preamble, just the summary."
)


def _ago(ts: float, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    days = (now - datetime.fromtimestamp(ts)).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return "a while back"


class Memory:
    def __init__(self, store: Store, retriever=None, embedder=None) -> None:
        self._store = store
        self._retriever = retriever
        self._embedder = embedder

    def remember(self, session_id: int, role: str, content: str,
                 prompt_text: Optional[str] = None) -> None:
        """Persist a turn, embedding it so recall can be gated on meaning.
        Embedding costs ~5 ms; matching without it costs a prompt full of noise."""
        vec = None
        if self._embedder is not None and len(content.split()) >= 3:
            try:
                vec = self._embedder.encode_one(content)
            except Exception as exc:
                print(f"[memory] embed failed, storing unindexed: {exc}", flush=True)
        self._store.add_message(session_id, role, content, vec=vec,
                                prompt_text=prompt_text)

    def _recall(self, session_id: int, query: str, k: int = 2) -> list[dict]:
        """Past conversations worth quoting — usually none.

        This used to be raw FTS5, which returns *something* for any query at
        all: "tell me a joke" was pulling in "how are you" and "what else is
        new", costing 60+ tokens of prefill every turn to actively mislead the
        model. Gated on cosine now, exactly like news retrieval (D-BRIEFING).
        """
        if self._embedder is None or len(query.split()) < 3:
            return []
        ids, mat = self._store.message_vectors(exclude_session=session_id)
        try:
            hits = embed.gated_top_k(self._embedder.encode_one(query), ids, mat,
                                     config.RECALL_MIN_SCORE, k)
        except Exception as exc:
            print(f"[memory] recall failed: {exc}", flush=True)
            return []
        return self._store.messages_by_id(hits)

    # ── the context message (facts + summary) ──────────────────────────────

    def context_message(self, session_id: int) -> Optional[Message]:
        """Facts and the rolling summary, as one system message. None if there
        is nothing to say — an empty block would just be noise in the prompt."""
        lines: list[str] = []
        facts = self._store.facts()
        if facts:
            lines.append("Known about the user: "
                         + "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in facts))
        summary, _ = self._store.summary(session_id)
        if summary:
            lines.append(f"Earlier in this conversation: {summary}")
        if not lines:
            return None
        return {"role": "system", "content": "\n".join(lines)}

    # ── the per-turn preamble (clock + recall) ─────────────────────────────

    def preamble(self, session_id: int, user_text: str,
                 now: Optional[datetime] = None) -> str:
        """Prefix for the current user message. The clock is here rather than in
        the system prompt so the primed prefix stays byte-identical (D-PREFIX)."""
        now = now or datetime.now()
        parts = [f"time: {now.strftime('%-I:%M %p')}"]
        if w := self._weather_now(now):
            parts.append(w)
        for h in self._recall(session_id, user_text):
            who = "they said" if h["role"] == "user" else "you said"
            text = " ".join(h["content"].split())[:160]
            parts.append(f"recall ({_ago(h['ts'], now)}, {who}): {text}")

        # News archive. Gated and hard-capped: on most turns this adds nothing,
        # which is what keeps per-turn latency flat as the archive grows.
        if self._retriever is not None:
            budget = config.RETRIEVAL_MAX_CHARS
            for c in self._retriever.search(user_text, k=config.RETRIEVAL_TOP_K):
                line = f"news ({c['day']}, {c['source']}): {c['title']}"
                if c.get("body"):
                    line += f" — {c['body']}"
                line = line[:budget]
                budget -= len(line)
                parts.append(line)
                if budget <= 0:
                    break
        return "<context>\n" + "\n".join(parts) + "\n</context>\n"

    def _weather_now(self, now: datetime) -> Optional[str]:
        """Today's forecast row for the current hour, resolved here rather than
        left to the model.

        The briefing carries all 24 rows, but a 4-bit 4B model would not match
        the clock against them: it answered "the weather at 3pm" correctly and
        "the weather now" with the overnight low, every time. One line of
        arithmetic removes the inference entirely.
        """
        if self._store.setting("weather_day") != date.today().isoformat():
            return None                    # stale; the briefing will refresh it
        try:
            rows = json.loads(self._store.setting("weather_hourly", "[]"))
        except (ValueError, TypeError):
            return None
        if not rows:
            return None
        want = f"{now.hour:02d}:00"
        row = next((r for r in rows if r.get("t") == want), None) or rows[-1]
        return (f"weather right now ({row['t']}): {row['temp']}{row.get('unit', '°C')}, "
                f"{row['cond']}, {row['pop']}% chance of precipitation")

    # ── the verbatim window ────────────────────────────────────────────────

    def window(self, session_id: int) -> list[Message]:
        """Recent turns to replay into the prompt, oldest first.

        Kept at up to 2x the verbatim budget and cut back to 1x when it
        overflows. Trimming in chunks means the expensive re-prefill happens
        once every few turns instead of on every single one.
        """
        rows = self._store.messages(session_id)
        keep = config.VERBATIM_TURNS * 2
        if len(rows) > keep * 2:
            rows = rows[-keep:]
        # Replay what the model actually produced, tool calls included — not the
        # cleaned-up text the UI shows.
        out: list[Message] = []
        for r in rows:
            text = r.get("prompt_text")
            if text is None:
                # Written before D-REPLAY: the stored assistant text is the
                # laundered reply with its tool call stripped out, so replaying
                # it verbatim is a worked example of narrating an action instead
                # of taking one. Measured on a real conversation: 16 such
                # messages was enough to stop tool calling completely, while the
                # same turn with these elided called the tool. The user still
                # sees the original text in the chat; only the replay changes.
                text = "(earlier reply)" if r["role"] == "assistant" else r["content"]
            out.append({"role": r["role"], "content": text})
        return out

    def needs_summary(self, session_id: int) -> bool:
        rows = self._store.messages(session_id)
        _, upto = self._store.summary(session_id)
        unfolded = [r for r in rows if r["id"] > upto]
        return len(unfolded) > config.VERBATIM_TURNS * 4

    def summarise(self, session_id: int, llm) -> None:
        """Fold everything older than the verbatim window into the summary.

        Runs on a throwaway KV cache (`llm.complete`) so it cannot disturb the
        conversation's cached prefix, and is called off the turn's critical
        path — it costs a full prefill and the user should never wait for it.
        """
        rows = self._store.messages(session_id)
        old = rows[:-config.VERBATIM_TURNS * 2]
        if not old:
            return
        prior, _ = self._store.summary(session_id)
        transcript = "\n".join(
            f"{r['role']}: {' '.join(r['content'].split())[:300]}" for r in old)
        content = (f"{_SUMMARY_PROMPT}\n\n"
                   + (f"Summary so far: {prior}\n\n" if prior else "")
                   + f"Conversation:\n{transcript}")
        text = llm.complete([{"role": "user", "content": content}], max_tokens=140)
        text = " ".join(text.split())
        if text:
            self._store.set_summary(session_id, text, old[-1]["id"])
            print(f"[memory] summarised session {session_id} "
                  f"({len(old)} msgs -> {len(text)} chars)", flush=True)
