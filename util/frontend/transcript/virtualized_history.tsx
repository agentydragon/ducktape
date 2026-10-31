import { Button, Paper, Text } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useId, useLayoutEffect, useReducer, useRef, useState } from "react";
import { flushSync } from "react-dom";

import IconArrowDown from "@tabler/icons-react/dist/esm/icons/IconArrowDown.mjs";
import { DISCLOSURE_STICKY_Z_INDEX } from "../disclosure";
import { HistoryGeometry, type RowDescriptor } from "./history_geometry";
import { HistoryViewport } from "./history_viewport";
import type { HistoryDiagnostics } from "./history_diagnostics";

export interface HistoryItem<T> extends RowDescriptor {
  readonly value: T;
  /** Stable content identities let a reading anchor survive regrouping into another row. */
  readonly anchorKeys?: readonly string[];
  readonly dataAttributes?: Record<string, string>;
  readonly presentation?: { kind: string; entityCount: number };
}

export interface VirtualizedHistoryProps<T> {
  /** Include the captured version when switching versions must reset measurements and reader intent. */
  scopeId: string;
  rows: readonly HistoryItem<T>[];
  renderRow: (value: T) => ReactNode;
  initialPosition: "start" | "end";
  followTail: boolean;
  label: string;
  showJumpToLatest: boolean;
  earlier?: { available: boolean; loading: boolean; load: () => void };
  tail?: ReactNode;
  onClick?: React.MouseEventHandler<HTMLDivElement>;
  diagnostics?: HistoryDiagnostics;
}

/** A scope change disposes measurements, observers, and queued scroll work together. */
export function VirtualizedHistory<T>(props: VirtualizedHistoryProps<T>): JSX.Element {
  return <HistoryViewportView key={props.scopeId} {...props} />;
}

function HistoryViewportView<T>({
  scopeId,
  rows,
  renderRow,
  initialPosition,
  followTail,
  label,
  showJumpToLatest,
  earlier,
  tail,
  onClick,
  diagnostics,
}: VirtualizedHistoryProps<T>): JSX.Element {
  const viewId = useId();
  const viewport = useRef<HTMLDivElement>(null);
  const contents = useRef<HTMLDivElement>(null);
  const tailContent = useRef<HTMLDivElement>(null);
  const [, redraw] = useReducer((value: number) => value + 1, 0);
  const loadOlder = useRef<() => void>(() => undefined);
  loadOlder.current = () => {
    if (earlier?.available && !earlier.loading) earlier.load();
  };
  const [geometry] = useState(() => new HistoryGeometry(scopeId));
  const [controller] = useState(
    () =>
      new HistoryViewport(
        scopeId,
        viewId,
        geometry,
        (synchronous) => {
          if (synchronous) flushSync(redraw);
          else redraw();
        },
        () => loadOlder.current(),
        initialPosition,
        followTail,
        diagnostics
      )
  );
  const descriptors = rows;
  const plan = geometry.plan(descriptors, controller.width);
  const entities = new Map(rows.flatMap((row) => (row.anchorKeys ?? []).map((key) => [key, row.key] as const)));
  const mounted = controller.range(plan, entities);
  useLayoutEffect(() => {
    controller.mount(viewport.current!, contents.current!, tailContent.current!);
    const unregister = diagnostics?.register(viewId, () => controller.snapshot());
    return () => {
      unregister?.();
      controller.dispose();
    };
  }, [controller, viewId, diagnostics]);
  useLayoutEffect(() => {
    controller.commit(
      plan,
      entities,
      new Map(rows.map((row) => [row.key, row.presentation ?? { kind: "row", entityCount: 1 }]))
    );
  });
  useEffect(() => {
    diagnostics?.mark({
      kind: "older-state",
      loading: earlier?.loading ?? false,
      available: earlier?.available ?? false,
      rowCount: rows.length,
    });
  }, [earlier?.loading, earlier?.available, rows.length, diagnostics]);
  return (
    <div
      ref={viewport}
      role="region"
      aria-label={label}
      tabIndex={0}
      style={{
        isolation: "isolate",
        overflowY: "auto",
        overflowAnchor: "none",
        scrollbarGutter: "stable",
        flex: 1,
        minHeight: 0,
      }}
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
        const target = event.target.closest<HTMLElement>("[aria-expanded], summary");
        if (target) controller.disclose(target);
      }}
      onClick={onClick}
    >
      {earlier?.loading && (
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
              {...row.dataAttributes}
              style={{
                position: "absolute",
                top: item.start,
                left: 0,
                width: "100%",
                paddingBottom: "var(--mantine-spacing-xs)",
              }}
            >
              {renderRow(row.value)}
            </div>
          );
        })}
      </div>
      <div ref={tailContent}>{tail}</div>
      {showJumpToLatest && !controller.endVisible && (
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
