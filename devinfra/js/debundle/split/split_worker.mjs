// Worker thread for splitFunctionParts. Receives one artifactChunk via
// workerData, runs splitOneChunk on it, and posts the per-chunk result back.

import { parentPort, workerData } from "node:worker_threads";
import { splitOneChunk } from "./function_parts.mjs";

try {
  const result = splitOneChunk(workerData.artifactChunk);
  parentPort.postMessage({
    ok: true,
    result: { ...result, jsFiles: [...result.jsFiles.entries()] },
  });
} catch (error) {
  parentPort.postMessage({
    ok: false,
    error: { message: error.message, stack: error.stack },
  });
}
