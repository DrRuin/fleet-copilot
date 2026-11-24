"""Append-only audit log and the record of executed actions, in one SQLite file."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

SCHEMA = """
create table if not exists audit (
    id integer primary key, at text, company text, session text, actor text, event text, detail text);
create table if not exists actions (
    key text primary key, id text, company text, kind text, target text, args text,
    status text, approver text, note text, at text);
create trigger if not exists audit_append_only before update on audit begin select raise(abort, 'append only'); end;
create trigger if not exists audit_no_delete before delete on audit begin select raise(abort, 'append only'); end;
"""


class Store:
    """Writes every event, and executes an approved action at most once per key."""

    def __init__(self, path: Path):
        """Open the file and create tables on first use."""
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.execute("pragma journal_mode=wal")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = Lock()

    def log(self, company: str, session: str, actor: str, event: str, detail: dict) -> None:
        """Append one audit event."""
        with self.lock, self.db:
            self.db.execute(
                "insert into audit (at, company, session, actor, event, detail) values (?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), company, session, actor, event, json.dumps(detail, default=str)),
            )

    def action(self, key: str) -> dict | None:
        """An executed or rejected action by its key."""
        row = self.db.execute("select * from actions where key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def execute(self, key: str, company: str, kind: str, target: str, args: dict, approver: str, note: str) -> str:
        """Record the action as done and return its reference, or the existing one when the key was already executed."""
        with self.lock, self.db:
            done = self.db.execute("select id from actions where key = ? and status = 'executed'", (key,)).fetchone()
            if done:
                return done["id"]
            count = self.db.execute("select count(*) from actions where kind = ?", (kind,)).fetchone()[0]
            ref = f"{kind.split('_')[0].upper()[:3]}-{count + 1:04d}"
            self.db.execute(
                "insert or replace into actions values (?, ?, ?, ?, ?, ?, 'executed', ?, ?, ?)",
                (key, ref, company, kind, target, json.dumps(args), approver, note, datetime.now(UTC).isoformat()),
            )
            return ref

    def audit(self, company: str, limit: int = 200) -> list[dict]:
        """Recent audit events for one company, newest first."""
        rows = self.db.execute(
            "select * from audit where company = ? order by id desc limit ?", (company, limit)
        ).fetchall()
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]

    def actions(self, company: str) -> list[dict]:
        """Executed actions for one company, newest first."""
        rows = self.db.execute("select * from actions where company = ? order by at desc", (company,)).fetchall()
        return [dict(r) | {"args": json.loads(r["args"])} for r in rows]
