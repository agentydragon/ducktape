import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import {
  changeModel,
  changeReasoningEffort,
  CommandSubmissionRefused,
  interruptTurn,
  submitInput,
  type CommandSubmission,
} from "./client";
import { displayableError } from "../client";
import { watchDispatches } from "./dispatch_watch";
import { DeliveryState } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";
import { LocalCommands, type LocalCommand, type LocalCommandSnapshot, type PendingCommand } from "./local_commands";
import { type ThreadEntity } from "./thread_sync";

const EMPTY_LOCAL: LocalCommandSnapshot = { commands: [], inputEchoes: [], error: null };

export interface CommandIssue {
  kind: "refused" | "unconfirmed";
  message: string;
}

export function pruneCommandErrors<T>(errors: Map<string, T>, commandIds: ReadonlySet<string>): Map<string, T> {
  if (Array.from(errors.keys()).every((id) => commandIds.has(id))) return errors;
  return new Map(Array.from(errors).filter(([id]) => commandIds.has(id)));
}

interface ThreadCommands {
  local: LocalCommandSnapshot;
  errors: ReadonlyMap<string, CommandIssue>;
  submissionError: string | null;
  submitInput: (text: string) => boolean;
  changeModel: (model: string) => boolean;
  changeReasoningEffort: (effort: string) => boolean;
  interruptTurn: (turnId: string) => boolean;
  deliver: (value: LocalCommand) => Promise<void>;
  store: LocalCommands;
}

export function useThreadCommands(threadId: string, entities: ThreadEntity[], deliveryAllowed = true): ThreadCommands {
  const store = useMemo(() => new LocalCommands(threadId), [threadId]);
  const local = useSyncExternalStore(store.subscribe, store.getSnapshot, () => EMPTY_LOCAL);
  const [errors, setErrors] = useState(new Map<string, CommandIssue>());
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const active = useRef(new Set<string>());
  useEffect(() => {
    setErrors((previous) =>
      pruneCommandErrors(previous, new Set(local.commands.map((command) => command.command.commandId)))
    );
  }, [local.commands]);
  const effectedCommandIds = useMemo(
    () =>
      new Set([...entities.flatMap((row) => ("origin_command_ids" in row.state ? row.state.origin_command_ids : []))]),
    [entities]
  );
  useEffect(() => store.observeCommandIds(effectedCommandIds), [effectedCommandIds, store]);

  const deliver = useCallback(
    async (value: LocalCommand): Promise<void> => {
      const id = value.command.commandId;
      if (!deliveryAllowed || active.current.has(id)) return;
      active.current.add(id);
      const settle = (issue: CommandIssue | null) => {
        if (issue === null)
          setErrors((previous) => {
            if (!previous.has(id)) return previous;
            const next = new Map(previous);
            next.delete(id);
            return next;
          });
        else if (store.getSnapshot().commands.some((command) => command.command.commandId === id))
          setErrors((previous) => new Map(previous).set(id, issue));
      };
      try {
        if (!navigator.onLine || !store.markAttempted(id)) return;
        const operation = value.command.operation;
        const commandId = value.command.commandId;
        let result: CommandSubmission;
        switch (operation.case) {
          case "submitInput":
            result = await submitInput(threadId, commandId, operation.value.text);
            break;
          case "changeModel":
            result = await changeModel(threadId, commandId, operation.value.model);
            break;
          case "changeReasoningEffort":
            result = await changeReasoningEffort(threadId, commandId, operation.value.effort);
            break;
          case "interruptTurn":
            result = await interruptTurn(threadId, commandId, operation.value.turnId);
            break;
          default:
            throw new Error("Unknown retained operation");
        }
        if (result.kind === "archived_admission") store.acknowledge(value.command, result.admission);
        else if (result.kind === "interrupt_result") store.acknowledgeInterrupt(value.command, result.result);
        else store.acknowledgeDispatch(value.command, result.dispatch);
        settle(null);
      } catch (reason) {
        settle(
          // A lost reply says nothing about how far the command got; the thread's history shows its
          // admission once the runner journaled it.
          {
            kind: reason instanceof CommandSubmissionRefused ? "refused" : "unconfirmed",
            message: displayableError(reason),
          }
        );
      } finally {
        active.current.delete(id);
      }
    },
    [deliveryAllowed, store, threadId]
  );
  // Deliver each retained command with no server receipt once on mount. An exact retry is safe after
  // a lost reply because the Dispatcher deduplicates by command ID.
  useEffect(() => {
    for (const value of store.getSnapshot().commands)
      if (value.admission === null && value.dispatch === null && !value.interruptResult) void deliver(value);
  }, [deliver, store]);
  useEffect(() => {
    const redeliverFailed = () => {
      for (const value of store.getSnapshot().commands)
        if (
          value.admission === null &&
          value.dispatch === null &&
          !value.interruptResult &&
          (!value.attempted || errors.has(value.command.commandId))
        )
          void deliver(value);
    };
    window.addEventListener("online", redeliverFailed);
    return () => window.removeEventListener("online", redeliverFailed);
  }, [deliver, errors, store]);
  const watchedIds = local.commands
    .filter(
      (value) =>
        value.dispatch?.state === DeliveryState.QUEUED_IN_DISPATCHER ||
        value.dispatch?.state === DeliveryState.DELIVERING_TO_RUNNER
    )
    .map((value) => value.command.commandId);
  // Command IDs are UUIDs, so a comma joins them without ambiguity; the effect reopens the watch
  // only when the set changes.
  const watched = watchedIds.join(",");
  useEffect(() => {
    if (watched === "") return;
    return watchDispatches(store, threadId, watched.split(","));
  }, [store, threadId, watched]);
  function submit(value: PendingCommand): boolean {
    if (!deliveryAllowed) {
      setSubmissionError("This session is not accepting commands.");
      return false;
    }
    try {
      void deliver(store.remember(value));
      setSubmissionError(null);
      return true;
    } catch (reason) {
      setSubmissionError(displayableError(reason));
      return false;
    }
  }
  return {
    local,
    errors,
    submissionError,
    deliver,
    store,
    submitInput: (text: string) =>
      submit({ commandId: crypto.randomUUID(), operation: { case: "submitInput", value: { text } } }),
    changeModel: (model: string) =>
      submit({ commandId: crypto.randomUUID(), operation: { case: "changeModel", value: { model } } }),
    changeReasoningEffort: (effort: string) =>
      submit({ commandId: crypto.randomUUID(), operation: { case: "changeReasoningEffort", value: { effort } } }),
    interruptTurn: (turnId: string) =>
      submit({ commandId: crypto.randomUUID(), operation: { case: "interruptTurn", value: { turnId } } }),
  };
}
