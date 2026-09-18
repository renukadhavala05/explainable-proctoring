"""
SQLite persistence for the proctoring system.

Uses only the standard library (sqlite3) so there is nothing extra to install.
Stores exam sessions and the contextual incidents produced by the risk engine,
so a proctor can review the history and evidence after the exam.
"""
import sqlite3
import json
import os
import threading
import time

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "proctoring.db")

_lock = threading.Lock()


def _connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id            TEXT PRIMARY KEY,
                candidate     TEXT,
                started_at    REAL,
                ended_at      REAL,
                enrolled      INTEGER DEFAULT 0,
                max_risk      REAL DEFAULT 0,
                incident_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS incidents (
                id            TEXT PRIMARY KEY,
                session_id    TEXT,
                type          TEXT,
                title         TEXT,
                severity      TEXT,
                weight        REAL,
                confidence    REAL,
                cameras       TEXT,
                cross_camera  INTEGER,
                start_ts      REAL,
                last_ts       REAL,
                duration      REAL,
                explanation   TEXT,
                signals       TEXT,
                evidence      TEXT,
                status        TEXT,
                review_action TEXT,
                review_note   TEXT,
                updated_at    REAL
            );
            """
        )


def upsert_session(session_id, candidate=None, enrolled=None):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sessions (id, candidate, started_at, enrolled) VALUES (?,?,?,?)",
                (session_id, candidate or "Candidate", time.time(), 1 if enrolled else 0),
            )
        else:
            if candidate is not None:
                conn.execute("UPDATE sessions SET candidate=? WHERE id=?", (candidate, session_id))
            if enrolled is not None:
                conn.execute("UPDATE sessions SET enrolled=? WHERE id=?", (1 if enrolled else 0, session_id))


def end_session(session_id, max_risk, incident_count):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at=?, max_risk=?, incident_count=? WHERE id=?",
            (time.time(), max_risk, incident_count, session_id),
        )


def save_incident(inc):
    """inc is the incident dict produced by the risk engine."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO incidents
                (id, session_id, type, title, severity, weight, confidence, cameras,
                 cross_camera, start_ts, last_ts, duration, explanation, signals,
                 evidence, status, review_action, review_note, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                last_ts=excluded.last_ts, duration=excluded.duration,
                confidence=excluded.confidence, weight=excluded.weight,
                explanation=excluded.explanation, signals=excluded.signals,
                evidence=excluded.evidence, status=excluded.status,
                review_action=excluded.review_action, review_note=excluded.review_note,
                updated_at=excluded.updated_at
            """,
            (
                inc["id"], inc["session_id"], inc["type"], inc["title"], inc["severity"],
                inc["weight"], inc["confidence"], json.dumps(inc["cameras"]),
                1 if inc["cross_camera"] else 0, inc["start_ts"], inc["last_ts"],
                inc["duration"], inc["explanation"], json.dumps(inc["contributing_signals"]),
                json.dumps(inc["evidence"]), inc["status"],
                inc.get("review", {}).get("action"), inc.get("review", {}).get("note"),
                time.time(),
            ),
        )


def set_review(incident_id, action, note):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE incidents SET review_action=?, review_note=?, updated_at=? WHERE id=?",
            (action, note, time.time(), incident_id),
        )


def list_sessions():
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
        return [dict(r) for r in rows]


def list_incidents(session_id):
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM incidents WHERE session_id=? ORDER BY start_ts ASC", (session_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["cameras"] = json.loads(d["cameras"] or "[]")
            d["signals"] = json.loads(d["signals"] or "[]")
            d["evidence"] = json.loads(d["evidence"] or "[]")
            d["cross_camera"] = bool(d["cross_camera"])
            out.append(d)
        return out
