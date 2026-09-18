/*
 * MediaPipe setup (ES module).
 *
 * MediaPipe Tasks run on WebAssembly, independent of TensorFlow.js, so they
 * never collide with face-api.js. This exposes factories for the Object
 * Detector (phone/book/person) and the Hand Landmarker (hands + hand-object
 * interaction) to the classic detection.js script.
 */
import { ObjectDetector, HandLandmarker, FilesetResolver } from "/static/js/vendor/mediapipe/vision_bundle.mjs";

let _fileset = null;
async function fs() {
  if (!_fileset) _fileset = await FilesetResolver.forVisionTasks("/static/js/vendor/mediapipe/wasm");
  return _fileset;
}

window.MediaPipeObjects = {
  loaded: true,
  async create() {
    return await ObjectDetector.createFromOptions(await fs(), {
      baseOptions: { modelAssetPath: "/static/models/mediapipe/efficientdet_lite0.tflite" },
      scoreThreshold: 0.4,
      maxResults: 10,
      runningMode: "VIDEO",
    });
  },
  async createHands() {
    return await HandLandmarker.createFromOptions(await fs(), {
      baseOptions: { modelAssetPath: "/static/models/mediapipe/hand_landmarker.task" },
      numHands: 2,
      runningMode: "VIDEO",
    });
  },
};

window.dispatchEvent(new Event("mp-setup-loaded"));
