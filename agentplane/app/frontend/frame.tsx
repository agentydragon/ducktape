import { toJsonString } from "@bufbuild/protobuf";
import type { JSX } from "react";

import "./frame.css";
import { commandLabel } from "./command_state";
import { HighlightedText } from "./json_view";
import { Direction, type Event } from "../../protocol/event_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";

function summary(event: Event): string {
  const observation = event.observation;
  switch (observation.case) {
    case "commandAdmitted": {
      const command = observation.value.command;
      return command ? `Command admitted · ${commandLabel(command)} · ${command.commandId}` : "Command admitted";
    }
    case "commandFailed":
      return `Command failed · ${observation.value.commandId} · ${observation.value.reason}`;
    case "commandNoop":
      return `Command had no effect · ${observation.value.commandId} · ${observation.value.reason}`;
    case "harnessUserMessageConfirmed":
      return `User message confirmed · ${observation.value.harnessMessageId} · originating commands: ${observation.value.originCommandIds.join(", ") || "none reported"}`;
    case "modelChanged":
      return `Model changed · ${observation.value.previousModel} → ${observation.value.model} · command ${observation.value.commandId || "not reported"}`;
    case "native":
      return `Native frame · ${Direction[observation.value.direction]}`;
    default:
      return observation.case?.replace(/[A-Z]/g, (letter) => ` ${letter.toLowerCase()}`) ?? "Event";
  }
}

/**
 * Administrative evidence, not a conversation card. Every entry has its exact generated envelope
 * available, including native frames: displaying only Native.line would hide timestamps and
 * causal references. The disclosure keeps Raw additive without making every delta a JSON wall.
 */
export function FrameView({ entry }: { entry: EventEntry }): JSX.Element {
  const { event, origin } = entry;
  if (!event || !origin) throw new Error(`Event entry ${entry.cursor} is missing its event or origin`);
  return (
    <details
      className="agentplane-frame"
      data-event-cursor={String(entry.cursor)}
      id={`agentplane-event-${entry.cursor}`}
    >
      <summary>
        <span className="agentplane-frame-sequence">Event {String(entry.cursor)} · </span>
        {summary(event)}
      </summary>
      <div className="agentplane-frame-origin">
        Source {origin.sourceId}:{String(origin.sequence)}
        {event.sourceSequences.length > 0 && (
          <> · caused by {event.sourceSequences.map((sequence) => `${origin.sourceId}:${sequence}`).join(", ")}</>
        )}
      </div>
      {event.observation.case === "native" && <HighlightedText text={event.observation.value.line} />}
      <div data-event-envelope={String(entry.cursor)}>
        <HighlightedText text={toJsonString(EventEntrySchema, entry, { prettySpaces: 2 })} />
      </div>
    </details>
  );
}
