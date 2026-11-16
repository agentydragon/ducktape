/**
 * A server-streaming RPC of a service's generated HTTP/JSON facade, followed through the shared
 * reader (`stream_reader.ts`). The facade answers with newline-delimited JSON: one `{"result": ...}`
 * object per message, and a last `{"error": ...}` object when the RPC fails after it began.
 *
 * `onOpen` marks each accepted connection; services decide whether to replay or resume.
 */
import type { JsonValue } from "@bufbuild/protobuf";
import { z } from "zod";

import { followResponse, type StreamConnection } from "./stream_reader";

export interface WatchHandlers {
  /** A response was accepted; called before its first frame. */
  onOpen: () => void;
  /** The `result` of one message. Throwing ends the connection. */
  onFrame: (result: JsonValue) => void;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
  onError?: (reason: unknown) => void;
  /** Bounds a watch that opens but never sends its initial snapshot. */
  initialFrameTimeoutMs?: number;
  /** Bounds a quiet stream whose contract promises periodic frames. */
  idleTimeoutMs?: number;
}

const messageSchema = z.union([
  z.object({ error: z.object({ code: z.number(), message: z.string() }) }),
  z.object({ result: z.json() }),
]);

export class WatchRpcError extends Error {
  constructor(
    readonly code: number,
    message: string
  ) {
    super(`${message} (code ${code})`);
  }
}

/** Splits text into lines and each into its message's `result`, erroring on an `error` message. */
function results(): TransformStream<string, JsonValue> {
  let partial = "";
  return new TransformStream({
    transform(chunk, controller) {
      const lines = (partial + chunk).split("\n");
      partial = lines.pop() ?? "";
      for (const line of lines) {
        if (line === "") continue;
        const message = messageSchema.parse(JSON.parse(line));
        if ("error" in message) throw new WatchRpcError(message.error.code, message.error.message);
        controller.enqueue(message.result);
      }
    },
  });
}

/** Follows `url` until the returned function is called. */
export function followWatch(
  url: string | (() => string),
  { onOpen, onFrame, onConnection, onError, initialFrameTimeoutMs, idleTimeoutMs }: WatchHandlers
): () => void {
  return followResponse(url, {
    accept: "application/x-ndjson",
    frames: (body) => body.pipeThrough(results()),
    onOpen,
    onFrame,
    onConnection,
    onError,
    initialFrameTimeoutMs,
    idleTimeoutMs,
  });
}
