import { useEffect, useMemo, useState } from "react";

import type { DispatchWorksetItem } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { MAX_DISPATCH_BATCH } from "./client";
import { isDeliveryIssue, NO_ISSUES, noticeKey } from "./command_notice_attention";
import {
  readSelectedDispatchReceipts,
  worksetItemFromReceipt,
  type ReadDispatchReceipts,
  type WorksetView,
} from "./dispatch_workset";
import type { LocalCommand } from "./local_commands";

interface LocalDeliveryEvidence {
  issues: readonly DispatchWorksetItem[];
  error: string | null;
}

const NO_EVIDENCE: LocalDeliveryEvidence = { issues: NO_ISSUES, error: null };

export function useLocalIssueReceipts(
  commands: readonly LocalCommand[],
  view: WorksetView,
  readReceipts: ReadDispatchReceipts
): LocalDeliveryEvidence {
  const snapshot = view.snapshot;
  const visible = new Set(
    (snapshot ? [...snapshot.active, ...snapshot.recentIssues, ...snapshot.recentAdmittedOrCancelled] : []).map(
      (item) => item.commandId
    )
  );
  const localIds = JSON.stringify(
    commands
      .filter(
        (command) =>
          !visible.has(command.command.commandId) &&
          !command.admission &&
          command.dispatch &&
          isDeliveryIssue(command.dispatch.state)
      )
      .map((command) => command.command.commandId)
      .sort()
  );
  // Read exact revisions for saved issues outside the workset. Visible issues already have their revision.
  const worksetIssues = JSON.stringify((snapshot?.recentIssues ?? NO_ISSUES).map(noticeKey));
  const canRead = snapshot !== null && !view.stale;
  // A changed workset revision or lost coverage invalidates earlier evidence even if the selected IDs match.
  const generation = useMemo(
    () => ({ canRead, localIds, readReceipts, coverageLost: view.coverageLost, worksetIssues }),
    [canRead, localIds, readReceipts, view.coverageLost, worksetIssues]
  );
  const [result, setResult] = useState<{ generation: typeof generation; evidence: LocalDeliveryEvidence } | null>(null);
  useEffect(() => {
    const { canRead, localIds, readReceipts } = generation;
    const ids = JSON.parse(localIds) as string[];
    if (!canRead || ids.length === 0) return;
    let request: AbortController | undefined;
    const refresh = () => {
      request?.abort();
      const controller = new AbortController();
      request = controller;
      const requests = [];
      for (let from = 0; from < ids.length; from += MAX_DISPATCH_BATCH) {
        requests.push(
          readSelectedDispatchReceipts(readReceipts, ids.slice(from, from + MAX_DISPATCH_BATCH), controller.signal)
        );
      }
      void Promise.allSettled(requests).then((reads) => {
        if (controller.signal.aborted) return;
        const failure = reads.find((read) => read.status === "rejected");
        setResult({
          generation,
          evidence: {
            issues: reads.flatMap((read) =>
              read.status === "fulfilled"
                ? read.value.filter((row) => isDeliveryIssue(row.state)).map(worksetItemFromReceipt)
                : []
            ),
            error: failure ? `Could not verify saved delivery issues. ${displayableError(failure.reason)}` : null,
          },
        });
      });
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    refresh();
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      request?.abort();
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [generation]);
  return result?.generation === generation ? result.evidence : NO_EVIDENCE;
}
