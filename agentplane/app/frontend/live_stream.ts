/** The app's server-sent event streams, followed through the shared reader (`stream_reader.ts`). */
import { type EventSourceMessage, EventSourceParserStream } from "eventsource-parser/stream";
import { type Dispatch, type SetStateAction, useEffect, useState } from "react";

import { followResponse, type StreamConnection } from "./stream_reader";

export interface StreamHandlers {
  /** By event name. A frame of any of them is what makes the stream live; others are ignored. */
  events: Record<string, (message: EventSourceMessage) => void>;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
}

/** Follows `url` until the returned function is called. */
export function followStream(url: string, { events, onConnection }: StreamHandlers): () => void {
  // An event without a name is a `message`, as for the browser's `EventSource`.
  const handler = (message: EventSourceMessage) => events[message.event ?? "message"];
  return followResponse(url, {
    accept: "text/event-stream",
    frames: (body) =>
      body.pipeThrough(new EventSourceParserStream()).pipeThrough(
        new TransformStream<EventSourceMessage, EventSourceMessage>({
          transform(message, controller) {
            if (handler(message) !== undefined) controller.enqueue(message);
          },
        })
      ),
    onFrame: (message) => handler(message)?.(message),
    onConnection,
  });
}

export interface SnapshotStream<T> {
  /** The latest snapshot, or null before the first; a local write stands until the next frame. */
  snapshot: T | null;
  setSnapshot: Dispatch<SetStateAction<T | null>>;
  connection: StreamConnection;
}

/** Follows the `snapshot` frames of `url`, while it is not null. */
export function useSnapshotStream<T>(url: string | null): SnapshotStream<T> {
  const [snapshot, setSnapshot] = useState<T | null>(null);
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  useEffect(() => {
    if (url === null) return;
    return followStream(url, {
      events: { snapshot: (message) => setSnapshot(JSON.parse(message.data) as T) },
      onConnection: setConnection,
    });
  }, [url]);
  return { snapshot, setSnapshot, connection };
}
