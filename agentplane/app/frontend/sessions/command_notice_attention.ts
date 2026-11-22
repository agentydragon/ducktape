import { create } from "@bufbuild/protobuf";
import { useEffect, useRef, useState } from "react";

import { CommandNoticeRevisionSchema, type CommandNoticeRevision } from "../../app_service_pb";
import {
  DeliveryState,
  type DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { acknowledgeCommandNotice, getCommandNoticeAcknowledgements } from "./command_notice_client";
import type { LocalCommand } from "./local_commands";

type Receipt = { commandId: string; receiptVersion?: bigint };

export function noticeKey(receipt: Receipt): string {
  return JSON.stringify([receipt.commandId, receipt.receiptVersion?.toString()]);
}

export function isDeliveryIssue(state: DeliveryState): boolean {
  return state === DeliveryState.FAILED || state === DeliveryState.UNKNOWN;
}

export function matchingLocalIssue(
  command: LocalCommand,
  sessionId: string,
  issues: readonly DispatchWorksetItem[]
): DispatchWorksetItem | undefined {
  const receipt = command.dispatch;
  if (command.admission || receipt === null || receipt.updatedAt === undefined || receipt.sessionId !== sessionId)
    return;
  // Compare the local display to the latest visible server notice. A different update, outcome,
  // or diagnostic keeps the local row visible. The revision sent to App always comes from Dispatcher.
  return issues.find(
    (issue) =>
      issue.commandId === receipt.commandId &&
      issue.state === receipt.state &&
      issue.operation === receipt.operation &&
      issue.updatedAt?.seconds === receipt.updatedAt?.seconds &&
      issue.updatedAt?.nanos === receipt.updatedAt?.nanos &&
      (issue.reasonTruncated
        ? issue.reason !== undefined && receipt.reason?.startsWith(issue.reason)
        : receipt.reason === issue.reason)
  );
}

export interface NoticeAttention {
  issues: readonly DispatchWorksetItem[];
  acknowledged: (item: Receipt) => boolean;
  canAcknowledge: (item: DispatchWorksetItem) => boolean;
  acknowledge: (item: DispatchWorksetItem) => void;
  historyRevision: number;
  pending: string | null;
  error: string | null;
}

export const NO_ISSUES: readonly DispatchWorksetItem[] = [];

export function useCommandNoticeAttention(
  sessionId: string,
  issues: readonly DispatchWorksetItem[],
  stale: boolean,
  archiveIssues: readonly DispatchWorksetItem[],
  selectedError: string | null
): NoticeAttention {
  const eligible = new Set(
    [...(stale ? NO_ISSUES : issues), ...archiveIssues]
      .filter((item) => isDeliveryIssue(item.state) && (item.receiptVersion ?? 0n) > 0n)
      .map(noticeKey)
  );
  // Workset checkpoints and a mounted archive page can describe the same revision.
  const revisions = JSON.stringify([...eligible].sort());
  const [acknowledged, setAcknowledged] = useState<ReadonlySet<string>>(() => new Set());
  const [lookupError, setLookupError] = useState<string | null>(null);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [pending, setPending] = useState<string | null>(null);
  const writing = useRef(false);

  useEffect(() => {
    const keys = new Set(JSON.parse(revisions) as string[]);
    if (!keys.size) {
      setLookupError(null);
      return;
    }
    const notices = [...keys].map((key) => {
      const [commandId, version] = JSON.parse(key) as [string, string];
      return create(CommandNoticeRevisionSchema, { commandId, receiptVersion: BigInt(version) });
    });
    let request: AbortController | undefined;
    const refresh = () => {
      request?.abort();
      const controller = new AbortController();
      request = controller;
      getCommandNoticeAcknowledgements(sessionId, notices, controller.signal)
        .then((acknowledgements) => {
          if (controller.signal.aborted) return;
          setAcknowledged((previous) => {
            // Recorded acknowledgements are additive. A lookup started before a successful
            // mutation can return a page that does not yet include it.
            const matches = new Set([...keys].filter((key) => previous.has(key)));
            for (const { notice } of acknowledgements) {
              if (notice && keys.has(noticeKey(notice))) matches.add(noticeKey(notice));
            }
            return matches;
          });
          setLookupError(null);
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setLookupError(`Could not load acknowledgements. ${displayableError(error)}`);
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
  }, [sessionId, revisions]);

  return {
    issues,
    acknowledged: (item) => acknowledged.has(noticeKey(item)),
    canAcknowledge: (item) => isDeliveryIssue(item.state) && eligible.has(noticeKey(item)),
    historyRevision,
    pending,
    error: writeError ?? lookupError ?? selectedError,
    acknowledge: (item) => {
      const receiptVersion = item.receiptVersion;
      const key = noticeKey(item);
      if (
        !isDeliveryIssue(item.state) ||
        !eligible.has(key) ||
        receiptVersion === undefined ||
        acknowledged.has(key) ||
        writing.current
      )
        return;
      writing.current = true;
      setPending(key);
      setWriteError(null);
      const notice: CommandNoticeRevision = create(CommandNoticeRevisionSchema, {
        commandId: item.commandId,
        receiptVersion,
      });
      acknowledgeCommandNotice(sessionId, notice)
        .then(() => {
          setAcknowledged((previous) => new Set([...previous, key]));
          setHistoryRevision((revision) => revision + 1);
        })
        .catch((error: unknown) => {
          setWriteError(`Could not acknowledge the delivery issue. ${displayableError(error)}`);
        })
        .finally(() => {
          writing.current = false;
          setPending(null);
        });
    },
  };
}
