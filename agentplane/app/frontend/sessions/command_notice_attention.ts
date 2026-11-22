import { create } from "@bufbuild/protobuf";
import { useEffect, useState } from "react";

import { CommandNoticeRevisionSchema, type CommandNoticeRevision } from "../../app_service_pb";
import {
  DeliveryState,
  type DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { acknowledgeCommandNotice, getCommandNoticeAcknowledgements } from "./command_notice_client";

const noticeChanged = new EventTarget();

type Receipt = { commandId: string; receiptVersion?: bigint };

export function noticeKey(receipt: Receipt): string {
  return JSON.stringify([receipt.commandId, receipt.receiptVersion?.toString()]);
}

export function isDeliveryIssue(state: DeliveryState): boolean {
  return state === DeliveryState.FAILED || state === DeliveryState.UNKNOWN;
}

export interface NoticeAttention {
  issues: readonly DispatchWorksetItem[];
  stale: boolean;
  acknowledged: (item: Receipt) => boolean;
  acknowledge: (item: DispatchWorksetItem) => void;
  pending: string | null;
  error: string | null;
}

interface Scope {
  sessionId: string;
  keys: ReadonlySet<string>;
  pending: string | null;
  error: string | null;
}

export const NO_ISSUES: readonly DispatchWorksetItem[] = [];

export function useCommandNoticeAttention(
  sessionId: string,
  issues: readonly DispatchWorksetItem[],
  stale: boolean
): NoticeAttention {
  // A new watch checkpoint can carry identical issues. Only a change of exact identities triggers a read.
  const revisions = JSON.stringify(
    stale
      ? []
      : issues
          .filter((item) => (item.receiptVersion ?? 0n) > 0n)
          .map(noticeKey)
          .sort()
  );
  const [scope, setScope] = useState<Scope | null>(null);
  const current = scope?.sessionId === sessionId ? scope : null;
  const inSession = (state: Scope | null): Scope =>
    state?.sessionId === sessionId ? state : { sessionId, keys: new Set(), pending: null, error: null };

  useEffect(() => {
    const keys = new Set(JSON.parse(revisions) as string[]);
    if (!keys.size) return;
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
          setScope((previous) => {
            const next = previous?.sessionId === sessionId ? previous : null;
            const matches = new Set([...keys].filter((key) => next?.keys.has(key)));
            for (const { notice } of acknowledgements) {
              if (notice && keys.has(noticeKey(notice))) matches.add(noticeKey(notice));
            }
            return { sessionId, keys: matches, pending: next?.pending ?? null, error: null };
          });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setScope((previous) => ({
            sessionId,
            keys: previous?.sessionId === sessionId ? previous.keys : new Set(),
            pending: previous?.sessionId === sessionId ? previous.pending : null,
            error: `Could not load acknowledgements. ${displayableError(error)}`,
          }));
        });
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    refresh();
    document.addEventListener("visibilitychange", refreshWhenVisible);
    noticeChanged.addEventListener("change", refresh);
    return () => {
      request?.abort();
      document.removeEventListener("visibilitychange", refreshWhenVisible);
      noticeChanged.removeEventListener("change", refresh);
    };
  }, [sessionId, revisions]);

  return {
    issues,
    stale,
    acknowledged: (item) => current?.keys.has(noticeKey(item)) ?? false,
    pending: current?.pending ?? null,
    error: current?.error ?? null,
    acknowledge: (item) => {
      const receiptVersion = item.receiptVersion;
      const key = noticeKey(item);
      if (stale || receiptVersion === undefined || receiptVersion <= 0n || !issues.includes(item) || current?.pending)
        return;
      setScope((previous) => ({ ...inSession(previous), pending: key, error: null }));
      const notice: CommandNoticeRevision = create(CommandNoticeRevisionSchema, {
        commandId: item.commandId,
        receiptVersion,
      });
      acknowledgeCommandNotice(sessionId, notice)
        .then(() => {
          setScope((previous) =>
            previous?.sessionId === sessionId
              ? { ...previous, keys: new Set([...previous.keys, key]), pending: null, error: null }
              : previous
          );
          noticeChanged.dispatchEvent(new Event("change"));
        })
        .catch((error: unknown) => {
          setScope((previous) =>
            previous?.sessionId === sessionId
              ? {
                  ...previous,
                  pending: null,
                  error: `Could not acknowledge the delivery issue. ${displayableError(error)}`,
                }
              : previous
          );
        });
    },
  };
}
