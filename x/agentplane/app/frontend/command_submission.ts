import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type { Command } from "../../protocol/command_pb";
import { command, displayableError } from "./client";
import { observedCommands, type ObservedCommand } from "./command_state";
import type { EventStream } from "./event_stream";
import { LocalCommands, type LocalCommand, type LocalCommandSnapshot } from "./local_commands";

const EMPTY: LocalCommandSnapshot = { commands: [], error: null };
const emptySnapshot = (): LocalCommandSnapshot => EMPTY;
const noSubscription = (): (() => void) => () => {};

export interface CommandSubmission {
  observed: ObservedCommand[];
  local: LocalCommand[];
  sending: ReadonlySet<string>;
  errors: ReadonlyMap<string, string>;
  error: string | null;
  submit: (value: Command) => boolean;
  retry: (value: LocalCommand) => Promise<void>;
}

/** Transport responses and Event replay are independent. HTTP admission never feeds the reducer
 * or moves its cursor; the verified stream transfers locally retained work to the runner fold. */
export function useCommandSubmission(threadId: string | null, stream: EventStream): CommandSubmission {
  const store = useMemo(() => (threadId ? new LocalCommands(threadId) : null), [threadId]);
  const local = useSyncExternalStore(store?.subscribe ?? noSubscription, store?.getSnapshot ?? emptySnapshot);
  const snapshot = useSyncExternalStore(stream.subscribe, stream.getSnapshot);
  const observed = useMemo(() => observedCommands(snapshot.conversation.entries), [snapshot.conversation.entries]);
  const inFlight = useRef(new Set<string>());
  const [sending, setSending] = useState<ReadonlySet<string>>(new Set());
  const [errors, setErrors] = useState<ReadonlyMap<string, string>>(new Map());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    try {
      store?.observePrefix(snapshot.conversation.entries);
    } catch (reason) {
      setError(displayableError(reason));
    }
  }, [store, snapshot.conversation.entries]);

  async function deliver(value: LocalCommand): Promise<void> {
    if (!store || value.admission || inFlight.current.has(value.command.commandId)) return;
    const id = value.command.commandId;
    inFlight.current.add(id);
    setSending(new Set(inFlight.current));
    setErrors((previous) => {
      const next = new Map(previous);
      next.delete(id);
      return next;
    });
    try {
      const admission = await command(store.threadId, value.command);
      store.acknowledge(value.command, admission);
      store.observePrefix(stream.getSnapshot().conversation.entries);
    } catch (reason) {
      setErrors((previous) => new Map(previous).set(id, displayableError(reason)));
    } finally {
      inFlight.current.delete(id);
      setSending(new Set(inFlight.current));
    }
  }

  function submit(value: Command): boolean {
    if (!store) return false;
    try {
      const retained = store.remember(value);
      setError(null);
      void deliver(retained);
      return true;
    } catch (reason) {
      setError(displayableError(reason));
      return false;
    }
  }

  const admittedIds = new Set(observed.map((value) => value.command.commandId));
  return {
    observed,
    local: local.commands.filter((value) => !admittedIds.has(value.command.commandId)),
    sending,
    errors,
    error: local.error ?? error,
    submit,
    retry: deliver,
  };
}
