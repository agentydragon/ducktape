/** The app's server-sent event streams, followed through the shared reader (`stream_reader.ts`). */
import { type EventSourceMessage, EventSourceParserStream } from "eventsource-parser/stream";

import { followResponse, type StreamConnection } from "./stream_reader";

export interface StreamHandlers {
  /** By event name. A frame of any of them is what makes the stream live; others are ignored. */
  events: Record<string, (message: EventSourceMessage) => void>;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
}

/** Follows `url` until the returned function is called. */
export function followStream(url: string, { events, onConnection }: StreamHandlers): () => void {
  const handlers = new Map(Object.entries(events));
  return followResponse<() => void>(url, {
    accept: "text/event-stream",
    frames: (body) =>
      body.pipeThrough(new EventSourceParserStream()).pipeThrough(
        new TransformStream<EventSourceMessage, () => void>({
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
