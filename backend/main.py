"""
FastAPI server for the explainable proctoring system.

Responsibilities:
  * serve the candidate / mobile / proctor web pages and the vendored AI libs
  * accept WebSocket connections from each camera and from the proctor
  * relay the live video frames to the proctor dashboard
  * feed detection signals into a per-session risk engine
  * capture evidence snapshots when incidents are raised
  * run one evaluation loop (~1.4 Hz) that updates risk and pushes state
"""
import asyncio
import base64
import mimetypes
import os
import socket
import time

# Windows often lacks these registrations; ES modules and WASM need correct MIME.
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/wasm", ".wasm")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .risk_engine import SessionRisk

ROOT = os.path.dirname(os.path.dirname(__file__))
STATIC = os.path.join(ROOT, "static")
TEMPLATES = os.path.join(ROOT, "templates")
EVIDENCE = os.path.join(ROOT, "evidence")
os.makedirs(EVIDENCE, exist_ok=True)

app = FastAPI(title="Explainable Proctoring System")


# ---------------------------------------------------------------------------
# In-memory hub
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.sessions = {}            # sid -> SessionRisk
        self.proctors = {}            # sid -> set[WebSocket]
        self.candidates = {}          # sid -> set[WebSocket]  (candidate + mobile pages)
        self.frame_bytes = {}         # (sid, cam) -> raw jpeg bytes (latest)

    def get_session(self, sid):
        if sid not in self.sessions:
            s = SessionRisk(sid)
            s.on_evidence_request = self.save_evidence
            self.sessions[sid] = s
            db.upsert_session(sid)
        return self.sessions[sid]

    def save_evidence(self, sid, camera, incident_id):
        data = self.frame_bytes.get((sid, camera))
        if not data:
            return None
        folder = os.path.join(EVIDENCE, sid)
        os.makedirs(folder, exist_ok=True)
        fname = f"{incident_id}_{camera}.jpg"
        with open(os.path.join(folder, fname), "wb") as f:
            f.write(data)
        return f"/evidence/{sid}/{fname}"

    async def to_proctors(self, sid, message):
        dead = []
        for ws in list(self.proctors.get(sid, set())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.proctors.get(sid, set()).discard(ws)

    async def to_candidates(self, sid, message):
        for ws in list(self.candidates.get(sid, set())):
            try:
                await ws.send_json(message)
            except Exception:
                pass


hub = Hub()


# ---------------------------------------------------------------------------
# Startup: DB + evaluation loop
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup():
    db.init_db()
    asyncio.create_task(evaluation_loop())


async def evaluation_loop():
    """Single loop that evaluates every active session and pushes state."""
    while True:
        for sid, session in list(hub.sessions.items()):
            try:
                state = session.evaluate()
                # persist incidents (active + newly ended)
                for inc in state["active_incidents"]:
                    db.save_incident(inc)
                await hub.to_proctors(sid, state)
                await _maybe_warn_candidate(sid, state)
            except Exception as e:  # keep the loop alive no matter what
                print("eval error", sid, e)
        await asyncio.sleep(0.7)


async def _maybe_warn_candidate(sid, state):
    warnings = []
    for inc in state["active_incidents"]:
        if inc["type"] == "FACE_ABSENT":
            warnings.append("Please stay in front of the camera.")
        elif inc["type"] == "LOOKING_AWAY":
            warnings.append("Please keep your eyes on the screen.")
        elif inc["type"] == "PHONE_DETECTED":
            warnings.append("Mobile phones are not allowed during the exam.")
        elif inc["type"] == "MULTIPLE_PEOPLE":
            warnings.append("Only the candidate may be present.")
    if warnings:
        await hub.to_candidates(sid, {"type": "warning", "messages": warnings,
                                      "risk": state["risk"], "risk_level": state["risk_level"]})
    else:
        await hub.to_candidates(sid, {"type": "ok", "risk": state["risk"],
                                      "risk_level": state["risk_level"]})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _page(name):
    return FileResponse(os.path.join(TEMPLATES, name))


@app.get("/")
def index():
    return _page("index.html")


@app.get("/candidate")
def candidate_page():
    return _page("candidate.html")


@app.get("/proctor")
def proctor_page():
    return _page("proctor.html")


@app.get("/mobile")
def mobile_page():
    return _page("mobile.html")


@app.get("/diag")
def diag_page():
    return _page("diag.html")


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------
def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/api/config")
def api_config():
    return {"lan_ip": _lan_ip(), "https_port": int(os.environ.get("HTTPS_PORT", "8443"))}


@app.get("/api/sessions")
def api_sessions():
    return JSONResponse(db.list_sessions())


@app.get("/api/sessions/{sid}/incidents")
def api_incidents(sid: str):
    return JSONResponse(db.list_incidents(sid))


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws/{role}/{sid}")
async def ws_endpoint(ws: WebSocket, role: str, sid: str):
    await ws.accept()
    session = hub.get_session(sid)

    if role == "proctor":
        hub.proctors.setdefault(sid, set()).add(ws)
        # send an immediate snapshot so the dashboard is not blank
        await ws.send_json(session.state())
    else:  # candidate or mobile
        hub.candidates.setdefault(sid, set()).add(ws)

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "signal":
                session.add_signal(msg.get("role", role), msg.get("signals", {}))

            elif mtype == "frame":
                cam = msg.get("role", role)
                image = msg.get("image", "")
                if image.startswith("data:"):
                    b64 = image.split(",", 1)[1]
                    try:
                        hub.frame_bytes[(sid, cam)] = base64.b64decode(b64)
                    except Exception:
                        pass
                await hub.to_proctors(sid, {"type": "frame", "camera": cam,
                                            "image": image, "ts": time.time()})

            elif mtype == "enroll":
                db.upsert_session(sid, candidate=msg.get("candidate"), enrolled=True)
                await hub.to_proctors(sid, {"type": "enrolled",
                                            "candidate": msg.get("candidate")})

            elif mtype == "review":
                session.set_review(msg.get("incident_id"), msg.get("action"), msg.get("note"))
                db.set_review(msg.get("incident_id"), msg.get("action"), msg.get("note"))

            elif mtype == "end":
                db.end_session(sid, session.max_risk, len(session.history))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("ws error", role, sid, e)
    finally:
        hub.proctors.get(sid, set()).discard(ws)
        hub.candidates.get(sid, set()).discard(ws)


# static mounts (after routes so they don't shadow them)
app.mount("/evidence", StaticFiles(directory=EVIDENCE), name="evidence")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
