/**
 * A server-streaming RPC of a service's generated HTTP/JSON facade, followed through the shared
 * reader (`stream_reader.ts`). The facade answers with newline-delimited JSON: one `{"result": ...}`
 * object per message, and a last `{"error": ...}` object when the RPC fails after it began.
 *
 * The request decides whether a connection starts at a fresh snapshot or a resume position.
 */
import type { JsonValue } from "@bufbuild/protobuf";
import { z } from "zod";

import { followResponse, type StreamConnection } from "./stream_reader";

export interface WatchHandlers {
  /** A response was accepted; frames that follow start at its requested snapshot or resume position. */
  onOpen: () => void;
  /** The `result` of one message. Throwing ends the connection. */
  onFrame: (result: JsonValue) => void;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
  /** Called for a failed connection before it is retried. */
  onError?: (reason: unknown) => void;
  /** Bounds a watch that cannot connect or deliver its initial state. */
  initialFrameTimeoutMs?: number;
  /** Which applied frame ends the initial timeout. By default, the first frame ends it. */
  initialFrameReady?: (frame: JsonValue) => boolean;
  /** Bounds a quiet stream whose contract promises periodic frames. */
  idleTimeoutMs?: number;
}

/** A gRPC status carried in a generated HTTP watch stream's final error envelope. */
export class WatchStreamError extends Error {
  constructor(
    message: string,
    readonly code: number
  ) {
    super(`${message} (code ${code})`);
  }
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
        if ("error" in message) throw new WatchStreamError(message.error.message, message.error.code);
        controller.enqueue(message.result);
      }
    },
  });
}

/** Follows `url` until the returned function is called. */
export function followWatch(
  url: string | (() => string),
  { onOpen, onFrame, onConnection, onError, initialFrameTimeoutMs, initialFrameReady, idleTimeoutMs }: WatchHandlers
): () => void {
  return followResponse(url, {
    accept: "application/x-ndjson",
    frames: (body) => body.pipeThrough(results()),
    onOpen,
    onFrame,
    onConnection,
    onError,
    initialFrameTimeoutMs,
    initialFrameReady,
    idleTimeoutMs,
  });
}
