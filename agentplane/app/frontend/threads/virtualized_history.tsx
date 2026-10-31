import { Button, Paper, Text } from "@mantine/core";
import {
  type JSX,
  type ReactNode,
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";

import { IconArrowDown } from "../icons";
import { DISCLOSURE_STICKY_Z_INDEX } from "../disclosure";
import { type ThreadEntity, type ThreadWindow } from "./thread_sync";
import { rowKey, type HistoryRow } from "./history_rows";
import { HistoryRowView } from "./history_row_views";
import { HistoryGeometry } from "./history_geometry";
import { HistoryViewport } from "./history_viewport";
import { revealEvidenceOnTap } from "./thread_evidence";
import { threadViewRecorder, markThreadViewEvent } from "./thread_view_recorder";

const MemoizedHistoryRowView = memo(HistoryRowView);

export interface VirtualizedHistoryProps {
  threadId: string;
  rows: HistoryRow[];
  inputEchoes: ReadonlyMap<string, string>;
  onInputEchoLoaded: (commandId: string) => void;
  /** Local input without an admission cursor yet: shown provisionally after ordered history. */
  tail?: ReactNode;
  running: boolean;
  activeTurn: string | null;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder" | "connection">;
}

/** The key gives all measurements, observers and queued work the lifetime of this mounted Thread. */
export function VirtualizedHistory(props: VirtualizedHistoryProps): JSX.Element {
  return <HistoryViewportView key={props.threadId} {...props} />;
}

function HistoryViewportView({
  threadId,
  rows,
  inputEchoes,
  onInputEchoLoaded,
  tail,
  running,
  activeTurn,
  history,
}: VirtualizedHistoryProps): JSX.Element {
  const viewId = useId();
  const viewport = useRef<HTMLDivElement>(null);
  const contents = useRef<HTMLDivElement>(null);
  const tailContent = useRef<HTMLDivElement>(null);
  const [, redraw] = useReducer((value: number) => value + 1, 0);
  const loadOlder = useRef(history.loadOlder);
  loadOlder.current = () => {
    if (history.olderAvailable && !history.loadingOlder) history.loadOlder();
  };
  const [geometry] = useState(() => new HistoryGeometry(threadId));
  const [controller] = useState(
    () =>
      new HistoryViewport(
        threadId,
        viewId,
        geometry,
        (synchronous) => {
          if (synchronous) flushSync(redraw);
          else redraw();
        },
        () => loadOlder.current()
      )
  );
  const descriptors = rows.map((row) => ({
    key: rowKey(row),
    revision:
      `${row.kind}:` +
      row.entities
        .map((entity) => `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:${entity.revisionCursor}`)
        .join("|"),
  }));
  const plan = geometry.plan(descriptors, controller.width);
  const entities = new Map(
    rows.flatMap((row) =>
      row.entities.map((entity) => [`${entity.entityKind}:${entity.entityId}`, rowKey(row)] as const)
    )
  );
  const mounted = controller.range(plan, entities);
  const live = useCallback((entity: ThreadEntity) => running && entity.turnId === activeTurn, [running, activeTurn]);
  useLayoutEffect(() => {
    controller.mount(viewport.current!, contents.current!, tailContent.current!);
    const unregister = threadViewRecorder.register(viewId, () => controller.snapshot());
    return () => {
      unregister();
      controller.dispose();
    };
  }, [controller, viewId]);
  useLayoutEffect(() => {
    controller.commit(
      plan,
      entities,
      new Map(rows.map((row) => [rowKey(row), { kind: row.kind, entityCount: row.entities.length }]))
    );
  });
  useEffect(() => {
    markThreadViewEvent({
      kind: "older-state",
      loading: history.loadingOlder,
      available: history.olderAvailable,
      rowCount: rows.length,
    });
  }, [history.loadingOlder, history.olderAvailable, rows.length]);
  return (
    <div
      ref={viewport}
      role="region"
      aria-label="Thread history"
      tabIndex={0}
      style={{ overflowY: "auto", overflowAnchor: "none", scrollbarGutter: "stable", flex: 1, minHeight: 0 }}
      onWheel={(event) =>
        controller.input(
          "wheel",
          event.deltaY < 0 ? "up" : "down",
          event.deltaY < 0
            ? event.currentTarget.scrollTop > 0
            : event.currentTarget.scrollTop < event.currentTarget.scrollHeight - event.currentTarget.clientHeight
        )
      }
      onKeyDown={(event) => controller.key(event.key)}
      onPointerDown={() => controller.pointerDown()}
      onPointerUp={() => controller.pointerUp()}
      onPointerCancel={() => controller.pointerUp()}
      onTouchStart={(event) => {
        if (event.touches[0]) controller.touchStart(event.touches[0].clientY);
      }}
      onTouchMove={(event) => {
        if (event.touches[0]) controller.touchMove(event.touches[0].clientY);
      }}
      onTouchEnd={() => controller.touchEnd()}
      onScroll={() => controller.scrolled()}
      onClickCapture={(event) => {
        if (!(event.target instanceof Element)) return;
        const target = event.target.closest<HTMLElement>("[aria-expanded]");
        if (target) controller.disclose(target);
      }}
      onClick={revealEvidenceOnTap}
    >
      {history.loadingOlder && (
        <div
          style={{
            position: "sticky",
            top: 0,
            height: 0,
            zIndex: 1,
            display: "flex",
            justifyContent: "center",
            alignItems: "flex-start",
            pointerEvents: "none",
          }}
        >
          <Paper role="status" shadow="xs" radius="xl" px="sm" py={2} mt="xs" withBorder>
            <Text size="xs" c="dimmed">
              Loading earlier…
            </Text>
          </Paper>
        </div>
      )}
      <div ref={contents} style={{ height: plan.total, position: "relative" }}>
        {mounted.map((item) => {
          const row = rows[item.index];
          return (
            <div
              key={item.key}
              data-history-row-key={item.key}
              data-index={item.index}
              data-thread-anchor={row.entities[0].cursor.toString()}
              style={{
                position: "absolute",
                top: item.start,
                left: 0,
                width: "100%",
                paddingBottom: "var(--mantine-spacing-xs)",
              }}
            >
              <MemoizedHistoryRowView
                threadId={threadId}
                row={row}
                live={live}
                inputEchoes={inputEchoes}
                onInputEchoLoaded={onInputEchoLoaded}
              />
            </div>
          );
        })}
      </div>
      <div ref={tailContent}>{tail}</div>
      {running && !controller.endVisible && (
        <div
          style={{
            position: "sticky",
            bottom: 0,
            height: 0,
            zIndex: DISCLOSURE_STICKY_Z_INDEX + 1,
            display: "flex",
            justifyContent: "center",
            alignItems: "flex-end",
            pointerEvents: "none",
          }}
        >
          <Button
            size="compact-xs"
            variant="default"
            radius="xl"
            mb="xs"
            leftSection={<IconArrowDown size={14} />}
            onClick={() => controller.follow()}
            style={{ pointerEvents: "auto" }}
          >
            Jump to latest
          </Button>
        </div>
      )}
    </div>
  );
}
