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

from datetime import datetime
from typing import Optional

import config
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
    def __init__(self, store: Store, retriever=None) -> None:
        self._store = store
        self._retriever = retriever

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
        hits = self._store.search(user_text, exclude_session=session_id, limit=3)
        for h in hits:
            who = "they said" if h["role"] == "user" else "you said"
            text = " ".join(h["content"].split())[:160]
            parts.append(f"recall ({_ago(h['ts'], now)}, {who}): {text}")

        # News archive. Gated and hard-capped: on most turns this adds nothing,
        # which is what keeps per-turn latency flat as the archive grows.
        if self._retriever is not None:
            budget = config.RETRIEVAL_MAX_CHARS
            for c in self._retriever.search(user_text, k=3):
                line = f"news ({c['day']}, {c['source']}): {c['title']}"
                if c.get("body"):
                    line += f" — {c['body']}"
                line = line[:budget]
                budget -= len(line)
                parts.append(line)
                if budget <= 0:
                    break
        return "<context>\n" + "\n".join(parts) + "\n</context>\n"

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
        return [{"role": r["role"], "content": r["content"]} for r in rows]

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
