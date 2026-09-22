import { Button, Drawer, Group, Stack, Text } from "@mantine/core";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";

import {
  threadObservationEntry,
  threadObservations,
  displayableError,
  type ArchivedObservationEntry,
  type ObservationPage,
} from "./client";
import { JsonView } from "./json_view";

type PageRequest = { before?: string; after?: string };
const OpenDebug = createContext<((cursor?: string) => void) | null>(null);

function ObservationEntry({ threadId, cursor }: { threadId: string; cursor: string }): JSX.Element {
  const [entry, setEntry] = useState<ArchivedObservationEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setEntry(null);
    setError(null);
    void threadObservationEntry(threadId, cursor, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setEntry(value);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(displayableError(reason));
      }
    );
    return () => controller.abort();
  }, [cursor, threadId]);
  if (error)
    return (
      <Text role="alert" c="red">
        {error}
      </Text>
    );
  return entry === null ? <Text role="status">Loading entry…</Text> : <JsonView value={entry.entry} />;
}

function Observation({
  threadId,
  observation,
}: {
  threadId: string;
  observation: ObservationPage["observations"][number];
}): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  return (
    <details
      data-debug-observation={observation.cursor}
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        Observation {observation.cursor} · {observation.kind}
      </summary>
      <Text size="xs" style={{ overflowWrap: "anywhere" }}>
        Source {observation.source_id}
      </Text>
      {expanded && <ObservationEntry threadId={threadId} cursor={observation.cursor} />}
    </details>
  );
}

function ObservationHistory({
  threadId,
  request,
  setRequest,
}: {
  threadId: string;
  request: PageRequest;
  setRequest: (request: PageRequest) => void;
}): JSX.Element {
  const [page, setPage] = useState<ObservationPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(null);
    void threadObservations(threadId, request, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setPage(value);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(displayableError(reason));
      }
    );
    return () => controller.abort();
  }, [threadId, request]);
  return (
    <Stack gap="sm" aria-label="Chronological observations">
      <Text size="sm" c="dimmed">
        Original observations in recorded order. Each page replaces the previous page.
      </Text>
      {error ? (
        <>
          <Text role="alert" c="red">
            {error}
          </Text>
          <Button onClick={() => setRequest({ ...request })}>Retry debug page</Button>
        </>
      ) : page === null ? (
        <Text role="status">Loading observations…</Text>
      ) : (
        <>
          <Group>
            <Button
              disabled={page.next_before_cursor === null}
              onClick={() => setRequest({ before: page.next_before_cursor! })}
            >
              Older observations
            </Button>
            <Button
              disabled={page.next_after_cursor === null}
              onClick={() => setRequest({ after: page.next_after_cursor! })}
            >
              Newer observations
            </Button>
            <Button variant="subtle" onClick={() => setRequest({})}>
              Latest observations
            </Button>
          </Group>
          {page.observations.length === 0 && <Text>No observations in this page.</Text>}
          {page.observations.map((observation) => (
            <Observation key={observation.cursor} threadId={threadId} observation={observation} />
          ))}
        </>
      )}
    </Stack>
  );
}

export function ChronologicalDebugProvider({
  threadId,
  children,
}: {
  threadId: string;
  children: ReactNode;
}): JSX.Element {
  const [selection, setSelection] = useState<PageRequest | null>(null);
  const open = (cursor?: string): void => {
    // The archive query has an exclusive bound. Keep its full signed-64-bit precision.
    const next = cursor === undefined ? null : BigInt(cursor) + 1n;
    setSelection(next !== null && next <= 9223372036854775807n ? { before: next.toString() } : {});
  };
  return (
    <OpenDebug.Provider value={open}>
      {children}
      <Drawer
        opened={selection !== null}
        onClose={() => setSelection(null)}
        title="Chronological debug"
        position="right"
        size="lg"
      >
        {selection !== null && <ObservationHistory threadId={threadId} request={selection} setRequest={setSelection} />}
      </Drawer>
    </OpenDebug.Provider>
  );
}

export function ChronologicalDebugLink({ observationCursor }: { observationCursor?: string }): JSX.Element {
  const open = useContext(OpenDebug);
  if (open === null) throw new Error("ChronologicalDebugLink requires ChronologicalDebugProvider");
  return (
    <Button variant="subtle" size="compact-xs" onClick={() => open(observationCursor)}>
      {observationCursor === undefined ? "Debug history" : "Inspect chronological context"}
    </Button>
  );
}
