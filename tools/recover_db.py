"""Rebuild a damaged LUMA SQLite database ("database disk image is malformed") by copying every readable row into a fresh file.

    python tools/recover_db.py data/luma.db data/luma.recovered.db

The damaged file is never modified. Afterwards stop LUMA, move the recovered file over data/luma.db (delete luma.db-wal / -shm), start LUMA.
"""
import os
import sqlite3
import sys

TABLES = [
    ("users", "email, name, picture, created, last_login"),
    ("conversations", "id, user_email, title, created, updated"),
    ("messages", "id, conversation_id, role, content, created"),
]
SCHEMA = """
CREATE TABLE users (email TEXT PRIMARY KEY, name TEXT, picture TEXT, created REAL NOT NULL, last_login REAL NOT NULL);
CREATE TABLE conversations (id TEXT PRIMARY KEY, user_email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE, title TEXT NOT NULL,
                            created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX idx_conv_user ON conversations(user_email, updated DESC);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                       role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX idx_msg_conv ON messages(conversation_id, id);
"""


def main(src: str, dst: str) -> None:
    if os.path.exists(dst):
        raise SystemExit(f"{dst} already exists; choose another name")
    old = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    new = sqlite3.connect(dst)
    new.executescript(SCHEMA)
    for table, cols in TABLES:
        ok = bad = 0
        try:
            rows = old.execute(f"SELECT {cols} FROM {table} NOT INDEXED").fetchall()
        except sqlite3.Error:
            rows = []
            cur = old.execute(f"SELECT {cols} FROM {table}")
            while True:
                try:
                    r = cur.fetchone()
                except sqlite3.Error:
                    bad += 1
                    break
                if r is None:
                    break
                rows.append(r)
        marks = ",".join("?" * len(cols.split(",")))
        for r in rows:
            try:
                new.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({marks})", tuple(r))
                ok += 1
            except sqlite3.Error:
                bad += 1
        print(f"{table}: recovered {ok}, lost {bad}")
    new.commit()
    print("integrity:", new.execute("PRAGMA integrity_check").fetchone()[0])
    new.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
