/*
 * detection.js  --  shared client-side AI pipeline.
 *
 * Runs entirely in the browser (GPU accelerated via WebGL):
 *   - face-api.js  : face detection, 68-pt landmarks, 128-d identity descriptor
 *   - MediaPipe    : object detection (phone, book, extra person, devices)
 *
 * It turns raw detections into a compact "signals" object and streams it,
 * plus a downscaled video frame, to the server over a WebSocket. The heavy
 * temporal reasoning happens on the server (risk_engine.py); this file only
 * reports what it currently sees.
 */

const MODEL_URL = "/static/models";
const OBJECT_CLASSES = ["cell phone", "book", "laptop", "tv", "remote", "keyboard", "mouse", "person"];

let faceOpts = null;
let objModel = null;          // MediaPipe ObjectDetector (WASM, independent of tfjs)
let handModel = null;         // MediaPipe HandLandmarker (hands + hand-object interaction)
let modelsLoaded = false;

const INTERACT_CLASSES = ["cell phone", "book", "remote"];  // objects that matter if a hand is on them

function rectsOverlap(a, b) {
  const ix = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
  const iy = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  return ix * iy;
}

function waitFor(cond, timeout = 20000, label = "dependency") {
  return new Promise((resolve, reject) => {
    const t0 = performance.now();
    (function poll() {
      if (cond()) return resolve();
      if (performance.now() - t0 > timeout) return reject(new Error("Timed out waiting for " + label));
      setTimeout(poll, 50);
    })();
  });
}

async function step(name, fn) {
  try {
    return await fn();
  } catch (e) {
    console.error("[detection] failed at:", name, e);
    throw new Error(name + " failed: " + (e && e.message ? e.message : e));
  }
}

async function loadModels(onStatus) {
  if (typeof faceapi === "undefined") throw new Error("face-api.js did not load");

  onStatus && onStatus("Loading face models…");
  await step("load tinyFaceDetector", () => faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL));
  await step("load faceLandmark68Net", () => faceapi.nets.faceLandmark68Net.loadFromUri(MODEL_URL));
  await step("load faceRecognitionNet", () => faceapi.nets.faceRecognitionNet.loadFromUri(MODEL_URL));
  faceOpts = new faceapi.TinyFaceDetectorOptions({ inputSize: 320, scoreThreshold: 0.45 });

  onStatus && onStatus("Loading object model…");
  await step("wait for MediaPipe module", () => waitFor(() => window.MediaPipeObjects, 20000, "MediaPipe module"));
  objModel = await step("create MediaPipe object detector", () => window.MediaPipeObjects.create());

  onStatus && onStatus("Loading hand model…");
  try {
    handModel = await window.MediaPipeObjects.createHands();
  } catch (e) {
    console.warn("[detection] hand model unavailable, continuing without it:", e);
    handModel = null;
  }

  modelsLoaded = true;
  onStatus && onStatus("Models ready");
}

/* ---- head pose / gaze from 68 landmarks (explainable heuristic) ---- */
function meanPoint(pts) {
  const s = pts.reduce((a, p) => ({ x: a.x + p.x, y: a.y + p.y }), { x: 0, y: 0 });
  return { x: s.x / pts.length, y: s.y / pts.length };
}

/* Eye Aspect Ratio: high when the eye is open, drops toward 0 when closed.
   Uses the 6 landmark points face-api returns per eye. */
function eyeAspectRatio(eye) {
  const d = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const vertical = d(eye[1], eye[5]) + d(eye[2], eye[4]);
  const horizontal = 2 * d(eye[0], eye[3]);
  return horizontal === 0 ? 0 : vertical / horizontal;
}

function eyeInfo(landmarks) {
  const le = landmarks.getLeftEye();
  const re = landmarks.getRightEye();
  const ear = (eyeAspectRatio(le) + eyeAspectRatio(re)) / 2;
  return { left: le, right: re, ear, closed: ear < 0.18 };
}

function headPose(landmarks, box) {
  const le = meanPoint(landmarks.getLeftEye());
  const re = meanPoint(landmarks.getRightEye());
  const noseArr = landmarks.getNose();
  const noseTip = noseArr[6] || meanPoint(noseArr);
  const mouth = meanPoint(landmarks.getMouth());

  const eyeMidX = (le.x + re.x) / 2;
  const eyeMidY = (le.y + re.y) / 2;

  const yaw = (noseTip.x - eyeMidX) / box.width;          // + right, - left
  const faceMidY = (eyeMidY + mouth.y) / 2;
  const span = Math.max(1, mouth.y - eyeMidY);
  const pitch = (noseTip.y - faceMidY) / span;            // + down, - up

  const YAW_T = 0.14, PITCH_DOWN = 0.35, PITCH_UP = -0.35;
  let direction = "center";
  let away = false;
  if (Math.abs(yaw) > YAW_T && Math.abs(yaw) >= Math.abs(pitch) * 0.5) {
    direction = yaw > 0 ? "right" : "left";
    away = true;
  } else if (pitch > PITCH_DOWN) {
    direction = "down"; away = true;
  } else if (pitch < PITCH_UP) {
    direction = "up"; away = true;
  }
  return { yaw, pitch, direction, away };
}

/* ---- the client ---- */
class ProctorClient {
  constructor(opts) {
    this.role = opts.role;                 // "candidate" | "mobile"
    this.sessionId = opts.sessionId;
    this.video = opts.video;
    this.overlay = opts.overlay;           // canvas for drawing boxes (optional)
    this.onSignals = opts.onSignals || (() => {});
    this.onServer = opts.onServer || (() => {});
    this.onStatus = opts.onStatus || (() => {});

    this.enrolledDescriptor = opts.enrolledDescriptor || null;
    this.token = opts.token || "";
    this.facingMode = opts.facingMode || (this.role === "mobile" ? "environment" : "user");
    this.stream = null;
    this.lastObjects = [];
    this.lastHands = [];
    this.ws = null;
    this.running = false;
    this._objTick = 0;
    this._lastObjTs = 0;
    this._lastHandTs = 0;
  }

  async start() {
    if (!modelsLoaded) await loadModels(this.onStatus);
    await this._startCamera();
    this._connect();
    this.running = true;
    this._detectLoop();
    this._frameLoop();
  }

  async _startCamera() {
    this.onStatus("Requesting camera…");
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: this.facingMode },
      audio: false,
    });
    this.stream = stream;
    this.video.srcObject = stream;
    await new Promise((res) => (this.video.onloadedmetadata = res));
    await this.video.play();
    this.onStatus("Camera on (" + (this.facingMode === "environment" ? "rear" : "front") + ")");
  }

  /* toggle between front (user) and rear (environment) cameras */
  async flipCamera() {
    this.facingMode = this.facingMode === "environment" ? "user" : "environment";
    try {
      await this._startCamera();
    } catch (e) {
      // fall back to any available camera if the requested facing mode is missing
      this.onStatus("Flip failed, reverting: " + e.message);
      this.facingMode = this.facingMode === "environment" ? "user" : "environment";
      await this._startCamera();
    }
    return this.facingMode;
  }

  _connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = this.token ? `?token=${encodeURIComponent(this.token)}` : "";
    this.ws = new WebSocket(`${proto}://${location.host}/ws/${this.role}/${this.sessionId}${q}`);
    this.ws.onmessage = (e) => this.onServer(JSON.parse(e.data));
    this.ws.onclose = () => {
      if (this.running) setTimeout(() => this._connect(), 1500);
    };
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  /* capture the current face descriptor for enrollment */
  async captureDescriptor() {
    const res = await faceapi
      .detectSingleFace(this.video, faceOpts)
      .withFaceLandmarks()
      .withFaceDescriptor();
    if (!res) return null;
    this.enrolledDescriptor = Array.from(res.descriptor);
    return this.enrolledDescriptor;
  }

  setEnrolled(descriptor) {
    this.enrolledDescriptor = descriptor;
  }

  async _detectLoop() {
    while (this.running) {
      const t0 = performance.now();
      try {
        await this._detectOnce();
      } catch (e) {
        /* keep looping */
      }
      const dt = performance.now() - t0;
      await new Promise((r) => setTimeout(r, Math.max(50, 600 - dt)));
    }
  }

  async _detectOnce() {
    const vw = this.video.videoWidth || 640;
    const vh = this.video.videoHeight || 480;

    // faces + landmarks + descriptors
    const faces = await faceapi
      .detectAllFaces(this.video, faceOpts)
      .withFaceLandmarks()
      .withFaceDescriptors();

    // objects + hands every other tick (cheaper) -- MediaPipe (WASM)
    this._objTick = (this._objTick + 1) % 2;
    if (this._objTick === 0 && objModel) {
      let ts = performance.now();
      if (ts <= this._lastObjTs) ts = this._lastObjTs + 1;   // MediaPipe needs increasing timestamps
      this._lastObjTs = ts;
      const res = objModel.detectForVideo(this.video, ts);
      this.lastObjects = (res.detections || [])
        .map((d) => {
          const c = d.categories[0] || {};
          const bb = d.boundingBox || {};
          return { class: c.categoryName, score: +(c.score || 0).toFixed(2),
                   bbox: [bb.originX, bb.originY, bb.width, bb.height] };
        })
        .filter((o) => OBJECT_CLASSES.includes(o.class) && o.score > 0.4);

      if (handModel) {
        let hts = performance.now();
        if (hts <= this._lastHandTs) hts = this._lastHandTs + 1;
        this._lastHandTs = hts;
        const hres = handModel.detectForVideo(this.video, hts);
        this.lastHands = (hres.landmarks || []).map((lm) => {
          let minx = 1, miny = 1, maxx = 0, maxy = 0;
          lm.forEach((p) => { minx = Math.min(minx, p.x); miny = Math.min(miny, p.y); maxx = Math.max(maxx, p.x); maxy = Math.max(maxy, p.y); });
          return { x: minx * vw, y: miny * vh, w: (maxx - minx) * vw, h: (maxy - miny) * vh };
        });
      }
    }

    // hand - object interaction: is a hand overlapping a phone/book?
    let handOnObject = false, handObjectClass = null;
    for (const o of this.lastObjects) {
      if (!INTERACT_CLASSES.includes(o.class)) continue;
      const ob = { x: o.bbox[0], y: o.bbox[1], w: o.bbox[2], h: o.bbox[3] };
      for (const h of this.lastHands) {
        if (rectsOverlap(h, ob) > 0.02 * (ob.w * ob.h)) { handOnObject = true; handObjectClass = o.class; break; }
      }
      if (handOnObject) break;
    }

    let signals = {
      face_present: faces.length > 0,
      face_count: faces.length,
      identity_match: null,
      identity_distance: null,
      head_yaw: 0,
      head_pitch: 0,
      looking_away: false,
      gaze_direction: "center",
      eyes_detected: false,
      eyes_closed: false,
      eye_openness: null,
      hands_count: this.lastHands.length,
      hand_on_object: handOnObject,
      hand_object_class: handObjectClass,
      objects: this.lastObjects.map((o) => ({ class: o.class, score: o.score })),
      person_count: this.lastObjects.filter((o) => o.class === "person").length,
      face_area: 0,
    };

    if (faces.length > 0) {
      // use the largest face as the candidate
      faces.sort((a, b) => b.detection.box.area - a.detection.box.area);
      const f = faces[0];
      const box = f.detection.box;
      signals.face_area = +(box.area / (vw * vh)).toFixed(3);
      const pose = headPose(f.landmarks, box);
      signals.head_yaw = +pose.yaw.toFixed(3);
      signals.head_pitch = +pose.pitch.toFixed(3);
      signals.looking_away = pose.away;
      signals.gaze_direction = pose.direction;

      const eyes = eyeInfo(f.landmarks);
      signals.eyes_detected = true;
      signals.eye_openness = +eyes.ear.toFixed(2);
      signals.eyes_closed = eyes.closed;

      if (this.enrolledDescriptor) {
        const d = faceapi.euclideanDistance(f.descriptor, this.enrolledDescriptor);
        signals.identity_distance = +d.toFixed(3);
        signals.identity_match = d < 0.55;
      }
    }

    this._send({ type: "signal", role: this.role, signals });
    this.onSignals(signals);
    this._draw(faces, vw, vh);
  }

  _draw(faces, vw, vh) {
    if (!this.overlay) return;
    const cx = this.overlay;
    cx.width = vw; cx.height = vh;
    const ctx = cx.getContext("2d");
    ctx.clearRect(0, 0, vw, vh);
    ctx.lineWidth = 3;
    ctx.font = "16px system-ui";
    faces.forEach((f, i) => {
      const b = f.detection.box;
      let color = "#2ecc71";
      if (faces.length > 1) color = "#e74c3c";
      else if (this.enrolledDescriptor) {
        const d = faceapi.euclideanDistance(f.descriptor, this.enrolledDescriptor);
        color = d < 0.55 ? "#2ecc71" : "#e74c3c";
      }
      ctx.strokeStyle = color;
      ctx.strokeRect(b.x, b.y, b.width, b.height);
      ctx.fillStyle = color;
      ctx.fillText(i === 0 ? "face" : "extra face", b.x, b.y - 6);

      // draw detected eyes
      if (f.landmarks) {
        const eyes = eyeInfo(f.landmarks);
        const eyeColor = eyes.closed ? "#e74c3c" : "#4c8dff";
        ctx.strokeStyle = eyeColor;
        ctx.lineWidth = 2;
        [eyes.left, eyes.right].forEach((eye) => {
          ctx.beginPath();
          eye.forEach((p, j) => (j ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
          ctx.closePath();
          ctx.stroke();
        });
        ctx.fillStyle = eyeColor;
        ctx.fillText(eyes.closed ? "eyes closed" : "eyes open", eyes.left[0].x, eyes.left[0].y - 8);
        ctx.lineWidth = 3;
      }
    });
    this.lastObjects.forEach((o) => {
      if (o.class === "person") return;
      const [x, y, w, h] = o.bbox;
      ctx.strokeStyle = "#f39c12";
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = "#f39c12";
      ctx.fillText(`${o.class} ${o.score}`, x, y - 6);
    });
    // draw detected hands
    ctx.lineWidth = 2;
    this.lastHands.forEach((h) => {
      ctx.strokeStyle = "#9b59b6";
      ctx.strokeRect(h.x, h.y, h.w, h.h);
      ctx.fillStyle = "#9b59b6";
      ctx.fillText("hand", h.x, h.y - 6);
    });
    ctx.lineWidth = 3;
  }

  async _frameLoop() {
    const c = document.createElement("canvas");
    while (this.running) {
      try {
        const vw = this.video.videoWidth || 640;
        const vh = this.video.videoHeight || 480;
        const scale = 360 / vh;
        c.width = vw * scale; c.height = 360;
        c.getContext("2d").drawImage(this.video, 0, 0, c.width, c.height);
        const data = c.toDataURL("image/jpeg", 0.5);
        this._send({ type: "frame", role: this.role, image: data });
      } catch (e) {}
      await new Promise((r) => setTimeout(r, 350));
    }
  }

  enroll(candidateName) {
    this._send({ type: "enroll", candidate: candidateName });
  }

  review(incidentId, action, note) {
    this._send({ type: "review", incident_id: incidentId, action, note });
  }

  end() {
    this._send({ type: "end" });
    this.running = false;
    if (this.ws) this.ws.close();
  }
}

window.ProctorClient = ProctorClient;
