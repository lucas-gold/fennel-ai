"""Durable state: chat sessions, messages, facts, rolling summaries (D8).

SQLite lives in the backend, not the app, for one reason: the LLM is the main
consumer. Recall, the summary and the verbatim window are all prompt inputs, so
keeping them next to the prompt builder avoids shipping conversation state back
and forth over the WebSocket on every turn. The app asks for what it needs to
draw (session list, message history) and nothing more.

Full-text recall uses FTS5, which is in the Python stdlib's SQLite build — no
embedding model, no extra dependency, and it stays honest offline.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Any, Optional

APP_DIR = os.path.expanduser("~/Library/Application Support/Fennel")
DB_PATH = os.path.join(APP_DIR, "fennel.sqlite3")
# Frozen: the real location, immune to tests reassigning DB_PATH. The first
# version of this migration keyed off `path == DB_PATH`, so a test that pointed
# DB_PATH at a scratch file made the migration fire *into the scratch file* —
# and because it moved rather than copied, the user's real database went with
# it and was deleted with the scratch. Never let a destructive migration be
# aimed by a mutable global.
_REAL_DB = DB_PATH
_LEGACY_DB = os.path.expanduser("~/Library/Application Support/my_ai/my_ai.sqlite3")


def _adopt_legacy(path: str) -> None:
    """Carry a pre-rename database over rather than silently starting empty.

    Copies, never moves. A migration that moves has no fallback if anything
    downstream goes wrong, and the original costs a few megabytes to keep.
    """
    if path != _REAL_DB or os.path.exists(path) or not os.path.exists(_LEGACY_DB):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # sqlite3's backup API rather than a file copy: it checkpoints the WAL, so
    # recent writes come along without touching the sidecars by hand.
    src = sqlite3.connect(_LEGACY_DB)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"[store] copied the pre-rename database from {_LEGACY_DB} "
          "(the original is left in place)", flush=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title   TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);

-- External-content FTS: the index mirrors messages rather than copying it.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;

-- Cards raised by tool calls. Kept with the conversation because they are part
-- of it: a reminder, a picture, a search result. Without this a reopened chat
-- showed "Drawing that now" with nothing underneath — the reply survived and
-- the thing it was about did not.
CREATE TABLE IF NOT EXISTS cards (
  id         TEXT PRIMARY KEY,
  session_id INTEGER NOT NULL,
  seq        INTEGER NOT NULL,
  kind       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_by_session ON cards(session_id, seq);

CREATE TABLE IF NOT EXISTS facts (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Daily briefing: one row per day, held verbatim because it is prefilled into
-- the prompt prefix and must be byte-identical all day (D-PREFIX).
CREATE TABLE IF NOT EXISTS briefings (
    day     TEXT PRIMARY KEY,
    text    TEXT NOT NULL,
    created REAL NOT NULL
);

-- The retrievable archive. Chunks outlive the day their briefing was current,
-- so "what was that story last week" still works; `vec` is a raw float32
-- buffer, which is plenty at this scale (a year is ~50 MB and one numpy dot).
CREATE TABLE IF NOT EXISTS chunks (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    day    TEXT NOT NULL,
    source TEXT NOT NULL,
    title  TEXT NOT NULL,
    body   TEXT NOT NULL DEFAULT '',
    link   TEXT NOT NULL DEFAULT '',
    ts     REAL NOT NULL,
    vec    BLOB
);
CREATE INDEX IF NOT EXISTS chunks_by_day ON chunks(day);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(title, body, content='chunks', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, body)
        VALUES ('delete', old.id, old.title, old.body);
END;

CREATE TABLE IF NOT EXISTS summaries (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    summary    TEXT NOT NULL,
    upto_id    INTEGER NOT NULL   -- last message id folded into the summary
);
"""

# FTS5 treats these as syntax; user speech is not a query language.
_FTS_UNSAFE = re.compile(r'[^\w\s]')


class Store:
    def __init__(self, path: Optional[str] = None) -> None:
        # Resolved at call time, not bound as a default, so tests can point
        # DB_PATH at a scratch file instead of the user's real database.
        path = path or DB_PATH
        _adopt_legacy(path)     # no-ops unless `path` is the real location
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Serialised by _lock; the connection is shared across the event loop
        # and the worker threads that asyncio.to_thread hands us.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            # Migration: messages predate embedding-gated recall (they were
            # matched by FTS alone, which injected noise into every prompt).
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(messages)")}
            if "vec" not in cols:
                self._db.execute("ALTER TABLE messages ADD COLUMN vec BLOB")
            # `content` is what the UI shows; `prompt_text` is what the model is
            # replayed. They differ for tool turns: storing only the visible text
            # dropped the <tool_call> blocks, so a resumed conversation became
            # dozens of examples of describing an action instead of taking one.
            if "prompt_text" not in cols:
                self._db.execute("ALTER TABLE messages ADD COLUMN prompt_text TEXT")
            # "web_search" became "lookups" when Wikipedia and live web search
            # split into separate tools. Carry the old value over rather than
            # silently switching the feature off under someone who had it on.
            old = self._db.execute(
                "SELECT value FROM settings WHERE key='web_search'").fetchone()
            has_new = self._db.execute(
                "SELECT 1 FROM settings WHERE key='lookups'").fetchone()
            if old and not has_new:
                self._db.execute(
                    "INSERT INTO settings (key, value) VALUES ('lookups', ?)",
                    (old["value"],))
            self._db.commit()

    # ── sessions ───────────────────────────────────────────────────────────

    def new_session(self) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO sessions (title, created, updated) VALUES ('', ?, ?)",
                (now, now))
            self._db.commit()
            return int(cur.lastrowid)

    def latest_session(self) -> Optional[int]:
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM sessions ORDER BY updated DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def list_sessions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT s.id, s.title, s.updated,
                          (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n
                   FROM sessions s ORDER BY s.updated DESC LIMIT ?""", (limit,)).fetchall()
        return [{"id": r["id"], "title": r["title"] or "New chat",
                 "updated": r["updated"], "count": r["n"]} for r in rows]

    def delete_session(self, session_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._db.commit()

    def touch(self, session_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE sessions SET updated = ? WHERE id = ?",
                             (time.time(), session_id))
            self._db.commit()

    # ── messages ───────────────────────────────────────────────────────────

    def add_message(self, session_id: int, role: str, content: str,
                    vec=None, prompt_text: Optional[str] = None) -> int:
        now = time.time()
        blob = None if vec is None else vec.astype("float32").tobytes()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (session_id, role, content, ts, vec, prompt_text)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, role, content, now, blob, prompt_text))
            # First user line names the chat, so the session list is readable
            # without generating a title (which would cost a whole extra turn).
            if role == "user":
                self._db.execute(
                    "UPDATE sessions SET title = ? WHERE id = ? AND title = ''",
                    (content[:60], session_id))
            self._db.execute("UPDATE sessions SET updated = ? WHERE id = ?",
                             (now, session_id))
            self._db.commit()
            return int(cur.lastrowid)

    def save_card(self, session_id: int, card_id: str, seq: int, kind: str,
                  payload: dict) -> None:
        """Record a card, or update one already recorded.

        Upsert rather than insert: a picture's card is written when it starts
        and again when it finishes, and the finished one is what matters.
        """
        import json as _json
        import time as _time
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO cards (id, session_id, seq, kind, payload, ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload, kind=excluded.kind",
                (card_id, int(session_id), int(seq), kind,
                 _json.dumps(payload), _time.time()))

    def forget_card(self, card_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM cards WHERE id = ?", (card_id,))

    def cards(self, session_id: int) -> list[dict]:
        import json as _json
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, payload FROM cards WHERE session_id = ? "
                "ORDER BY seq, ts", (int(session_id),)).fetchall()
        out = []
        for r in rows:
            try:
                out.append({"id": r["id"], "name": r["kind"],
                            "args": _json.loads(r["payload"])})
            except Exception:
                pass
        return out

    def messages(self, session_id: int, limit: Optional[int] = None) -> list[dict]:
        q = ("SELECT id, role, content, ts, prompt_text FROM messages"
             " WHERE session_id = ? ORDER BY id")
        with self._lock:
            rows = self._db.execute(q, (session_id,)).fetchall()
        out = [{"id": r["id"], "role": r["role"], "content": r["content"], "ts": r["ts"],
                "prompt_text": r["prompt_text"]} for r in rows]
        return out[-limit:] if limit else out

    # ── facts ──────────────────────────────────────────────────────────────

    def set_fact(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO facts (key, value, updated) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                  updated=excluded.updated""",
                (key, value, time.time()))
            self._db.commit()

    def facts(self, limit: int = 40) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, value FROM facts ORDER BY updated DESC LIMIT ?",
                (limit,)).fetchall()
        return [(r["key"], r["value"]) for r in rows]

    # ── summary ────────────────────────────────────────────────────────────

    def summary(self, session_id: int) -> tuple[str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT summary, upto_id FROM summaries WHERE session_id = ?",
                (session_id,)).fetchone()
        return (row["summary"], row["upto_id"]) if row else ("", 0)

    def set_summary(self, session_id: int, summary: str, upto_id: int) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO summaries (session_id, summary, upto_id) VALUES (?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary,
                                                         upto_id=excluded.upto_id""",
                (session_id, summary, upto_id))
            self._db.commit()

    # ── settings ───────────────────────────────────────────────────────────

    def setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?",
                                   (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO settings (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value))
            self._db.commit()

    # ── daily briefing + archive ───────────────────────────────────────────

    def briefing(self, day: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT text FROM briefings WHERE day = ?",
                                   (day,)).fetchone()
        return row["text"] if row else None

    def set_briefing(self, day: str, text: str) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO briefings (day, text, created) VALUES (?,?,?)
                   ON CONFLICT(day) DO UPDATE SET text=excluded.text""",
                (day, text, time.time()))
            self._db.commit()

    def add_chunks(self, day: str, rows: list[dict]) -> None:
        """rows: {source,title,body,link,vec(np.ndarray|None)}"""
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM chunks WHERE day = ?", (day,))  # idempotent rebuild
            self._db.executemany(
                """INSERT INTO chunks (day, source, title, body, link, ts, vec)
                   VALUES (?,?,?,?,?,?,?)""",
                [(day, r["source"], r["title"], r.get("body", ""), r.get("link", ""),
                  now, None if r.get("vec") is None else r["vec"].astype("float32").tobytes())
                 for r in rows])
            self._db.commit()

    def all_chunk_vectors(self) -> tuple[list[int], Optional["np.ndarray"]]:
        """Every stored vector as one matrix, for a single dot product. At ~50 MB
        a year this stays far cheaper than any index would be to maintain."""
        import numpy as np
        with self._lock:
            rows = self._db.execute(
                "SELECT id, vec FROM chunks WHERE vec IS NOT NULL ORDER BY id").fetchall()
        if not rows:
            return [], None
        ids = [int(r["id"]) for r in rows]
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype="float32")
        return ids, mat.reshape(len(ids), -1)

    def chunks_by_id(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT id, day, source, title, body, link FROM chunks WHERE id IN ({qs})",
                ids).fetchall()
        by_id = {int(r["id"]): dict(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def message_vectors(self, exclude_session: Optional[int] = None):
        """Embedded messages from *other* conversations — this session's recent
        turns are already in the prompt verbatim, so recalling them is waste."""
        import numpy as np
        sql = "SELECT id, vec FROM messages WHERE vec IS NOT NULL"
        params: list[Any] = []
        if exclude_session is not None:
            sql += " AND session_id != ?"
            params.append(exclude_session)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", params).fetchall()
        if not rows:
            return [], None
        ids = [int(r["id"]) for r in rows]
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype="float32")
        return ids, mat.reshape(len(ids), -1)

    def messages_by_id(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT id, role, content, ts FROM messages WHERE id IN ({qs})",
                ids).fetchall()
        by_id = {int(r["id"]): dict(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def search_chunks_fts(self, query: str, limit: int = 6) -> list[int]:
        terms = [t for t in _FTS_UNSAFE.sub(" ", query).split() if len(t) > 2]
        if not terms:
            return []
        try:
            with self._lock:
                rows = self._db.execute(
                    """SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?
                       ORDER BY bm25(chunks_fts) LIMIT ?""",
                    (" OR ".join(terms), limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [int(r["rowid"]) for r in rows]

    def prune_chunks(self, keep_days: int) -> int:
        """Bound the archive so storage can't grow without limit."""
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM chunks WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM briefings WHERE created < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    # ── recall ─────────────────────────────────────────────────────────────

    def search(self, query: str, exclude_session: Optional[int] = None,
               limit: int = 4) -> list[dict]:
        """Full-text recall across every past conversation. The current session
        is excluded because its recent turns are already in the prompt verbatim."""
        terms = [t for t in _FTS_UNSAFE.sub(" ", query).split() if len(t) > 2]
        if not terms:
            return []
        # OR rather than AND: a paraphrased question shares few exact words with
        # the original, and bm25 ranking sorts out the noise.
        match = " OR ".join(terms)
        sql = """SELECT m.content, m.role, m.session_id, m.ts
                 FROM messages_fts f JOIN messages m ON m.id = f.rowid
                 WHERE messages_fts MATCH ? {}
                 ORDER BY bm25(messages_fts) LIMIT ?"""
        params: list[Any] = [match]
        clause = ""
        if exclude_session is not None:
            clause = "AND m.session_id != ?"
            params.append(exclude_session)
        params.append(limit)
        try:
            with self._lock:
                rows = self._db.execute(sql.format(clause), params).fetchall()
        except sqlite3.OperationalError:      # malformed MATCH; recall is optional
            return []
        return [{"content": r["content"], "role": r["role"],
                 "session_id": r["session_id"], "ts": r["ts"]} for r in rows]
