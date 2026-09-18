/*
 * MediaPipe Object Detector setup (ES module).
 *
 * MediaPipe Tasks runs on WebAssembly and is completely independent of
 * TensorFlow.js, so it never collides with face-api.js (which keeps its own
 * bundled TF). This module loads once and exposes a factory on `window` that
 * the classic detection.js script can call.
 */
import { ObjectDetector, FilesetResolver } from "/static/js/vendor/mediapipe/vision_bundle.mjs";

window.MediaPipeObjects = {
  loaded: true,
  async create() {
    const fileset = await FilesetResolver.forVisionTasks("/static/js/vendor/mediapipe/wasm");
    return await ObjectDetector.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: "/static/models/mediapipe/efficientdet_lite0.tflite" },
      scoreThreshold: 0.4,
      maxResults: 10,
      runningMode: "VIDEO",
    });
  },
};

window.dispatchEvent(new Event("mp-setup-loaded"));
