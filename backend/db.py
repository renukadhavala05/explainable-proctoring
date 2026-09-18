"""
SQLite persistence for the proctoring system.

Uses only the standard library (sqlite3) so there is nothing extra to install.
Stores exam sessions and the contextual incidents produced by the risk engine,
so a proctor can review the history and evidence after the exam.
"""
import sqlite3
import json
import os
import hashlib
import secrets
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

            CREATE TABLE IF NOT EXISTS users (
                username   TEXT PRIMARY KEY,
                role       TEXT,
                name       TEXT,
                pw_salt    TEXT,
                pw_hash    TEXT,
                created_at REAL
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


# ---------------------------------------------------------------------------
# Users / authentication (stdlib pbkdf2 hashing -- no extra dependency)
# ---------------------------------------------------------------------------
def _hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return salt, h


def create_user(username, password, role, name=None):
    salt, h = _hash_pw(password)
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, role, name, pw_salt, pw_hash, created_at) VALUES (?,?,?,?,?,?)",
            (username, role, name or username, salt, h, time.time()),
        )


def get_user(username):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def verify_user(username, password):
    u = get_user(username)
    if not u:
        return None
    _, h = _hash_pw(password, u["pw_salt"])
    if secrets.compare_digest(h, u["pw_hash"]):
        return u
    return None


def ensure_proctor(username, password):
    """Seed a default proctor account if it does not exist yet."""
    if not get_user(username):
        create_user(username, password, "proctor", name="Proctor")


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
