import { ActionIcon, Button, Drawer, Group, Stack, Text } from "@mantine/core";
import { create, toJson } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import { IconHistory } from "../icons";

import { nextStored, threadObservationEntry, threadObservations } from "./client";
import { displayableError } from "../client";
import { Disclosure, DisclosureBody } from "../../../../util/frontend/disclosure";
import { JsonView } from "../../../../util/frontend/json_view";
import { EventEntrySchema } from "../../../proto/event_log_pb";
import {
  EventKind,
  ReadSessionEventsRequestSchema,
  ReadSessionObservationsRequestSchema,
  type ReadSessionEventsResponse,
  type ReadSessionObservationsRequest,
  type ReadSessionObservationsResponse,
} from "../../../history_service/api/history/history_service_pb";

const OpenDebug = createContext<((cursor?: string) => void) | null>(null);

function ObservationEntry({ threadId, cursor }: { threadId: string; cursor: string }): JSX.Element {
  const [response, setResponse] = useState<ReadSessionEventsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setResponse(null);
    setError(null);
    void threadObservationEntry(
      create(ReadSessionEventsRequestSchema, {
        sessionId: threadId,
        afterCursor: BigInt(cursor) - 1n,
        limit: 1,
      }),
      controller.signal
    ).then(
      (value) => {
        if (!controller.signal.aborted) setResponse(value);
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
  if (response === null) return <Text role="status">Loading entry…</Text>;
  const entry = response.entries[0];
  return entry === undefined ? (
    <Text role="alert" c="red">
      History Service did not return the requested observation.
    </Text>
  ) : (
    <JsonView value={toJson(EventEntrySchema, entry)} />
  );
}

function observationRequest(
  sessionId: string,
  bounds: { beforeCursor?: bigint; afterCursor?: bigint } = {}
): ReadSessionObservationsRequest {
  return create(ReadSessionObservationsRequestSchema, { sessionId, ...bounds, limit: 30 });
}

function olderCursor(page: ReadSessionObservationsResponse): string | null {
  const first = page.observations[0]?.cursor;
  return first !== undefined && first > 1n ? first.toString() : null;
}

function newerCursor(page: ReadSessionObservationsResponse): string | null {
  const last = page.observations.at(-1)?.cursor;
  return last !== undefined && nextStored(last, page.settled) <= page.lastCursor ? last.toString() : null;
}

function Observation({
  threadId,
  observation,
}: {
  threadId: string;
  observation: ReadSessionObservationsResponse["observations"][number];
}): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  return (
    <Disclosure
      dataAttributes={{ "data-debug-observation": observation.cursor.toString() }}
      open={expanded}
      onOpenChange={setExpanded}
      summary={
        <>
          Observation {observation.cursor.toString()} ·{" "}
          {EventKind[observation.kind]?.toLowerCase() ?? `kind ${observation.kind}`}
        </>
      }
    >
      <DisclosureBody>
        <ObservationEntry threadId={threadId} cursor={observation.cursor.toString()} />
      </DisclosureBody>
    </Disclosure>
  );
}

function ObservationHistory({
  threadId,
  request,
  setRequest,
}: {
  threadId: string;
  request: ReadSessionObservationsRequest;
  setRequest: (request: ReadSessionObservationsRequest) => void;
}): JSX.Element {
  const [page, setPage] = useState<ReadSessionObservationsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(null);
    void threadObservations(request, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setPage(value);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(displayableError(reason));
      }
    );
    return () => controller.abort();
  }, [threadId, request]);
  const beforeCursor = page === null ? null : olderCursor(page);
  const afterCursor = page === null ? null : newerCursor(page);
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
          <Button onClick={() => setRequest(create(ReadSessionObservationsRequestSchema, request))}>
            Retry debug page
          </Button>
        </>
      ) : page === null ? (
        <Text role="status">Loading observations…</Text>
      ) : (
        <>
          <Group>
            <Button
              disabled={beforeCursor === null}
              onClick={() => setRequest(observationRequest(request.sessionId, { beforeCursor: BigInt(beforeCursor!) }))}
            >
              Older observations
            </Button>
            <Button
              disabled={afterCursor === null}
              onClick={() => setRequest(observationRequest(request.sessionId, { afterCursor: BigInt(afterCursor!) }))}
            >
              Newer observations
            </Button>
            <Button variant="subtle" onClick={() => setRequest(observationRequest(request.sessionId))}>
              Latest observations
            </Button>
          </Group>
          {page.observations.length === 0 && <Text>No observations in this page.</Text>}
          {page.observations.map((observation) => (
            <Observation key={observation.cursor.toString()} threadId={threadId} observation={observation} />
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
  const [selection, setSelection] = useState<ReadSessionObservationsRequest | null>(null);
  const open = (cursor?: string): void => {
    // The archive query has an exclusive bound. Keep its full signed-64-bit precision.
    const next = cursor === undefined ? null : BigInt(cursor) + 1n;
    setSelection(
      next !== null && next <= 9223372036854775807n
        ? observationRequest(threadId, { beforeCursor: next })
        : observationRequest(threadId)
    );
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

/** Opens the chronological debug drawer, at `cursor` if given. Throws outside a
 * `ChronologicalDebugProvider` subtree. */
export function useOpenChronologicalDebug(): (cursor?: string) => void {
  const open = useContext(OpenDebug);
  if (open === null) throw new Error("useOpenChronologicalDebug requires ChronologicalDebugProvider");
  return open;
}

/** Sits at the right of an observation's own row (its raw-frames disclosure control, or the plain
 * text standing in for one) rather than as a separate full-row link below it. A native `title`, not
 * a Mantine `Tooltip`, matches `EvidenceToggle`'s reasoning in `thread_evidence.tsx`. */
export function ChronologicalDebugIcon({ observationCursor }: { observationCursor: string }): JSX.Element {
  const open = useOpenChronologicalDebug();
  return (
    <ActionIcon
      size="xs"
      variant="subtle"
      color="gray"
      aria-label="Inspect chronological context"
      title="Inspect chronological context"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        open(observationCursor);
      }}
    >
      <IconHistory size={14} />
    </ActionIcon>
  );
}
