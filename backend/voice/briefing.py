"""The daily briefing: current events without touching the model's weights.

The whole design rests on one observation — **the briefing is identical for the
whole day**. That makes it prefix material, not per-turn material: it is
prefilled once at startup by `LLM.prime()` and then costs nothing per turn.
Measured on this M2: a ~1300-token briefing added 0.07 s to time-to-first-token.

Which is also why it is budgeted rather than exhaustive. The same measurement
showed decode slowing 24 → 21 tok/s, because attention runs over a longer KV
cache — so the prefix carries a curated headline set, and everything else stays
in the retrievable archive where it costs nothing until asked for.

The briefing is *replaced* daily, never appended, so the prefix is the same size
in year three as on day one. Only the archive grows, and it is pruned.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import config
from voice import feeds
from voice.store import Store


def today() -> str:
    return date.today().isoformat()


class Briefing:
    def __init__(self, store: Store) -> None:
        self._store = store

    # ── settings ───────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._store.setting("daily_updates", "0") == "1"

    @property
    def place(self) -> str:
        return self._store.setting("location", "")

    def configure(self, enabled: Optional[bool] = None,
                  place: Optional[str] = None) -> None:
        if enabled is not None:
            self._store.set_setting("daily_updates", "1" if enabled else "0")
        if place is not None:
            self._store.set_setting("location", place.strip())

    # ── the prefix text ────────────────────────────────────────────────────

    def cached(self, day: Optional[str] = None) -> Optional[str]:
        if not self.enabled:
            return None
        return self._store.briefing(day or today())

    def _fingerprint(self) -> str:
        """What today's briefing was built from. Changing the city has to
        invalidate it: "is there a briefing for today" was not enough, and the
        result was a saved city that never produced any weather."""
        return f"{today()}|{self.place.strip().lower()}"

    def is_stale(self) -> bool:
        if not self.enabled:
            return False
        if self._store.briefing(today()) is None:
            return True
        return self._store.setting("briefing_inputs") != self._fingerprint()

    def build(self, embedder=None) -> Optional[str]:
        """Fetch, compose, store, and index. Blocking and network-bound — call it
        off the event loop, and never on a path the user is waiting on."""
        if not self.enabled:
            return None
        day = today()
        lines: list[str] = []

        if place := self.place:
            if geo := feeds.geocode(place):
                if w := feeds.weather(*geo):
                    lines.append(w)

        items = feeds.headlines()
        rows = [{"source": i.source, "title": i.title, "body": i.summary,
                 "link": i.link} for i in items]

        # Everything fetched goes to the archive; only what fits the budget goes
        # into the prompt prefix.
        if embedder is not None and rows:
            try:
                vecs = embedder.encode([f"{r['title']}. {r['body']}" for r in rows])
                for r, v in zip(rows, vecs):
                    r["vec"] = v
            except Exception as exc:
                print(f"[briefing] embedding failed, archiving unindexed: {exc}",
                      flush=True)
        if rows:
            self._store.add_chunks(day, rows)

        header = (f"Today is {datetime.now():%A, %B %-d, %Y}. "
                  "The notes below were fetched today and are more current than "
                  "your training data — prefer them for anything recent. They are "
                  "headlines, not full articles: if something isn't covered here, "
                  "say you don't know rather than guessing.")
        # Weather is already in `lines` and is deliberately not charged against
        # the headline budget: it is the most-asked-for part of the briefing.
        budget = config.BRIEFING_MAX_CHARS
        used = 0
        for i in items:
            entry = f"- [{i.source}] {i.title}"
            if i.summary:
                entry += f" — {i.summary}"
            if used + len(entry) > budget:
                break
            used += len(entry) + 1
            lines.append(entry)

        if not lines:
            print("[briefing] nothing fetched; leaving the prefix alone", flush=True)
            return None

        text = header + "\n\n" + "\n".join(lines)
        self._store.set_briefing(day, text)
        self._store.set_setting("briefing_inputs", self._fingerprint())
        pruned = self._store.prune_chunks(config.ARCHIVE_KEEP_DAYS)
        print(f"[briefing] {day}: {len(lines)} lines, {len(text)} chars, "
              f"{len(rows)} archived, {pruned} pruned", flush=True)
        return text


class Retriever:
    """Hybrid search over the archive: embeddings for meaning, FTS5 for exact
    names and numbers. Neither alone is enough — vectors miss a rare proper noun,
    keywords miss a paraphrase."""

    def __init__(self, store: Store, embedder=None) -> None:
        self._store = store
        self._embedder = embedder

    def search(self, query: str, k: int = 3) -> list[dict]:
        """Returns nothing at all for a query the archive can't help with — which
        is most of them. That is the point: declining costs zero prompt tokens,
        so latency stays flat no matter how large the archive gets."""
        if self._embedder is None:
            return []
        # Very short turns ("thanks", "yes", "hi") carry no query at all, but a
        # 384-dim vector will still land somewhere and occasionally clear the
        # floor. Cheaper and more honest to not ask.
        if len(query.split()) < 3:
            return []
        ids, mat = self._store.all_chunk_vectors()
        if mat is None or not len(ids):
            return []
        try:
            sims = mat @ self._embedder.encode_one(query)   # both L2-normalised
        except Exception as exc:
            print(f"[retrieve] vector search failed: {exc}", flush=True)
            return []

        order = [i for i in sims.argsort()[::-1][:k * 2]
                 if float(sims[i]) >= config.RETRIEVAL_MIN_SCORE]
        if not order:
            return []          # the gate: no topical hit, so inject nothing

        scored = {ids[idx]: 1.0 / (rank + 1) for rank, idx in enumerate(order)}
        # FTS refines but never gates: it returns *something* for any query at
        # all (measured), so on its own it would drag noise into every prompt.
        # Reciprocal-rank fusion, so the two score scales never need aligning.
        for rank, cid in enumerate(self._store.search_chunks_fts(query, k * 2)):
            if cid in scored:
                scored[cid] += 0.8 / (rank + 1)

        top = sorted(scored, key=lambda c: -scored[c])[:k]
        return self._store.chunks_by_id(top)
