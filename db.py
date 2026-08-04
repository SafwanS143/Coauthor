"""SQLite storage shared by the presence watcher, the activity poller and the report."""

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS presence_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,          -- ISO-8601 UTC
    person   TEXT NOT NULL,          -- display name as Google renders it, or '*'
    event    TEXT NOT NULL,          -- join|leave|cursor|overflow|disconnected|
                                     -- page_reload|watcher_start|watcher_stop|jserror
    source   TEXT,                   -- chip|cursor|custom|python
    extra    TEXT
);
CREATE INDEX IF NOT EXISTS ix_presence_ts ON presence_events(ts);

CREATE TABLE IF NOT EXISTS edit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    person      TEXT NOT NULL,       -- display name, email, or people/NNN if unresolved
    email       TEXT,
    kind        TEXT,                -- revision|activity:EDIT|activity:COMMENT|...
    revision_id TEXT,
    UNIQUE(ts, person, kind, revision_id)
);
CREATE INDEX IF NOT EXISTS ix_edit_ts ON edit_events(ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    words  INTEGER,
    chars  INTEGER,
    sha256 TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_lock = threading.Lock()


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Idempotent repairs for databases written by earlier versions."""
    # Rows stored before add_edit stopped using NULL revision ids. They sit
    # outside the UNIQUE constraint, so they can be duplicated among themselves
    # and can also collide with the rows written now. Order matters here:
    # rewriting them to '' first would trip the very constraint being restored.
    conn.execute(
        "DELETE FROM edit_events WHERE revision_id IS NULL AND id NOT IN "
        "(SELECT MIN(id) FROM edit_events WHERE revision_id IS NULL "
        " GROUP BY ts, person, kind)"
    )
    conn.execute("UPDATE OR IGNORE edit_events SET revision_id='' WHERE revision_id IS NULL")
    # Anything still NULL lost the race to an identical row that already used
    # the empty string, so it is a duplicate by definition.
    conn.execute("DELETE FROM edit_events WHERE revision_id IS NULL")


@contextmanager
def write(conn):
    """Serialise writes; the Playwright callback thread and the main thread share a conn."""
    with _lock:
        yield conn
        conn.commit()


def add_presence(conn, ts, person, event, source=None, extra=None):
    with write(conn) as c:
        c.execute(
            "INSERT INTO presence_events (ts, person, event, source, extra) VALUES (?,?,?,?,?)",
            (ts, person, event, source, extra),
        )


def add_edit(conn, ts, person, email=None, kind=None, revision_id=None):
    # Empty string, never NULL. SQL treats every NULL as distinct from every
    # other NULL, so a UNIQUE constraint containing one silently stops
    # deduplicating: Drive Activity rows carry no revision id, and the poller
    # re-reads an overlapping window every five minutes, so each event would be
    # stored two or three times and inflate the edit count.
    with write(conn) as c:
        c.execute(
            "INSERT OR IGNORE INTO edit_events (ts, person, email, kind, revision_id) "
            "VALUES (?,?,?,?,?)",
            (ts, person, email, kind, revision_id or ""),
        )


def add_snapshot(conn, ts, words, chars, sha256):
    with write(conn) as c:
        c.execute(
            "INSERT INTO snapshots (ts, words, chars, sha256) VALUES (?,?,?,?)",
            (ts, words, chars, sha256),
        )


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    with write(conn) as c:
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, str(value)))
