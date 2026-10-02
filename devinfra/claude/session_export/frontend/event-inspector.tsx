import { useState, type JSX } from "react";
import { Box, Code, Group, NativeSelect, Stack, Text, TextInput } from "@mantine/core";

import type { SessionEvent } from "./api";

export function eventKind(event: SessionEvent): string {
  const subtype = event.payload.subtype;
  return typeof subtype === "string" ? `${event.event_type} · ${subtype}` : event.event_type;
}

function EventRow({ event }: { event: SessionEvent }): JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <Box
      component="details"
      data-raw-event
      data-sequence={event.sequence_num}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      style={{ borderBottom: "1px solid var(--mantine-color-default-border)" }}
    >
      <Box
        component="summary"
        py={4}
        fz="xs"
        style={{ cursor: "pointer" }}
        aria-label={`Raw event ${event.sequence_num}`}
      >
        <Text component="span" size="xs" ff="monospace" c="dimmed" mr="xs">
          {event.sequence_num}
        </Text>
        <Text component="span" size="xs" style={{ overflowWrap: "anywhere" }}>
          {eventKind(event)}
        </Text>
      </Box>
      {open && (
        <Code block mb="xs" data-event-json style={{ maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap" }}>
          {JSON.stringify(event, null, 2)}
        </Code>
      )}
    </Box>
  );
}

export function EventInspector({ events }: { events: SessionEvent[] }): JSX.Element {
  const [kind, setKind] = useState("");
  const [search, setSearch] = useState("");
  const needle = search.trim().toLowerCase();
  const visible = events.filter(
    (event) =>
      (kind === "" || eventKind(event) === kind) &&
      (needle === "" || JSON.stringify(event).toLowerCase().includes(needle))
  );
  return (
    <Stack gap="xs" data-event-inspector>
      <Group gap="xs" grow>
        <NativeSelect
          aria-label="Event kind"
          size="xs"
          value={kind}
          onChange={(e) => setKind(e.currentTarget.value)}
          data={[
            { value: "", label: "All event kinds" },
            ...[...new Set(events.map(eventKind))].sort().map((value) => ({ value, label: value })),
          ]}
        />
        <TextInput
          aria-label="Search event data"
          placeholder="Search event data"
          type="search"
          size="xs"
          value={search}
          onChange={(e) => setSearch(e.currentTarget.value)}
        />
      </Group>
      <Text size="xs" c="dimmed" role="status">
        {visible.length} of {events.length} loaded events
      </Text>
      <Box>
        {visible.map((event) => (
          <EventRow key={`${event.sequence_num}-${event.event_id}`} event={event} />
        ))}
        {visible.length === 0 && <Text size="sm">No loaded events match these filters.</Text>}
      </Box>
    </Stack>
  );
}
