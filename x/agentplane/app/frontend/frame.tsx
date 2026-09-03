import { Code } from "@mantine/core";
import { toJsonString } from "@bufbuild/protobuf";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import json from "highlight.js/lib/languages/json";
import { useMemo } from "react";

import "./frame.css";
import { Direction, EventSchema, type Event } from "./protocol_pb";

// The core build plus one grammar: the package's default entry point registers every language it
// ships, which is most of a megabyte for the one we use.
hljs.registerLanguage("json", json);

/** What crossed the pipe: a harness frame is its own line, anything else is the protocol event. */
function payloadOf(event: Event): string {
  return event.observation.case === "native" ? event.observation.value.line : toJsonString(EventSchema, event);
}

/** The sequence that places the frame in the stream, and for a harness frame its direction. */
function prefixOf(event: Event): string {
  return event.observation.case === "native"
    ? `${event.sequence} ${Direction[event.observation.value.direction]}`
    : String(event.sequence);
}

// A harness that writes a plain line to its stdout still gets a frame, so highlight only what
// announces itself as JSON rather than colouring the words of a log line as if they were tokens.
function isJson(payload: string): boolean {
  return /^\s*[[{]/.test(payload);
}

/**
 * One frame of the raw stream. The payload wraps rather than scrolling sideways — a frame is read
 * where it sits, and on a phone there is no sideways to scroll — and JSON is highlighted, which is
 * what makes a wrapped blob legible as structure instead of a wall of punctuation.
 */
export function FrameView({ event }: { event: Event }): JSX.Element {
  const payload = payloadOf(event);
  // highlight.js escapes the text it wraps; the sanitizer is the belt, since this is harness output
  // and the same untrusted source markdown.tsx guards against.
  const html = useMemo(
    () =>
      isJson(payload)
        ? DOMPurify.sanitize(hljs.highlight(payload, { language: "json" }).value, {
            ALLOWED_TAGS: ["span"],
            ALLOWED_ATTR: ["class"],
          })
        : null,
    [payload]
  );
  return (
    <Code block className="agentplane-frame">
      <span className="agentplane-frame-sequence">{prefixOf(event)} </span>
      {html === null ? payload : <span dangerouslySetInnerHTML={{ __html: html }} />}
    </Code>
  );
}
