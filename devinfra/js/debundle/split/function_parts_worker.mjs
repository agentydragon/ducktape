import { parentPort, workerData } from "node:worker_threads";
import { transformOneJsChunk } from "./function_parts.mjs";

try {
  const result = transformOneJsChunk({
    ...workerData,
    stageName: workerData.mode === "normalize" ? "normalizeOneJsChunk" : "splitOneJsChunkIntoFunctionParts",
    emitParts: workerData.mode === "split",
  });
  parentPort.postMessage({
    ok: true,
    result: {
      ...result,
      jsFiles: [...result.jsFiles.entries()],
    },
  });
} catch (error) {
  parentPort.postMessage({
    error: {
      message: error.message,
      stack: error.stack,
    },
    ok: false,
  });
}
