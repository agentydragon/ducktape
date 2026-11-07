/**
 * A server-streaming RPC of a service's generated HTTP/JSON facade, followed through the shared
 * reader (`stream_reader.ts`). The facade answers with newline-delimited JSON: one `{"result": ...}`
 * object per message, and a last `{"error": ...}` object when the RPC fails after it began.
 *
 * A watch replays what it holds on every connection; `onOpen` marks where a replay begins.
 */
import type { JsonValue } from "@bufbuild/protobuf";
import { z } from "zod";

import { followResponse, type StreamConnection } from "./stream_reader";

export interface WatchHandlers {
  /** A response was accepted: the frames that follow replay the watch's state from its start. */
  onOpen: () => void;
  /** The `result` of one message. Throwing ends the connection. */
  onFrame: (result: JsonValue) => void;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
  /** Bounds a watch that opens but never sends its initial snapshot. */
  initialFrameTimeoutMs?: number;
  /** Bounds a quiet stream whose contract promises periodic frames. */
  idleTimeoutMs?: number;
}

const messageSchema = z.union([
  z.object({ error: z.object({ code: z.number(), message: z.string() }) }),
  z.object({ result: z.json() }),
]);

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
        if ("error" in message) throw new Error(`${message.error.message} (code ${message.error.code})`);
        controller.enqueue(message.result);
      }
    },
  });
}

/** Follows `url` until the returned function is called. */
export function followWatch(
  url: string,
  { onOpen, onFrame, onConnection, initialFrameTimeoutMs, idleTimeoutMs }: WatchHandlers
): () => void {
  return followResponse(url, {
    accept: "application/x-ndjson",
    frames: (body) => body.pipeThrough(results()),
    onOpen,
    onFrame,
    onConnection,
    initialFrameTimeoutMs,
    idleTimeoutMs,
  });
}
