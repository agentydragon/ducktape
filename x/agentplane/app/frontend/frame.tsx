import { Code } from "@mantine/core";
import { toJsonString } from "@bufbuild/protobuf";
import { type JSX, useMemo } from "react";

import "./frame.css";
import { highlightJson, looksLikeJson } from "./json_view";
import { Direction, type Event } from "./x/agentplane/protocol/event_pb";
import { EventEntrySchema, type EventEntry, type EventOrigin } from "./x/agentplane/protocol/event_log_pb";

/** EventEntry's required-by-contract payloads are optional in generated TypeScript. */
function eventOf(entry: EventEntry): Event {
  if (entry.event === undefined) throw new Error(`event entry ${entry.cursor} has no event`);
  return entry.event;
}

function originOf(entry: EventEntry): EventOrigin {
  if (entry.origin === undefined) throw new Error(`event entry ${entry.cursor} has no origin`);
  return entry.origin;
}

/** What crossed the pipe: a harness frame is its own line, anything else is the protocol event. */
function payloadOf(entry: EventEntry): string {
  const event = eventOf(entry);
  return event.observation.case === "native" ? event.observation.value.line : toJsonString(EventEntrySchema, entry);
}

/** The source cursor/origin that places this in the stream, and for a harness frame its direction. */
function prefixOf(entry: EventEntry): string {
  const event = eventOf(entry);
  const origin = originOf(entry);
  const identity = `${entry.cursor} ${origin.sourceId}:${origin.sequence}`;
  return event.observation.case === "native" ? `${identity} ${Direction[event.observation.value.direction]}` : identity;
}

/**
 * One frame of the raw stream. The payload wraps rather than scrolling sideways — a frame is read
 * where it sits, and on a phone there is no sideways to scroll — and JSON is highlighted, which is
 * what makes a wrapped blob legible as structure instead of a wall of punctuation.
 */
export function FrameView({ entry }: { entry: EventEntry }): JSX.Element {
  const payload = payloadOf(entry);
  // A harness that writes a plain line to its stdout still gets a frame, so highlight only what
  // announces itself as JSON rather than colouring the words of a log line as if they were tokens.
  const html = useMemo(() => (looksLikeJson(payload) ? highlightJson(payload) : null), [payload]);
  return (
    <Code block className="agentplane-hljs agentplane-frame">
      <span className="agentplane-frame-sequence">{prefixOf(entry)} </span>
      {html === null ? payload : <span dangerouslySetInnerHTML={{ __html: html }} />}
    </Code>
  );
}
