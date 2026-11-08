import { Button, Group, Stack, Text } from "@mantine/core";
import { create, toJson } from "@bufbuild/protobuf";
import { type InfiniteData, infiniteQueryOptions, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { EventEntrySchema } from "../../../proto/event_log_pb";
import type { CSSProperties, JSX, MouseEvent } from "react";

import { displayableError } from "../client";
import { threadEvidence, threadNativeFrames } from "./client";
import { DisclosureBody } from "../../../../util/frontend/disclosure";
import { JsonView } from "../../../../util/frontend/json_view";
import { ChronologicalDebugIcon } from "./chronological_debug";
import { InspectionToggle } from "../../../../util/frontend/transcript/inspection";
import { RetainedDisclosure, useRetainedDisclosure } from "../../../../util/frontend/transcript/retained_disclosures";
import {
  ReadEntityEvidenceRequestSchema,
  ReadNativeFramesRequestSchema,
  type ThreadEntity,
} from "../../../history_service/history_service_pb";
import { threadEntityKey, threadEntityKind } from "./thread_entity";
import "./thread_evidence.css";

function firstPage<T, P>(data: InfiniteData<T, P> | undefined): InfiniteData<T, P> | undefined {
  return data && { pages: data.pages.slice(0, 1), pageParams: data.pageParams.slice(0, 1) };
}

function EvidenceFramesPage({
  threadId,
  entity,
  observationCursor,
}: {
  threadId: string;
  entity: ThreadEntity;
  observationCursor: string;
}): JSX.Element {
  const client = useQueryClient();
  const options = infiniteQueryOptions({
    queryKey: [
      "thread-native-frames",
      threadId,
      entity.projectionEpoch,
      threadEntityKind(entity),
      entity.entityId,
      observationCursor,
    ],
    initialPageParam: "0",
    // Disclosure readers start at the first page when reopened.
    gcTime: 0,
    staleTime: 0,
    queryFn: ({ signal, pageParam }) =>
      threadNativeFrames(
        create(ReadNativeFramesRequestSchema, {
          sessionId: threadId,
          projectionEpoch: entity.projectionEpoch,
          entity: threadEntityKey(entity),
          observationCursor: BigInt(observationCursor),
          afterSequence: BigInt(pageParam),
          limit: 30,
        }),
        signal
      ),
    getNextPageParam: (page) => page.nextAfterSequence?.toString(),
  });
  const query = useInfiniteQuery(options);
  const page = query.data?.pages.at(-1);
  const afterSequence = query.data?.pageParams.at(-1) ?? "0";
  const error = query.error === null ? null : displayableError(query.error);
  const loading = query.isFetching;
  const loadFirst = (): void => {
    client.setQueryData(options.queryKey, firstPage);
    void query.refetch({ cancelRefetch: false });
  };
  return (
    <DisclosureBody>
      <Stack gap="xs">
        {error && <Text c="red">{error}</Text>}
        {page?.frames.map((frame) =>
          frame.entry !== undefined ? (
            <JsonView key={frame.sourceSequence.toString()} value={toJson(EventEntrySchema, frame.entry)} />
          ) : (
            <Text size="xs" key={frame.sourceSequence.toString()}>
              Raw frame {frame.sourceSequence.toString()} unavailable
            </Text>
          )
        )}
        {!page && error && (
          <Button loading={loading} onClick={loadFirst}>
            Retry raw frames
          </Button>
        )}
        {afterSequence !== "0" && (
          <Button variant="subtle" disabled={loading} onClick={loadFirst}>
            First frames
          </Button>
        )}
        {page?.nextAfterSequence !== undefined && (
          <Button variant="subtle" loading={loading} onClick={() => void query.fetchNextPage({ cancelRefetch: false })}>
            Load more frames
          </Button>
        )}
      </Stack>
    </DisclosureBody>
  );
}

function EvidenceFrames(props: { threadId: string; entity: ThreadEntity; observationCursor: string }): JSX.Element {
  const { entity } = props;
  const id = `${entity.projectionEpoch}:${threadEntityKind(entity)}:${entity.entityId}:frames:${props.observationCursor}`;
  return (
    <RetainedDisclosure
      id={id}
      summary={<span>Observation {props.observationCursor} raw frames</span>}
      summaryAside={
        <span className="agentplane-evidence-frames-debug">
          <ChronologicalDebugIcon observationCursor={props.observationCursor} />
        </span>
      }
    >
      <EvidenceFramesPage key={`${props.threadId}:${id}`} {...props} />
    </RetainedDisclosure>
  );
}

function EvidencePageView({ threadId, entity }: { threadId: string; entity: ThreadEntity }): JSX.Element {
  const client = useQueryClient();
  const options = infiniteQueryOptions({
    queryKey: ["thread-evidence", threadId, entity.projectionEpoch, threadEntityKind(entity), entity.entityId],
    initialPageParam: "0",
    gcTime: 0,
    staleTime: 0,
    queryFn: ({ signal, pageParam }) =>
      threadEvidence(
        create(ReadEntityEvidenceRequestSchema, {
          sessionId: threadId,
          projectionEpoch: entity.projectionEpoch,
          entity: threadEntityKey(entity),
          afterCursor: BigInt(pageParam),
          limit: 30,
        }),
        signal
      ),
    getNextPageParam: (page) => page.nextAfterCursor?.toString(),
  });
  const query = useInfiniteQuery(options);
  const page = query.data?.pages.at(-1);
  const afterCursor = query.data?.pageParams.at(-1) ?? "0";
  const error = query.error === null ? null : displayableError(query.error);
  const loading = query.isFetching;
  const loadFirst = (): void => {
    client.setQueryData(options.queryKey, firstPage);
    void query.refetch({ cancelRefetch: false });
  };
  return (
    <Stack gap="xs">
      {error && (
        <DisclosureBody>
          <Text c="red">{error}</Text>
        </DisclosureBody>
      )}
      {page?.observations.map((observation) => (
        <Stack
          gap="xs"
          key={observation.observationCursor.toString()}
          data-evidence-observation={observation.observationCursor.toString()}
        >
          {observation.hasNative ? (
            <EvidenceFrames
              key={observation.observationCursor.toString()}
              threadId={threadId}
              entity={entity}
              observationCursor={observation.observationCursor.toString()}
            />
          ) : (
            <DisclosureBody key={observation.observationCursor.toString()}>
              <Group justify="space-between" wrap="nowrap" gap="xs">
                <Text size="xs">Observation {observation.observationCursor.toString()} has no native frame</Text>
                <ChronologicalDebugIcon observationCursor={observation.observationCursor.toString()} />
              </Group>
            </DisclosureBody>
          )}
        </Stack>
      ))}
      {(page?.nextAfterCursor !== undefined || afterCursor !== "0") && (
        <DisclosureBody>
          <Stack gap="xs">
            {page?.nextAfterCursor !== undefined && (
              <Button
                variant="subtle"
                loading={loading}
                onClick={() => void query.fetchNextPage({ cancelRefetch: false })}
              >
                Load more evidence
              </Button>
            )}
            {afterCursor !== "0" && (
              <Button variant="subtle" disabled={loading} onClick={loadFirst}>
                First evidence
              </Button>
            )}
          </Stack>
        </DisclosureBody>
      )}
    </Stack>
  );
}

function evidenceDisclosure(entity: ThreadEntity): string {
  return `${entity.projectionEpoch}:${threadEntityKind(entity)}:${entity.entityId}:evidence`;
}

/** An icon, not a disclosure row, so it adds no height to its card: it sits in the card's header
 * row where there is one, and is pinned out of flow at a corner (`style`) where there is not.
 *
 * Labelled by a native `title`, not a Mantine `Tooltip`: rows move under a resting pointer while the
 * thread streams, and a Tooltip opening then positions itself with `flushSync` from a ResizeObserver
 * callback, committing the whole list's pending re-render mid-delivery (a "ResizeObserver loop"). */
export function EvidenceToggle({
  entity,
  placement = "overlay",
  style,
}: {
  entity: ThreadEntity;
  placement?: "overlay" | "inline" | "bubble";
  style?: CSSProperties;
}): JSX.Element {
  const [open, setOpen] = useRetainedDisclosure(evidenceDisclosure(entity));
  return <InspectionToggle open={open} onChange={setOpen} label="Evidence" placement={placement} style={style} />;
}

const INSPECTION_REVEALED = "data-inspection-revealed";

/** A touch screen has no hover to show an item's evidence toggle, so a tap on the item marks it
 * instead, and a tap on another item, or on nothing, moves the mark. It is one delegated handler on
 * the history and a DOM attribute, not state per item: nothing re-renders and no row resizes. The
 * mark is set on every device, but only a touch screen's stylesheet reads it. */
export function revealEvidenceOnTap(event: MouseEvent<HTMLElement>): void {
  const { target } = event;
  if (!(target instanceof Element)) return;
  // A StepLine is itself a disclosure button on touch screens. Let that tap both open the
  // tool/reasoning row and reveal its separate Evidence action; all other controls stay excluded.
  const stepSummary = target.closest(".transcript-step-details .ui-disclosure-summary");
  if (target.closest("a, button, summary, input, textarea, select") && !stepSummary) return;
  if (window.getSelection()?.isCollapsed === false) return;
  event.currentTarget.querySelector(`[${INSPECTION_REVEALED}]`)?.removeAttribute(INSPECTION_REVEALED);
  // An input's status lives beside its bubble, not inside the evidence owner. Tapping
  // anywhere in that row still selects the bubble (and reveals its completed check).
  const owner =
    target.closest(".transcript-inspection-owner") ??
    target.closest(".transcript-user-message-row")?.querySelector(".transcript-inspection-owner");
  owner?.setAttribute(INSPECTION_REVEALED, "");
}

export function EvidencePanel({ threadId, entity }: { threadId: string; entity: ThreadEntity }): JSX.Element {
  const id = evidenceDisclosure(entity);
  const [open] = useRetainedDisclosure(id);
  return open ? (
    <RetainedDisclosure
      id={id}
      summary={
        <Text size="sm" c="dimmed">
          Evidence
        </Text>
      }
    >
      <EvidencePageView key={`${threadId}:${id}`} threadId={threadId} entity={entity} />
    </RetainedDisclosure>
  ) : (
    <></>
  );
}
