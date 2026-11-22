import { useCallback, useEffect, useLayoutEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { DispatchWorksetItem } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { NO_ISSUES, useCommandNoticeAttention, type NoticeAttention } from "./command_notice_attention";
import { watchCommandOwnership } from "./command_ownership";
import {
  EMPTY_WORKSET,
  readDispatchReceipts,
  watchDispatchWorkset,
  type ReadDispatchReceipts,
  type WorksetView,
} from "./dispatch_workset";
import { LocalCommands, type LocalCommandSnapshot } from "./local_commands";
import { useLocalIssueReceipts } from "./local_delivery_receipts";

const EMPTY_LOCAL: LocalCommandSnapshot = {
  commands: [],
  inputEchoes: [],
  error: null,
  dispatchWatchError: null,
  dispatchWatchCoverageLost: false,
};

export interface ThreadDeliveryState {
  sessionId: string;
  workset: WorksetView;
  local: LocalCommandSnapshot;
  store: LocalCommands;
  attention: NoticeAttention;
  readReceipts: ReadDispatchReceipts;
  registerArchiveIssues: (issues: readonly DispatchWorksetItem[]) => () => void;
}

export function useDispatchWorkset(sessionId: string | null): WorksetView {
  // A new connection for the same Session still needs its own first snapshot.
  const scope = useMemo(() => ({ sessionId }), [sessionId]);
  const [state, setState] = useState<{ scope: typeof scope; view: WorksetView } | null>(null);
  useEffect(() => {
    if (scope.sessionId === null) return;
    let active = true;
    const stop = watchDispatchWorkset(scope.sessionId, (view) => {
      if (active) setState({ scope, view });
    });
    return () => {
      active = false;
      stop();
    };
  }, [scope]);
  return state?.scope === scope ? state.view : EMPTY_WORKSET;
}

/** Mounted once for the selected Session, including while its History is loading. */
export function useThreadDelivery(sessionId: string, worksetEnabled: boolean): ThreadDeliveryState {
  const workset = useDispatchWorkset(worksetEnabled ? sessionId : null);
  const store = useMemo(() => new LocalCommands(sessionId), [sessionId]);
  const local = useSyncExternalStore(store.subscribe, store.getSnapshot, () => EMPTY_LOCAL);
  const readReceipts = useCallback<ReadDispatchReceipts>(
    (ids, signal) => readDispatchReceipts(sessionId, ids, signal),
    [sessionId]
  );
  const connection = workset.snapshot?.sessionId === sessionId ? workset.connection : null;
  useLayoutEffect(() => {
    if (connection && !connection.aborted) return watchCommandOwnership(store, readReceipts, connection);
  }, [connection, readReceipts, store]);
  const [archiveIssues, setArchiveIssues] = useState<readonly DispatchWorksetItem[]>(NO_ISSUES);
  const registerArchiveIssues = useCallback<ThreadDeliveryState["registerArchiveIssues"]>((issues) => {
    setArchiveIssues(issues);
    return () => setArchiveIssues((current) => (current === issues ? NO_ISSUES : current));
  }, []);
  const savedIssues = useLocalIssueReceipts(local.commands, workset, readReceipts);
  const attention = useCommandNoticeAttention(
    sessionId,
    [...(workset.snapshot?.recentIssues ?? NO_ISSUES), ...savedIssues.issues],
    workset.stale,
    archiveIssues,
    savedIssues.error
  );
  return { sessionId, workset, local, store, attention, readReceipts, registerArchiveIssues };
}
