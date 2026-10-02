import { useId, useState, type JSX } from "react";
import { Box, Button, Text } from "@mantine/core";

import type { SessionEvent } from "./api";
import { EventInspector, eventKind } from "./event-inspector";

export type EventTimelineItem = {
  id: string;
  events: SessionEvent[];
};

export type EventStrip = {
  kind: "event-strip";
  id: string;
  events: SessionEvent[];
};

type EventAnchor = {
  sequence: bigint;
  lastItemIndex: number;
};

type OrderedEvent = {
  event: SessionEvent;
  sequence: bigint;
  index: number;
};

type EventDotBucket = {
  id: string;
  events: SessionEvent[];
};

type EventSelection = { kind: "all" } | { kind: "bucket"; anchorEventId: string; eventIds: string[] };

const MAX_EVENT_DOTS = 8;

function compareSequence(left: bigint, right: bigint): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function orderEvents(events: readonly SessionEvent[]): OrderedEvent[] {
  return events
    .map((event, index) => ({ event, index, sequence: BigInt(event.sequence_num) }))
    .sort((left, right) => compareSequence(left.sequence, right.sequence) || left.index - right.index);
}

function firstSequence(events: readonly SessionEvent[]): bigint | undefined {
  let first: bigint | undefined;
  for (const event of events) {
    const sequence = BigInt(event.sequence_num);
    if (first === undefined || sequence < first) first = sequence;
  }
  return first;
}

function makeStrip(events: SessionEvent[]): EventStrip {
  const firstEvent = events[0];
  if (firstEvent === undefined) throw new Error("Event strips require at least one event.");
  return {
    kind: "event-strip",
    id: `event-strip:${firstEvent.event_id || firstEvent.sequence_num}`,
    events,
  };
}

/**
 * Place every raw event once in a strip after the presentation item whose first
 * event starts that sequence interval. Inverted anchors are clamped to the
 * preceding anchor so the strips remain chronological without moving items.
 */
export function buildEventTimeline<T extends EventTimelineItem>(
  items: readonly T[],
  events: readonly SessionEvent[]
): Array<T | EventStrip> {
  const orderedEvents = orderEvents(events);
  if (orderedEvents.length === 0) return [...items];

  const anchorsBySequence = new Map<bigint, EventAnchor>();
  let previousSequence: bigint | undefined;
  items.forEach((item, index) => {
    const itemSequence = firstSequence(item.events);
    if (itemSequence === undefined) return;
    const sequence =
      previousSequence === undefined || itemSequence > previousSequence ? itemSequence : previousSequence;
    previousSequence = sequence;
    const existing = anchorsBySequence.get(sequence);
    anchorsBySequence.set(sequence, {
      sequence,
      lastItemIndex: Math.max(existing?.lastItemIndex ?? -1, index),
    });
  });
  const anchors = [...anchorsBySequence.values()];

  const eventsByAnchor = new Map<bigint | null, SessionEvent[]>();
  let anchorIndex = -1;
  for (const { event, sequence } of orderedEvents) {
    while (anchorIndex + 1 < anchors.length && anchors[anchorIndex + 1]!.sequence <= sequence) anchorIndex += 1;
    const anchor = anchorIndex === -1 ? null : anchors[anchorIndex]!.sequence;
    const bucket = eventsByAnchor.get(anchor) ?? [];
    bucket.push(event);
    eventsByAnchor.set(anchor, bucket);
  }

  const timeline: Array<T | EventStrip> = [];
  const leadingEvents = eventsByAnchor.get(null);
  if (leadingEvents !== undefined) timeline.push(makeStrip(leadingEvents));

  const anchorsByLastItem = new Map<number, EventAnchor[]>();
  for (const anchor of anchors) {
    const sameIndex = anchorsByLastItem.get(anchor.lastItemIndex) ?? [];
    sameIndex.push(anchor);
    anchorsByLastItem.set(anchor.lastItemIndex, sameIndex);
  }

  items.forEach((item, index) => {
    timeline.push(item);
    for (const anchor of anchorsByLastItem.get(index) ?? []) {
      const bucket = eventsByAnchor.get(anchor.sequence);
      if (bucket !== undefined) timeline.push(makeStrip(bucket));
    }
  });
  return timeline;
}

function eventColor(event: SessionEvent): string {
  if (event.event_type === "user") return "blue";
  if (event.event_type === "assistant") return "violet";
  if (event.event_type === "system") return "gray";
  if (event.event_type.startsWith("control_")) return "orange";
  return "teal";
}

function eventRange(events: SessionEvent[]): string {
  const first = events[0]?.sequence_num;
  const last = events.at(-1)?.sequence_num;
  if (first === undefined) return "";
  return first === last ? first : `${first}–${last}`;
}

function eventDotBuckets(events: SessionEvent[]): EventDotBucket[] {
  const count = Math.min(events.length, MAX_EVENT_DOTS);
  return Array.from({ length: count }, (_, index) => {
    const start = Math.floor((index * events.length) / count);
    const end = Math.floor(((index + 1) * events.length) / count);
    const bucket = events.slice(start, end);
    const firstEvent = bucket[0];
    if (firstEvent === undefined) throw new Error("Event dot buckets require at least one event.");
    return { id: eventIdentity(firstEvent), events: bucket };
  });
}

function eventIdentity(event: SessionEvent): string {
  return event.event_id || event.sequence_num;
}

function bucketLabel(bucket: EventDotBucket): string {
  const first = bucket.events[0];
  const last = bucket.events.at(-1);
  if (first === undefined || last === undefined) return "Show events";
  if (bucket.events.length === 1) return `Show event ${first.sequence_num}: ${eventKind(first)}`;
  return `Show events ${first.sequence_num} through ${last.sequence_num} (${bucket.events.length} events)`;
}

function bucketTitle(bucket: EventDotBucket): string {
  const kinds = [...new Set(bucket.events.map(eventKind))];
  const label = eventRange(bucket.events);
  const count = bucket.events.length;
  return `${count === 1 ? "Event" : `${count} events`} ${label}: ${kinds.join(", ")}`;
}

export function EventTimelineStrip({ events }: { events: SessionEvent[] }): JSX.Element | null {
  const [selection, setSelection] = useState<EventSelection | null>(null);
  const detailsId = useId();
  if (events.length === 0) return null;

  const buckets = eventDotBuckets(events);
  const selectedEvents =
    selection?.kind === "all"
      ? events
      : selection?.kind === "bucket"
        ? events.filter((event) => selection.eventIds.includes(eventIdentity(event)))
        : null;
  const historySequences = events.map((event) => event.sequence_num).join(" ");

  return (
    <Box
      component="section"
      data-event-strip
      data-history-sequences={historySequences}
      data-event-count={events.length}
      style={{
        minWidth: 0,
        maxWidth: "100%",
        overflow: "hidden",
        paddingBlock: 2,
      }}
    >
      <Box
        role="group"
        aria-label="Session activity events"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          minWidth: 0,
        }}
      >
        <Text size="xs" c="dimmed" style={{ flex: "0 0 auto" }}>
          Activity
        </Text>
        <Box
          role="group"
          aria-label="Event sequence"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 2,
            flex: "1 1 auto",
            minWidth: 0,
            overflowX: "auto",
            whiteSpace: "nowrap",
          }}
        >
          {buckets.map((bucket) => {
            const first = bucket.events[0];
            if (first === undefined) return null;
            const anchorEventId = eventIdentity(first);
            const active =
              selection?.kind === "bucket" &&
              bucket.events.some((event) => eventIdentity(event) === selection.anchorEventId);
            const color = eventColor(first);
            return (
              <Button
                key={bucket.id}
                type="button"
                variant="subtle"
                color="gray"
                aria-label={bucketLabel(bucket)}
                aria-expanded={active}
                aria-controls={detailsId}
                title={bucketTitle(bucket)}
                data-event-dot
                data-event-dot-count={bucket.events.length}
                data-event-dot-sequences={bucket.events.map((event) => event.sequence_num).join(" ")}
                onClick={() =>
                  setSelection(
                    active
                      ? null
                      : {
                          kind: "bucket",
                          anchorEventId,
                          eventIds: bucket.events.map(eventIdentity),
                        }
                  )
                }
                style={{
                  flex: "0 0 auto",
                  width: 20,
                  minWidth: 20,
                  height: 24,
                  minHeight: 24,
                  paddingInline: 0,
                  borderRadius: 12,
                  border: active ? "1px solid var(--mantine-color-gray-5)" : undefined,
                }}
              >
                <Box
                  component="span"
                  aria-hidden="true"
                  style={{
                    display: "block",
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    backgroundColor: `var(--mantine-color-${color}-5)`,
                  }}
                />
              </Button>
            );
          })}
        </Box>
        <Button
          type="button"
          size="compact-xs"
          variant={selection?.kind === "all" ? "light" : "subtle"}
          color="gray"
          aria-label={`Show all ${events.length} events`}
          aria-expanded={selection?.kind === "all"}
          aria-controls={detailsId}
          title={`Show all ${events.length} events`}
          data-event-strip-toggle
          onClick={() => setSelection((selected) => (selected?.kind === "all" ? null : { kind: "all" }))}
          style={{ flex: "0 0 auto", minHeight: 24 }}
        >
          All {events.length}
        </Button>
      </Box>
      <Box
        id={detailsId}
        hidden={selectedEvents === null}
        data-event-strip-details={selectedEvents === null ? undefined : ""}
        style={{ minWidth: 0 }}
      >
        {selectedEvents !== null && <EventInspector events={selectedEvents} />}
      </Box>
    </Box>
  );
}
