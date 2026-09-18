"""
FastAPI server for the explainable proctoring system (multi-user edition).

Adds on top of the detection pipeline:
  * Login / authentication (one proctor account + self-service candidate login)
  * Per-candidate isolated exam sessions (no more collisions between people)
  * A proctor dashboard listing every live candidate
  * Secure per-session pairing tokens so the correct phone joins the correct
    candidate as the secondary camera (QR shown on the candidate's exam page)
"""
import asyncio
import base64
import mimetypes
import os
import secrets
import socket
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .risk_engine import SessionRisk

# Windows often lacks these registrations; ES modules and WASM need correct MIME.
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/wasm", ".wasm")

ROOT = os.path.dirname(os.path.dirname(__file__))
STATIC = os.path.join(ROOT, "static")
TEMPLATES = os.path.join(ROOT, "templates")
EVIDENCE = os.path.join(ROOT, "evidence")
os.makedirs(EVIDENCE, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "proctoring-demo-secret-change-me")
PROCTOR_USER = os.environ.get("PROCTOR_USER", "admin")
PROCTOR_PASS = os.environ.get("PROCTOR_PASS", "admin123")

app = FastAPI(title="Explainable Proctoring System")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")


# ---------------------------------------------------------------------------
# In-memory hub
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.sessions = {}            # sid -> SessionRisk
        self.proctors = {}            # sid -> set[WebSocket]
        self.candidates = {}          # sid -> set[WebSocket]
        self.frame_bytes = {}         # (sid, cam) -> raw jpeg bytes (latest)
        self.session_tokens = {}      # sid -> pairing token

    def get_session(self, sid):
        if sid not in self.sessions:
            s = SessionRisk(sid)
            s.on_evidence_request = self.save_evidence
            self.sessions[sid] = s
            db.upsert_session(sid)
        return self.sessions[sid]

    def new_token(self, sid, candidate=None):
        session = self.get_session(sid)
        if candidate:
            session.candidate_name = candidate
        token = secrets.token_urlsafe(8)
        self.session_tokens[sid] = token
        return token

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
        for ws in list(self.proctors.get(sid, set())):
            try:
                await ws.send_json(message)
            except Exception:
                self.proctors.get(sid, set()).discard(ws)

    async def to_candidates(self, sid, message):
        for ws in list(self.candidates.get(sid, set())):
            try:
                await ws.send_json(message)
            except Exception:
                pass


hub = Hub()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup():
    db.init_db()
    db.ensure_proctor(PROCTOR_USER, PROCTOR_PASS)
    asyncio.create_task(evaluation_loop())


async def evaluation_loop():
    while True:
        for sid, session in list(hub.sessions.items()):
            try:
                state = session.evaluate()
                for inc in state["active_incidents"]:
                    db.save_incident(inc)
                await hub.to_proctors(sid, state)
                await _maybe_warn_candidate(sid, state)
            except Exception as e:
                print("eval error", sid, e)
        await asyncio.sleep(0.7)


async def _maybe_warn_candidate(sid, state):
    warnings = []
    for inc in state["active_incidents"]:
        t = inc["type"]
        if t == "FACE_ABSENT":
            warnings.append("Please stay in front of the camera.")
        elif t == "LOOKING_AWAY":
            warnings.append("Please keep your eyes on the screen.")
        elif t == "PHONE_DETECTED":
            warnings.append("Mobile phones are not allowed during the exam.")
        elif t == "MULTIPLE_PEOPLE":
            warnings.append("Only the candidate may be present.")
        elif t == "HAND_ON_OBJECT":
            warnings.append("Please keep your hands away from prohibited items.")
    payload = {"type": "warning" if warnings else "ok", "messages": warnings,
               "risk": state["risk"], "risk_level": state["risk_level"]}
    await hub.to_candidates(sid, payload)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def current_user(request: Request):
    return request.session.get("user")


def _page(name):
    return FileResponse(os.path.join(TEMPLATES, name))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/")
def index(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login")
    if u["role"] == "proctor":
        return RedirectResponse("/dashboard")
    return RedirectResponse(f"/candidate?session={u['sid']}&token={hub.session_tokens.get(u['sid'], '')}")


@app.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/")
    return _page("login.html")


@app.get("/dashboard")
def dashboard_page(request: Request):
    u = current_user(request)
    if not u or u["role"] != "proctor":
        return RedirectResponse("/login")
    return _page("dashboard.html")


@app.get("/candidate")
def candidate_page(request: Request):
    u = current_user(request)
    if not u or u["role"] != "candidate":
        return RedirectResponse("/login")
    return _page("candidate.html")


@app.get("/proctor")
def proctor_page(request: Request):
    u = current_user(request)
    if not u or u["role"] != "proctor":
        return RedirectResponse("/login")
    return _page("proctor.html")


@app.get("/mobile")
def mobile_page():
    # No login: the phone pairs using the secure token in the URL (validated on WS).
    return _page("mobile.html")


@app.get("/diag")
def diag_page():
    return _page("diag.html")


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------
@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    role = (data.get("role") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    name = (data.get("name") or username).strip()

    if not username or not password:
        return JSONResponse({"error": "Username and password are required."}, status_code=400)

    if role == "proctor":
        u = db.verify_user(username, password)
        if not u or u["role"] != "proctor":
            return JSONResponse({"error": "Invalid proctor credentials."}, status_code=401)
        request.session["user"] = {"role": "proctor", "username": username, "name": u["name"]}
        return {"redirect": "/dashboard"}

    # candidate: self-service — create on first login, verify thereafter
    existing = db.get_user(username)
    if existing:
        if existing["role"] != "candidate" or not db.verify_user(username, password):
            return JSONResponse({"error": "That ID is taken or the password is wrong."}, status_code=401)
        name = existing["name"]
    else:
        db.create_user(username, password, "candidate", name=name)

    sid = f"exam_{username}"
    token = hub.new_token(sid, candidate=name)
    db.upsert_session(sid, candidate=name)
    request.session["user"] = {"role": "candidate", "username": username, "name": name, "sid": sid}
    return {"redirect": f"/candidate?session={sid}&token={token}"}


@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return {"redirect": "/login"}


@app.get("/api/me")
def api_me(request: Request):
    return current_user(request) or {}


# ---------------------------------------------------------------------------
# Other REST
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


@app.get("/api/active")
def api_active(request: Request):
    u = current_user(request)
    if not u or u["role"] != "proctor":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    out = []
    for sid, s in hub.sessions.items():
        out.append({
            "sid": sid,
            "candidate": getattr(s, "candidate_name", "Candidate"),
            "risk": round(s.risk, 1),
            "risk_level": SessionRisk.level(s.risk),
            "cameras_online": {c: s.has_camera(c) for c in ("candidate", "mobile")},
            "active_incidents": len(s.active),
            "token": hub.session_tokens.get(sid, ""),
        })
    out.sort(key=lambda x: -x["risk"])
    return out


@app.get("/api/sessions/{sid}/incidents")
def api_incidents(request: Request, sid: str):
    u = current_user(request)
    if not u or u["role"] != "proctor":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(db.list_incidents(sid))


# ---------------------------------------------------------------------------
# WebSocket (token-secured)
# ---------------------------------------------------------------------------
@app.websocket("/ws/{role}/{sid}")
async def ws_endpoint(ws: WebSocket, role: str, sid: str):
    token = ws.query_params.get("token")

    if role == "proctor":
        user = ws.session.get("user") if "session" in ws.scope else None
        if not user or user.get("role") != "proctor":
            await ws.close(code=1008)
            return
    elif role in ("candidate", "mobile"):
        if not token or hub.session_tokens.get(sid) != token:
            await ws.close(code=1008)
            return
    else:
        await ws.close(code=1008)
        return

    await ws.accept()
    session = hub.get_session(sid)

    if role == "proctor":
        hub.proctors.setdefault(sid, set()).add(ws)
        await ws.send_json(session.state())
    else:
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
                    try:
                        hub.frame_bytes[(sid, cam)] = base64.b64decode(image.split(",", 1)[1])
                    except Exception:
                        pass
                await hub.to_proctors(sid, {"type": "frame", "camera": cam,
                                            "image": image, "ts": time.time()})

            elif mtype == "enroll":
                name = msg.get("candidate")
                if name:
                    session.candidate_name = name
                db.upsert_session(sid, candidate=name, enrolled=True)
                await hub.to_proctors(sid, {"type": "enrolled", "candidate": name})

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
