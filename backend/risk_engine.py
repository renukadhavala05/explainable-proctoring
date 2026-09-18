"""
Explainable risk engine  --  the research core of the project.

Key idea (the contribution):
    Individual detections (a phone appears for one frame, the head turns for a
    moment) are NOT treated as cheating. Instead every detection is stored as a
    time-stamped *observation*. A set of temporal RULES look across a sliding
    window and only raise a contextual INCIDENT when a condition is *sustained*
    or *repeated* enough to be meaningful. Each incident carries:
        - a plain-English explanation
        - the contributing signals (why it fired)
        - a confidence and a weight (how much it adds to the risk score)
        - the camera(s) that contributed, and whether it was cross-camera confirmed
        - evidence snapshots

    When two cameras (laptop + mobile) independently support the same finding,
    the incident is marked cross-camera confirmed and its confidence is boosted.

The final risk score is a smoothed, decaying, weighted sum of active incidents,
so it rises quickly on strong evidence and fades slowly when behaviour returns
to normal -- and it is always explainable as a list of contributions.
"""
import time
import uuid
from collections import deque

# ----------------------------------------------------------------------------
# Tunable parameters (kept here so they are easy to explain / defend in a viva)
# ----------------------------------------------------------------------------
WINDOW = 8.0            # seconds of history kept per camera
EVAL_WINDOW = 5.0       # seconds most rules look back over
MIN_FRAMES = 2          # need at least this many observations to judge a window

# Objects that matter for proctoring (COCO class names from coco-ssd)
PROHIBITED = {"cell phone", "book", "laptop", "tv", "remote", "keyboard", "mouse"}
PHONE_CLASSES = {"cell phone", "remote"}          # remote is often a mis-read phone
NOTE_CLASSES = {"book"}
DEVICE_CLASSES = {"laptop", "tv", "keyboard", "mouse"}

RISK_UP = 0.5           # how fast risk rises toward the target
RISK_DOWN = 0.08        # how slowly risk decays when things calm down


def _now():
    return time.time()


class Incident:
    def __init__(self, itype, title, severity, weight):
        self.id = uuid.uuid4().hex[:12]
        self.type = itype
        self.title = title
        self.severity = severity          # low / medium / high
        self.base_weight = weight
        self.weight = weight
        self.confidence = 0.0
        self.cameras = set()
        self.cross_camera = False
        self.start_ts = _now()
        self.last_ts = self.start_ts
        self.explanation = ""
        self.contributing_signals = []
        self.evidence = []
        self.status = "active"
        self.review = {"action": None, "note": None}

    def to_dict(self, session_id):
        return {
            "id": self.id,
            "session_id": session_id,
            "type": self.type,
            "title": self.title,
            "severity": self.severity,
            "weight": round(self.weight, 1),
            "confidence": round(self.confidence, 2),
            "cameras": sorted(self.cameras),
            "cross_camera": self.cross_camera,
            "start_ts": self.start_ts,
            "last_ts": self.last_ts,
            "duration": round(self.last_ts - self.start_ts, 1),
            "explanation": self.explanation,
            "contributing_signals": self.contributing_signals,
            "evidence": self.evidence,
            "status": self.status,
            "review": self.review,
        }


class SessionRisk:
    """Holds all temporal state and incidents for one exam session."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.candidate_name = "Candidate"
        self.buffers = {"candidate": deque(), "mobile": deque()}
        self.latest = {"candidate": None, "mobile": None}
        self.active = {}          # type -> Incident  (currently live incidents)
        self.history = []         # list of incident dicts (for the timeline)
        self.risk = 0.0
        self.max_risk = 0.0
        self.on_evidence_request = None   # callback(session_id, camera, incident_id)

    # -- ingest -------------------------------------------------------------
    def add_signal(self, camera, signals):
        if camera not in self.buffers:
            return
        ts = _now()
        signals = dict(signals or {})
        signals["_ts"] = ts
        self.buffers[camera].append(signals)
        self.latest[camera] = signals
        self._prune(ts)

    def _prune(self, ts):
        for buf in self.buffers.values():
            while buf and ts - buf[0]["_ts"] > WINDOW:
                buf.popleft()

    def has_camera(self, camera):
        return len(self.buffers.get(camera, [])) > 0 and \
            (_now() - self.buffers[camera][-1]["_ts"] < 3.0)

    # -- window helpers -----------------------------------------------------
    def _recent(self, camera, window=EVAL_WINDOW):
        ts = _now()
        return [s for s in self.buffers[camera] if ts - s["_ts"] <= window]

    @staticmethod
    def _fraction(frames, predicate):
        if len(frames) < MIN_FRAMES:
            return 0.0, 0
        hits = sum(1 for f in frames if predicate(f))
        return hits / len(frames), hits

    # -- main evaluation ----------------------------------------------------
    def evaluate(self):
        """Run all rules, update incidents and risk. Returns a state dict."""
        findings = {}
        for cam in ("candidate", "mobile"):
            if self.has_camera(cam):
                self._rules_for_camera(cam, findings)

        self._correlate(findings)
        self._reconcile(findings)
        self._update_risk()
        return self.state()

    # -- per-camera temporal rules -----------------------------------------
    def _rules_for_camera(self, cam, findings):
        frames = self._recent(cam)
        if len(frames) < MIN_FRAMES:
            return

        # Face-centric rules only make sense on the primary (laptop) camera,
        # which is pointed at the candidate. The mobile watches the desk and
        # will legitimately not always see a face.
        primary = cam == "candidate"

        # FACE ABSENT ------------------------------------------------------
        if primary:
            frac_absent, n = self._fraction(frames, lambda f: not f.get("face_present"))
            if frac_absent >= 0.7:
                self._merge(findings, "FACE_ABSENT",
                            title="Candidate not visible",
                            severity="high", weight=30, conf=frac_absent, cam=cam,
                            signal=f"No face detected in {int(frac_absent*100)}% of the last {int(EVAL_WINDOW)}s ({cam} camera)")

        # MULTIPLE FACES / EXTRA PERSON ------------------------------------
        frac_multi, _ = self._fraction(frames, lambda f: (f.get("face_count") or 0) >= 2 or (f.get("person_count") or 0) >= 2)
        if frac_multi >= 0.4:
            self._merge(findings, "MULTIPLE_PEOPLE",
                        title="More than one person present",
                        severity="high", weight=35, conf=frac_multi, cam=cam,
                        signal=f"Two or more people seen in {int(frac_multi*100)}% of recent frames ({cam} camera)")

        # IDENTITY MISMATCH ------------------------------------------------
        idframes = [f for f in frames if f.get("face_present") and f.get("identity_match") is not None]
        frac_bad, _ = self._fraction(idframes, lambda f: not f.get("identity_match"))
        if primary and idframes and frac_bad >= 0.6:
            avg_d = sum(f.get("identity_distance", 1.0) for f in idframes) / len(idframes)
            self._merge(findings, "IDENTITY_MISMATCH",
                        title="Face does not match enrolled candidate",
                        severity="high", weight=30, conf=frac_bad, cam=cam,
                        signal=f"Live face did not match the enrolled reference (avg distance {avg_d:.2f}, {cam} camera)")

        # LOOKING AWAY (sustained) -----------------------------------------
        away_frames = [f for f in frames if f.get("face_present")]
        frac_away, _ = self._fraction(away_frames, lambda f: f.get("looking_away"))
        if primary and away_frames and frac_away >= 0.7:
            dirs = [f.get("gaze_direction", "away") for f in away_frames if f.get("looking_away")]
            direction = max(set(dirs), key=dirs.count) if dirs else "away"
            self._merge(findings, "LOOKING_AWAY",
                        title="Sustained looking away from screen",
                        severity="medium", weight=15, conf=frac_away, cam=cam,
                        signal=f"Head/gaze turned {direction} for {int(frac_away*100)}% of the last {int(EVAL_WINDOW)}s ({cam} camera)")

        # PHONE ------------------------------------------------------------
        frac_phone, _ = self._fraction(frames, lambda f: any(o.get("class") in PHONE_CLASSES for o in f.get("objects", [])))
        if frac_phone >= 0.3:
            self._merge(findings, "PHONE_DETECTED",
                        title="Mobile phone detected",
                        severity="high", weight=30, conf=frac_phone, cam=cam,
                        signal=f"A phone was detected in {int(frac_phone*100)}% of recent frames ({cam} camera)")

        # NOTES / BOOK -----------------------------------------------------
        frac_book, _ = self._fraction(frames, lambda f: any(o.get("class") in NOTE_CLASSES for o in f.get("objects", [])))
        if frac_book >= 0.3:
            self._merge(findings, "NOTES_DETECTED",
                        title="Book / notes detected",
                        severity="medium", weight=18, conf=frac_book, cam=cam,
                        signal=f"Book or notes visible in {int(frac_book*100)}% of recent frames ({cam} camera)")

        # EXTRA DEVICE -----------------------------------------------------
        frac_dev, _ = self._fraction(frames, lambda f: any(o.get("class") in DEVICE_CLASSES for o in f.get("objects", [])))
        if frac_dev >= 0.4:
            self._merge(findings, "EXTRA_DEVICE",
                        title="Additional device detected",
                        severity="low", weight=10, conf=frac_dev, cam=cam,
                        signal=f"An extra device was visible in {int(frac_dev*100)}% of recent frames ({cam} camera)")

        # HAND - OBJECT INTERACTION ----------------------------------------
        frac_hand, _ = self._fraction(frames, lambda f: f.get("hand_on_object"))
        if frac_hand >= 0.35:
            objs = [f.get("hand_object_class") for f in frames if f.get("hand_on_object") and f.get("hand_object_class")]
            obj = max(set(objs), key=objs.count) if objs else "an item"
            is_phone = obj == "cell phone"
            self._merge(findings, "HAND_ON_OBJECT",
                        title=f"Hand interacting with {obj}",
                        severity="high" if is_phone else "medium",
                        weight=28 if is_phone else 16, conf=frac_hand, cam=cam,
                        signal=f"A hand was on/near {obj} in {int(frac_hand*100)}% of recent frames ({cam} camera)")

    def _merge(self, findings, itype, title, severity, weight, conf, cam, signal):
        f = findings.get(itype)
        if f is None:
            findings[itype] = {
                "title": title, "severity": severity, "weight": weight,
                "confidence": conf, "cameras": {cam}, "signals": [signal],
            }
        else:
            f["cameras"].add(cam)
            f["confidence"] = max(f["confidence"], conf)
            f["signals"].append(signal)

    # -- cross-camera correlation ------------------------------------------
    def _correlate(self, findings):
        """Boost findings that both cameras agree on, and add correlated ones."""
        for itype, f in findings.items():
            if len(f["cameras"]) >= 2:
                f["cross_camera"] = True
                f["confidence"] = min(1.0, f["confidence"] * 1.25 + 0.1)
                f["weight"] = f["weight"] * 1.3
                f["signals"].append("Confirmed independently by BOTH the laptop and mobile cameras")
            else:
                f["cross_camera"] = False

        # Correlated phone-use: laptop shows head/gaze down + a phone anywhere
        cand = self.latest.get("candidate")
        if self.has_camera("candidate") and cand and cand.get("gaze_direction") == "down":
            if "PHONE_DETECTED" in findings:
                pf = findings["PHONE_DETECTED"]
                pf["confidence"] = min(1.0, pf["confidence"] + 0.15)
                pf["signals"].append("Candidate was looking down while the phone was visible (consistent with phone use)")

    # -- turn findings into live incidents ---------------------------------
    def _reconcile(self, findings):
        now = _now()
        # update / create
        for itype, f in findings.items():
            inc = self.active.get(itype)
            if inc is None:
                inc = Incident(itype, f["title"], f["severity"], f["weight"])
                self.active[itype] = inc
                self._request_evidence(inc, f["cameras"])
            inc.last_ts = now
            inc.weight = f["weight"]
            inc.confidence = f["confidence"]
            inc.cameras = set(f["cameras"])
            inc.cross_camera = f.get("cross_camera", False)
            inc.contributing_signals = f["signals"]
            inc.explanation = self._explain(inc)

        # end incidents whose condition disappeared
        ended = [t for t in self.active if t not in findings]
        for t in ended:
            inc = self.active.pop(t)
            inc.status = "ended"
            inc.last_ts = now
            self._archive(inc)

        # keep the max risk / active list fresh in history for active ones
        for inc in self.active.values():
            self._archive(inc, live=True)

    def _explain(self, inc):
        cams = " + ".join(sorted(inc.cameras))
        conf = int(inc.confidence * 100)
        base = f"{inc.title}. Evidence sustained over time on the {cams} camera(s); confidence {conf}%."
        if inc.cross_camera:
            base += " This was confirmed from two independent viewpoints, which strongly increases reliability."
        return base

    def _archive(self, inc, live=False):
        d = inc.to_dict(self.session_id)
        # replace existing history entry with same id, else append
        for i, h in enumerate(self.history):
            if h["id"] == inc.id:
                self.history[i] = d
                return
        self.history.append(d)

    def _request_evidence(self, inc, cameras):
        if self.on_evidence_request:
            for cam in cameras:
                path = self.on_evidence_request(self.session_id, cam, inc.id)
                if path:
                    inc.evidence.append(path)

    # -- risk score ---------------------------------------------------------
    def _update_risk(self):
        target = 0.0
        for inc in self.active.values():
            target += inc.weight * inc.confidence
        target = min(100.0, target)
        alpha = RISK_UP if target > self.risk else RISK_DOWN
        self.risk += (target - self.risk) * alpha
        self.risk = max(0.0, min(100.0, self.risk))
        self.max_risk = max(self.max_risk, self.risk)

    @staticmethod
    def level(risk):
        if risk < 20:
            return "low"
        if risk < 50:
            return "medium"
        if risk < 75:
            return "high"
        return "critical"

    def risk_breakdown(self):
        return sorted(
            [
                {
                    "type": inc.type,
                    "title": inc.title,
                    "contribution": round(inc.weight * inc.confidence, 1),
                    "cross_camera": inc.cross_camera,
                }
                for inc in self.active.values()
            ],
            key=lambda x: -x["contribution"],
        )

    # -- output -------------------------------------------------------------
    def state(self):
        return {
            "type": "state",
            "session_id": self.session_id,
            "risk": round(self.risk, 1),
            "risk_level": self.level(self.risk),
            "max_risk": round(self.max_risk, 1),
            "cameras_online": {c: self.has_camera(c) for c in ("candidate", "mobile")},
            "active_incidents": [i.to_dict(self.session_id) for i in
                                 sorted(self.active.values(), key=lambda x: -x.weight * x.confidence)],
            "risk_breakdown": self.risk_breakdown(),
            "latest_signals": {c: self.latest.get(c) for c in ("candidate", "mobile")},
            "timeline": self.history[-60:],
        }

    def set_review(self, incident_id, action, note):
        for store in (list(self.active.values()), None):
            pass
        # update active
        for inc in self.active.values():
            if inc.id == incident_id:
                inc.review = {"action": action, "note": note}
        for h in self.history:
            if h["id"] == incident_id:
                h["review"] = {"action": action, "note": note}
