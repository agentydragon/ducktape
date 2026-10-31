import { type JSX, type ReactNode, memo, useCallback } from "react";
import { VirtualizedHistory as History } from "../../../../util/frontend/virtualized_history";
import type { HistoryDiagnostics } from "../../../../util/frontend/history_diagnostics";
import { type ThreadEntity, type ThreadWindow } from "./thread_sync";
import { rowKey, type HistoryRow } from "./history_rows";
import { HistoryRowView } from "./history_row_views";
import { revealEvidenceOnTap } from "./thread_evidence";
import { threadViewRecorder, markThreadViewEvent } from "./thread_view_recorder";

const MemoizedHistoryRowView = memo(HistoryRowView);
const diagnostics: HistoryDiagnostics = {
  mark: markThreadViewEvent,
  snapshotView: (phase, viewId, reason) => threadViewRecorder.snapshotView(phase, viewId, reason),
  register: (viewId, source) =>
    threadViewRecorder.register(viewId, () => {
      const layout = source();
      if (!layout) return null;
      const { scopeId, ...geometry } = layout;
      return { ...geometry, threadId: scopeId };
    }),
};

export interface VirtualizedHistoryProps {
  threadId: string;
  rows: HistoryRow[];
  inputEchoes: ReadonlyMap<string, string>;
  onInputEchoLoaded: (commandId: string) => void;
  tail?: ReactNode;
  running: boolean;
  activeTurn: string | null;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder" | "connection">;
}

export function VirtualizedHistory({
  threadId,
  rows,
  inputEchoes,
  onInputEchoLoaded,
  tail,
  running,
  activeTurn,
  history,
}: VirtualizedHistoryProps): JSX.Element {
  const live = useCallback((entity: ThreadEntity) => running && entity.turnId === activeTurn, [running, activeTurn]);
  return (
    <History
      scopeId={threadId}
      initialPosition="end"
      followTail={true}
      label="Thread history"
      showJumpToLatest={running}
      earlier={{ available: history.olderAvailable, loading: history.loadingOlder, load: history.loadOlder }}
      tail={tail}
      onClick={revealEvidenceOnTap}
      diagnostics={diagnostics}
      rows={rows.map((row) => ({
        key: rowKey(row),
        revision:
          `${row.kind}:` +
          row.entities
            .map(
              (entity) => `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:${entity.revisionCursor}`
            )
            .join("|"),
        value: row,
        anchorKeys: row.entities.map((entity) => `${entity.entityKind}:${entity.entityId}`),
        dataAttributes: { "data-thread-anchor": row.entities[0].cursor.toString() },
        presentation: { kind: row.kind, entityCount: row.entities.length },
      }))}
      renderRow={(row) => (
        <MemoizedHistoryRowView
          threadId={threadId}
          row={row}
          live={live}
          inputEchoes={inputEchoes}
          onInputEchoLoaded={onInputEchoLoaded}
        />
      )}
    />
  );
}
