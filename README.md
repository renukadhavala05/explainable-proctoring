# Explainable AI Online Exam Proctoring System

A human-in-the-loop online exam proctoring system that uses the **laptop camera
(primary)** and a **mobile phone (secondary)** to monitor an exam. Instead of
raising a black-box "cheating" alarm, it performs **temporal reasoning** over
detections, correlates the two camera viewpoints, and produces **explainable
contextual incidents** with evidence for a human proctor, who makes the final
decision.

## What it does

| Capability | How |
|---|---|
| Authentication | login for proctor + self-service candidate login (pbkdf2 hashed) |
| Multi-user sessions | each candidate gets an isolated exam session; proctor sees all live candidates on a dashboard |
| Face verification | `face-api.js` 128-d descriptor matching vs an enrolled reference |
| Multiple-person detection | face count + MediaPipe person count |
| Eye detection | eye landmarks + Eye Aspect Ratio (open/closed) |
| Gaze / head-pose (attention) | 68-point landmark geometry (explainable heuristic) |
| Object detection | MediaPipe (WASM): phone, book, extra device |
| Hand detection + hand-object interaction | MediaPipe Hand Landmarker; flags a hand on a phone/book |
| Secure phone pairing | per-session token in a QR on the candidate's exam page |
| Temporal reasoning | sliding-window rules — a signal must be *sustained/repeated* to become an incident |
| Cross-camera correlation | incidents confirmed by both cameras are trusted more |
| Explainable risk | smoothed, decaying weighted score with a per-incident breakdown |
| Human-in-the-loop | proctor reviews feeds, evidence, signals, timeline and confirms/dismisses |

**All AI runs in the browser** (GPU via WebGL) so the Python server stays tiny
and easy to install. The **research core** — temporal reasoning, correlation and
the risk engine — lives in [`backend/risk_engine.py`](backend/risk_engine.py).

## Quick start (tabs demo — no phone needed)

Everything is already installed. Just run:

```
start_demo.bat
```

Then open **http://localhost:8000** — you'll be sent to the **login** page.

- **Proctor:** click *Proctor*, sign in with **admin / admin123** → dashboard of all live candidates.
- **Candidate:** click *Candidate*, enter a name + any roll number + password (first login creates the account) → your own isolated exam page.

Open the candidate in one tab and the proctor in another (sign in as proctor, click the candidate). Two different candidate logins = two isolated sessions, so people no longer collide. The candidate's exam page shows a **QR (with a secure token)** to pair a phone as the secondary camera.

**Demo steps**
1. Open the **Candidate** tab → *Enable Camera* → type a name → *Capture Reference Face* → *Start Exam*.
2. Open the **Proctor** tab (same session id). You'll see the live feed, risk gauge, signals and timeline.
3. Try these to see incidents build up (they only fire when *sustained*, not on a single frame):
   - Look away from the screen for a few seconds → **Looking away**.
   - Hold up a phone → **Mobile phone detected**.
   - Have a second person enter the frame → **More than one person**.
   - Leave the frame → **Candidate not visible**.
4. In the proctor dashboard, **Confirm** or **Dismiss** each incident (human-in-the-loop).

> `localhost` is a browser "secure context", so the laptop camera works over plain
> HTTP — no certificate needed for the tabs demo.

## Real trial with the mobile secondary camera

Phones only allow camera access over HTTPS, so use the HTTPS launcher:

```
start_https.bat
```

- Laptop: open `https://localhost:8443` (accept the self-signed warning once).
- Phone (same Wi-Fi): scan the QR on the landing page, open the link, accept the
  warning, tap *Start secondary camera*.
- Both cameras now feed the same session → the dashboard shows cross-camera
  confirmed incidents.

## Project layout

```
backend/
  main.py         FastAPI: pages, websockets, feed relay, evidence, eval loop
  risk_engine.py  temporal reasoning + cross-camera correlation + risk (the core)
  db.py           SQLite storage of sessions + incidents
static/
  js/detection.js browser AI pipeline (face-api + coco-ssd -> signals)
  js/vendor/      vendored libraries (offline)
  models/         vendored model weights (offline)
templates/        index / candidate / proctor / mobile pages
data/             sqlite database (created on first run)
evidence/         incident snapshots (created on first run)
```

## How the "explainable" part works (for your report / viva)

1. **Observation, not verdict.** Each browser frame emits a *signals* object
   (face present?, identity distance, gaze direction, objects seen…). A single
   detection is never treated as cheating.
2. **Temporal rules.** [`risk_engine.py`](backend/risk_engine.py) keeps a sliding
   window per camera. A rule fires only when a condition holds for a large enough
   fraction of the window (e.g. phone seen in ≥30% of recent frames, or looking
   away ≥70% of the last 5s).
3. **Incidents.** A fired rule becomes an *incident* carrying a plain-English
   explanation, the contributing signals, a confidence, a weight, evidence
   snapshots and the cameras involved.
4. **Cross-camera correlation.** If both cameras support the same finding it is
   marked *cross-camera confirmed* and its confidence/weight are boosted.
5. **Risk score.** A smoothed, decaying weighted sum of active incidents — always
   presented as a breakdown of *what is driving the risk*, never a bare number.
6. **Human decides.** The proctor confirms or dismisses each incident.

## Notes / possible extensions
- Iris-based gaze and dedicated hand-detection can be added as extra signal
  modules feeding the same risk engine (the interface is already signal-based).
- The rule thresholds live at the top of `risk_engine.py` and are easy to tune.
