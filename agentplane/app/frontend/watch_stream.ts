/**
 * A server-streaming RPC of a service's generated HTTP/JSON facade, followed through the shared
 * reader (`stream_reader.ts`). The facade answers with newline-delimited JSON: one `{"result": ...}`
 * object per message, and a last `{"error": ...}` object when the RPC fails after it began.
 *
 * A watch replays what it holds on every connection; `onOpen` marks where a replay begins.
 */
import { followResponse, type StreamConnection } from "./stream_reader";

export interface WatchHandlers {
  /** A response was accepted: the frames that follow replay the watch's state from its start. */
  onOpen: () => void;
  /** The `result` of one message. Throwing ends the connection. */
  onFrame: (result: unknown) => void;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
}

interface Message {
  result?: unknown;
  error?: { code: number; message: string };
}

/** Splits text into lines and each into its message's `result`, erroring on an `error` message. */
function results(): TransformStream<string, unknown> {
  let partial = "";
  return new TransformStream({
    transform(chunk, controller) {
      const lines = (partial + chunk).split("\n");
      partial = lines.pop() ?? "";
      for (const line of lines) {
        if (line === "") continue;
        const message = JSON.parse(line) as Message;
        if (message.error !== undefined) throw new Error(`${message.error.message} (code ${message.error.code})`);
        controller.enqueue(message.result);
      }
    },
  });
}

/** Follows `url` until the returned function is called. */
export function followWatch(url: string, { onOpen, onFrame, onConnection }: WatchHandlers): () => void {
  return followResponse(url, {
    accept: "application/x-ndjson",
    frames: (body) => body.pipeThrough(results()),
    onOpen,
    onFrame,
    onConnection,
  });
}
