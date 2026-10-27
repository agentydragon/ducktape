/** The app's server-sent event streams, followed through the shared reader (`stream_reader.ts`). */
import { type EventSourceMessage, EventSourceParserStream } from "eventsource-parser/stream";
import { type Dispatch, type SetStateAction, useEffect, useState } from "react";

import { followResponse, type StreamConnection } from "./stream_reader";

export interface StreamHandlers {
  /** By event name. A valid frame makes the stream live; others are ignored. */
  events: Record<string, (message: EventSourceMessage) => string | void>;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
}

/** Follows `url` until the returned function is called. */
export function followStream(url: string, { events, onConnection }: StreamHandlers): () => void {
  const handlers = new Map(Object.entries(events));
  return followResponse<() => string | void>(url, {
    accept: "text/event-stream",
    frames: (body) =>
      body.pipeThrough(new EventSourceParserStream()).pipeThrough(
        new TransformStream<EventSourceMessage, () => string | void>({
          transform(message, controller) {
            // An event without a name is a `message`, as for the browser's `EventSource`.
            const handle = handlers.get(message.event ?? "message");
            if (handle !== undefined) controller.enqueue(() => handle(message));
          },
        })
      ),
    onFrame: (deliver) => deliver(),
    onConnection,
  });
}

export interface SnapshotStream<T> {
  /** The latest snapshot, or null before the first; a local write stands until the next frame. */
  snapshot: T | null;
  setSnapshot: Dispatch<SetStateAction<T | null>>;
  connection: StreamConnection;
}

export type JsonFrame<T> = { success: true; data: T } | { success: false; error: string };

/** Follows the `snapshot` frames of `url`, while it is not null. Each frame is accepted only after
 * its caller's generated runtime decoder succeeds. */
export function useSnapshotStream<T>(url: string | null, decode: (data: string) => JsonFrame<T>): SnapshotStream<T> {
  const [snapshot, setSnapshot] = useState<T | null>(null);
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  useEffect(() => {
    if (url === null) return;
    return followStream(url, {
      events: {
        snapshot: (message) => {
          const frame = decode(message.data);
          if (!frame.success) return frame.error;
          setSnapshot(frame.data);
        },
      },
      onConnection: setConnection,
    });
  }, [decode, url]);
  return { snapshot, setSnapshot, connection };
}
