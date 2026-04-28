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

APP_DIR = os.path.expanduser("~/Library/Application Support/my_ai")
DB_PATH = os.path.join(APP_DIR, "my_ai.sqlite3")

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

CREATE TABLE IF NOT EXISTS facts (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL NOT NULL
);
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

    def add_message(self, session_id: int, role: str, content: str) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (session_id, role, content, ts) VALUES (?,?,?,?)",
                (session_id, role, content, now))
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

    def messages(self, session_id: int, limit: Optional[int] = None) -> list[dict]:
        q = "SELECT id, role, content, ts FROM messages WHERE session_id = ? ORDER BY id"
        with self._lock:
            rows = self._db.execute(q, (session_id,)).fetchall()
        out = [{"id": r["id"], "role": r["role"], "content": r["content"], "ts": r["ts"]}
               for r in rows]
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
