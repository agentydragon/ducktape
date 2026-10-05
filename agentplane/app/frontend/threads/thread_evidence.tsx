import { ActionIcon, Button, Group, Stack, Text } from "@mantine/core";
import IconZoomCode from "@tabler/icons-react/dist/esm/icons/IconZoomCode.mjs";
import { type CSSProperties, type JSX, type MouseEvent, useEffect, useEffectEvent, useRef, useState } from "react";

import {
  displayableError,
  threadEvidence,
  threadNativeFrames,
  type EvidencePage,
  type NativeFramePage,
} from "../client";
import { JsonView } from "../json_view";
import { ChronologicalDebugIcon } from "./chronological_debug";
import { RetainedDisclosure, useRetainedDisclosure } from "./retained_disclosures";
import type { ThreadEntity } from "./thread_sync";
import "./thread_evidence.css";

function EvidenceFramesPage({
  threadId,
  entity,
  observationCursor,
}: {
  threadId: string;
  entity: ThreadEntity;
  observationCursor: string;
}): JSX.Element {
  const [page, setPage] = useState<NativeFramePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [afterSequence, setAfterSequence] = useState("0");
  const request = useRef<AbortController | null>(null);
  const scope = {
    projectionEpoch: entity.projectionEpoch,
    entityKind: entity.entityKind,
    entityId: entity.entityId,
  };
  const loadFirstPage = useEffectEvent(() => load());
  useEffect(() => {
    loadFirstPage();
    return () => request.current?.abort();
  }, []);
  const load = (after = "0"): void => {
    if (loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    void threadNativeFrames(threadId, scope, observationCursor, after, controller.signal)
      .then(
        (value) => {
          if (!controller.signal.aborted) {
            setPage(value);
            setAfterSequence(after);
          }
        },
        (reason: unknown) => {
          if (!controller.signal.aborted) setError(displayableError(reason));
        }
      )
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  };
  return (
    <Stack gap="xs">
      {error && <Text c="red">{error}</Text>}
      {page?.frames.map((frame) =>
        frame.availability === "present" ? (
          <JsonView key={frame.source_sequence} value={frame.entry} />
        ) : (
          <Text size="xs" key={frame.source_sequence}>
            Raw frame {frame.source_sequence} unavailable
          </Text>
        )
      )}
      {!page && error && (
        <Button loading={loading} onClick={() => load()}>
          Retry raw frames
        </Button>
      )}
      {afterSequence !== "0" && (
        <Button variant="subtle" disabled={loading} onClick={() => load()}>
          First frames
        </Button>
      )}
      {page?.next_after_sequence && (
        <Button variant="subtle" loading={loading} onClick={() => load(page.next_after_sequence ?? "0")}>
          Load more frames
        </Button>
      )}
    </Stack>
  );
}

function EvidenceFrames(props: { threadId: string; entity: ThreadEntity; observationCursor: string }): JSX.Element {
  const { entity } = props;
  const id = `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:frames:${props.observationCursor}`;
  return (
    <RetainedDisclosure
      id={id}
      summary={
        // A `<span>`, not a `Group`'s default `<div>`: `<summary>` only allows phrasing content.
        <Group component="span" justify="space-between" wrap="nowrap" gap="xs">
          <span>Observation {props.observationCursor} raw frames</span>
          <ChronologicalDebugIcon observationCursor={props.observationCursor} />
        </Group>
      }
    >
      <EvidenceFramesPage key={id} {...props} />
    </RetainedDisclosure>
  );
}

function EvidencePageView({ threadId, entity }: { threadId: string; entity: ThreadEntity }): JSX.Element {
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [afterCursor, setAfterCursor] = useState("0");
  const request = useRef<AbortController | null>(null);
  const scope = {
    projectionEpoch: entity.projectionEpoch,
    entityKind: entity.entityKind,
    entityId: entity.entityId,
  };
  const loadFirstPage = useEffectEvent(() => load());
  useEffect(() => {
    loadFirstPage();
    return () => request.current?.abort();
  }, []);
  const load = (after = "0"): void => {
    if (loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    void threadEvidence(threadId, scope, after, controller.signal)
      .then(
        (value) => {
          if (!controller.signal.aborted) {
            setPage(value);
            setAfterCursor(after);
          }
        },
        (reason: unknown) => {
          if (!controller.signal.aborted) setError(displayableError(reason));
        }
      )
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  };
  return (
    <Stack gap="xs">
      {error && <Text c="red">{error}</Text>}
      {page?.observations.map((observation) => (
        <Stack gap="xs" key={observation.observation_cursor} data-evidence-observation={observation.observation_cursor}>
          {observation.has_native ? (
            <EvidenceFrames
              key={observation.observation_cursor}
              threadId={threadId}
              entity={entity}
              observationCursor={observation.observation_cursor}
            />
          ) : (
            <Group key={observation.observation_cursor} justify="space-between" wrap="nowrap" gap="xs">
              <Text size="xs">Observation {observation.observation_cursor} has no native frame</Text>
              <ChronologicalDebugIcon observationCursor={observation.observation_cursor} />
            </Group>
          )}
        </Stack>
      ))}
      {page?.next_after_cursor && (
        <Button variant="subtle" loading={loading} onClick={() => load(page.next_after_cursor ?? "0")}>
          Load more evidence
        </Button>
      )}
      {afterCursor !== "0" && (
        <Button variant="subtle" disabled={loading} onClick={() => load()}>
          First evidence
        </Button>
      )}
    </Stack>
  );
}

function evidenceDisclosure(entity: ThreadEntity): string {
  return `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:evidence`;
}

/** An icon, not a disclosure row, so it adds no height to its card: it sits in the card's header
 * row where there is one, and is pinned out of flow at a corner (`style`) where there is not.
 *
 * Labelled by a native `title`, not a Mantine `Tooltip`: rows move under a resting pointer while the
 * thread streams, and a Tooltip opening then positions itself with `flushSync` from a ResizeObserver
 * callback, committing the whole list's pending re-render mid-delivery (a "ResizeObserver loop"). */
export function EvidenceToggle({ entity, style }: { entity: ThreadEntity; style?: CSSProperties }): JSX.Element {
  const [open, setOpen] = useRetainedDisclosure(evidenceDisclosure(entity));
  return (
    <ActionIcon
      size="xs"
      variant={open ? "light" : "subtle"}
      color="gray"
      className="agentplane-evidence-toggle"
      aria-label="Evidence"
      title="Evidence"
      aria-expanded={open}
      onClick={() => setOpen(!open)}
      style={style}
    >
      <IconZoomCode size={14} />
    </ActionIcon>
  );
}

const EVIDENCE_REVEALED = "data-evidence-revealed";

/** A touch screen has no hover to show an item's evidence toggle, so a tap on the item marks it
 * instead, and a tap on another item, or on nothing, moves the mark. It is one delegated handler on
 * the history and a DOM attribute, not state per item: nothing re-renders and no row resizes. The
 * mark is set on every device, but only a touch screen's stylesheet reads it. */
export function revealEvidenceOnTap(event: MouseEvent<HTMLElement>): void {
  const { target } = event;
  if (!(target instanceof Element) || target.closest("a, button, summary, input, textarea, select")) return;
  if (window.getSelection()?.isCollapsed === false) return;
  event.currentTarget.querySelector(`[${EVIDENCE_REVEALED}]`)?.removeAttribute(EVIDENCE_REVEALED);
  target.closest(".agentplane-evidence-owner")?.setAttribute(EVIDENCE_REVEALED, "");
}

export function EvidencePanel({ threadId, entity }: { threadId: string; entity: ThreadEntity }): JSX.Element {
  const id = evidenceDisclosure(entity);
  const [open] = useRetainedDisclosure(id);
  return open ? <EvidencePageView key={id} threadId={threadId} entity={entity} /> : <></>;
}
