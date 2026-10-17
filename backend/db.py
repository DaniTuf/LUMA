"""SQLite storage for users and chat history."""
import os
import sqlite3
import threading
import time
import uuid

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT,
                picture TEXT,
                created REAL NOT NULL,
                last_login REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
                title TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_email, updated DESC);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
            """
        )
        _conn.commit()
    return _conn


def _run(sql: str, params: tuple = (), *, fetch: str | None = None, commit: bool = False):
    with _lock:
        conn = _get()
        cur = conn.execute(sql, params)
        result = None
        if fetch == "one":
            row = cur.fetchone()
            result = dict(row) if row else None
        elif fetch == "all":
            result = [dict(r) for r in cur.fetchall()]
        if commit:
            conn.commit()
        return result if fetch else cur.rowcount


# ---------- users ----------

def upsert_user(email: str, name: str, picture: str) -> dict:
    now = time.time()
    _run(
        """INSERT INTO users (email, name, picture, created, last_login) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET name=excluded.name, picture=excluded.picture, last_login=excluded.last_login""",
        (email, name, picture, now, now),
        commit=True,
    )
    return get_user(email)


def get_user(email: str) -> dict | None:
    return _run("SELECT email, name, picture FROM users WHERE email = ?", (email,), fetch="one")


# ---------- conversations ----------

def create_conversation(email: str, title: str) -> str:
    cid = uuid.uuid4().hex
    now = time.time()
    _run(
        "INSERT INTO conversations (id, user_email, title, created, updated) VALUES (?, ?, ?, ?, ?)",
        (cid, email, title, now, now),
        commit=True,
    )
    return cid


def list_conversations(email: str) -> list[dict]:
    return _run(
        "SELECT id, title, updated FROM conversations WHERE user_email = ? ORDER BY updated DESC LIMIT 200",
        (email,),
        fetch="all",
    )


def get_conversation(email: str, cid: str) -> dict | None:
    """Returns the conversation only if it belongs to this user."""
    return _run(
        "SELECT id, title, updated FROM conversations WHERE id = ? AND user_email = ?",
        (cid, email),
        fetch="one",
    )


def rename_conversation(email: str, cid: str, title: str) -> bool:
    return _run(
        "UPDATE conversations SET title = ? WHERE id = ? AND user_email = ?", (title, cid, email), commit=True
    ) > 0


def delete_conversation(email: str, cid: str) -> bool:
    return _run("DELETE FROM conversations WHERE id = ? AND user_email = ?", (cid, email), commit=True) > 0


# ---------- messages ----------

def add_message(cid: str, role: str, content: str) -> None:
    now = time.time()
    _run("INSERT INTO messages (conversation_id, role, content, created) VALUES (?, ?, ?, ?)", (cid, role, content, now), commit=True)
    _run("UPDATE conversations SET updated = ? WHERE id = ?", (now, cid), commit=True)


def get_messages(cid: str, limit: int | None = None) -> list[dict]:
    if limit:
        rows = _run(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (cid, limit),
            fetch="all",
        )
        return list(reversed(rows))
    return _run("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id", (cid,), fetch="all")
