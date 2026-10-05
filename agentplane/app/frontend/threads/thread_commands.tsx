import { Button, Paper, Stack, Text } from "@mantine/core";
import { type Command } from "../../../protocol/command_pb";
import { type JSX, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { command, displayableError } from "../client";
import { Body, UserInputBubble, pendingSentMessage } from "./thread_cards";
import { EvidencePanel, EvidenceToggle } from "./thread_evidence";
import { LocalCommands, type LocalCommand, type LocalCommandSnapshot } from "./local_commands";
import { decimalBigInt, useThreadSync, type ThreadEntity } from "./thread_sync";

const EMPTY_LOCAL: LocalCommandSnapshot = { commands: [], dismissedCommandIds: [], error: null };

export function pruneCommandErrors(errors: Map<string, string>, commandIds: ReadonlySet<string>): Map<string, string> {
  if (Array.from(errors.keys()).every((id) => commandIds.has(id))) return errors;
  return new Map(Array.from(errors).filter(([id]) => commandIds.has(id)));
}

interface ProjectedCommands {
  local: LocalCommandSnapshot;
  errors: ReadonlyMap<string, string>;
  submissionError: string | null;
  submit: (value: Command) => boolean;
  deliver: (value: LocalCommand) => Promise<void>;
  store: LocalCommands;
}

export function useProjectedCommands(threadId: string, entities: ThreadEntity[]): ProjectedCommands {
  const store = useMemo(() => new LocalCommands(threadId), [threadId]);
  const local = useSyncExternalStore(store.subscribe, store.getSnapshot, () => EMPTY_LOCAL);
  const [errors, setErrors] = useState(new Map<string, string>());
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
      if (active.current.has(id)) return;
      active.current.add(id);
      try {
        store.acknowledge(value.command, await command(threadId, value.command));
        setErrors((previous) => {
          if (!previous.has(id)) return previous;
          const next = new Map(previous);
          next.delete(id);
          return next;
        });
      } catch (reason) {
        if (store.getSnapshot().commands.some((command) => command.command.commandId === id))
          setErrors((previous) => new Map(previous).set(id, displayableError(reason)));
      } finally {
        active.current.delete(id);
      }
    },
    [store, threadId]
  );
  // Deliver each retained command not yet seen admitted once on mount, and again when the browser
  // comes back online after a failed attempt. An exact retry is safe: the server answers it from its
  // archive once admitted, and the runner deduplicates by command id.
  useEffect(() => {
    for (const value of store.getSnapshot().commands) if (value.admission === null) void deliver(value);
  }, [deliver, store]);
  useEffect(() => {
    const redeliverFailed = () => {
      for (const value of store.getSnapshot().commands)
        if (value.admission === null && errors.has(value.command.commandId)) void deliver(value);
    };
    window.addEventListener("online", redeliverFailed);
    return () => window.removeEventListener("online", redeliverFailed);
  }, [deliver, errors, store]);
  function submit(value: Command): boolean {
    try {
      void deliver(store.remember(value));
      setSubmissionError(null);
      return true;
    } catch (reason) {
      setSubmissionError(displayableError(reason));
      return false;
    }
  }
  return { local, errors, submissionError, submit, deliver, store };
}

export function SelectedCommandOutcomes({
  commands,
  store,
  errors,
  deliver,
}: {
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, string>;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element | null {
  const controls = commands.filter((value) => value.command.operation.case !== "submitInput");
  const rows = useThreadSync().useCommandRows(controls.slice(0, 128).map((value) => value.command.commandId));
  if (controls.length === 0) return null;
  return <SelectedCommandRows rows={rows} commands={controls} store={store} errors={errors} deliver={deliver} />;
}

/** Keep unadmitted input and input outcomes without confirmed messages at the history tail. */
export function PendingInputMessages({
  commands,
  entities,
  errors,
  store,
  deliver,
}: {
  commands: LocalCommand[];
  entities: ThreadEntity[];
  errors: ReadonlyMap<string, string>;
  store: LocalCommands;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element | null {
  const inputCommands = commands.slice(0, 128).filter((value) => value.command.operation.case === "submitInput");
  const rows = useThreadSync().useCommandRows(inputCommands.map((value) => value.command.commandId));
  const byId = new Map(rows.map((row) => [row.entityId, row]));
  const localCommandIds = new Set(inputCommands.map((value) => value.command.commandId));
  const confirmedCommandIds = new Set(
    entities
      .filter((row) => row.entityKind === "confirmed_input")
      .flatMap((row) => ("origin_command_ids" in row.state ? row.state.origin_command_ids : []))
  );
  const historyMessageIds = new Set(entities.filter(pendingSentMessage).map((row) => row.entityId));
  const projectedOutcomes = entities
    .filter(
      (row): row is ThreadEntity & { state: Extract<ThreadEntity["state"], { outcome: string }> } =>
        row.entityKind === "command" &&
        "outcome" in row.state &&
        row.state.operation === "submit_input" &&
        row.state.outcome !== "pending" &&
        !localCommandIds.has(row.entityId) &&
        !confirmedCommandIds.has(row.entityId) &&
        !store.isDismissed(row.entityId)
    )
    .sort((left, right) => {
      const leftCursor = decimalBigInt(left.cursor);
      const rightCursor = decimalBigInt(right.cursor);
      return leftCursor < rightCursor ? -1 : leftCursor > rightCursor ? 1 : 0;
    });
  const pending = inputCommands.filter(
    (value) => !historyMessageIds.has(value.command.commandId) && !confirmedCommandIds.has(value.command.commandId)
  );
  if (pending.length === 0 && projectedOutcomes.length === 0) return null;
  return (
    <Stack className="agentplane-input-messages" role="region" aria-label="Input messages" gap="xs" pt="xs">
      {pending.map((value) => {
        if (value.command.operation.case !== "submitInput") return null;
        const id = value.command.commandId;
        const row = byId.get(id);
        const outcome = row && "outcome" in row.state ? row.state.outcome : null;
        const admitted = row !== undefined || value.admission !== null;
        const failed = outcome === "failed";
        const noop = outcome === "noop";
        const effected = outcome === "effected";
        const outcomeReason = row && "outcome" in row.state ? row.state.outcome_reason : null;
        return (
          <UserInputBubble
            key={id}
            commandId={id}
            text={value.command.operation.value.text}
            phase={failed ? "failed" : noop ? "noop" : effected ? "confirmed" : admitted ? "pending" : "local"}
            pending={outcome === null}
            status={
              failed
                ? `Input failed${outcomeReason ? `: ${outcomeReason}` : ""}`
                : noop
                  ? `Input not applied${outcomeReason ? `: ${outcomeReason}` : ""}`
                  : effected
                    ? undefined
                    : admitted
                      ? "Saved · awaiting effect"
                      : "Saved locally · awaiting admission"
            }
            statusColor={failed ? "red" : "dimmed"}
            error={errors.get(id)}
            action={
              failed || noop
                ? { label: "Dismiss", onClick: () => store.dismiss(id) }
                : !admitted
                  ? { label: "Retry", onClick: () => void deliver(value) }
                  : undefined
            }
          />
        );
      })}
      {projectedOutcomes.map((row) => {
        const failed = row.state.outcome === "failed";
        const noop = row.state.outcome === "noop";
        return (
          <UserInputBubble
            key={row.entityId}
            threadId={row.threadId}
            entity={row}
            phase={failed ? "failed" : noop ? "noop" : "confirmed"}
            status={`${failed ? "Input failed" : noop ? "Input not applied" : "Input applied"}${row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}`}
            statusColor={failed ? "red" : "dimmed"}
            action={{ label: "Dismiss", onClick: () => store.dismiss(row.entityId) }}
          />
        );
      })}
    </Stack>
  );
}

function SelectedCommandRows({
  rows,
  commands,
  store,
  errors,
  deliver,
}: {
  rows: ThreadEntity[];
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, string>;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element {
  useEffect(() => {
    for (const row of rows) {
      if ("outcome" in row.state && row.state.outcome === "effected") store.dismiss(row.entityId);
    }
  }, [rows, store]);
  const byId = new Map(rows.map((row) => [row.entityId, row]));
  return (
    <Stack role="region" aria-label="Pending commands" gap="xs">
      {commands.map((value) => {
        const row = byId.get(value.command.commandId);
        // Once the server has admitted the command and is still working on it, it has a cursor and
        // renders inline in history as a pending message bubble instead -- same as any other
        // pending submitInput command, whether or not this browser is the one tracking it locally.
        if (row && pendingSentMessage(row)) return null;
        const admitted = row !== undefined || value.admission !== null;
        const terminal = row && "outcome" in row.state && ["failed", "noop"].includes(row.state.outcome);
        return (
          <Paper key={value.command.commandId} data-command-id={value.command.commandId} p="xs" withBorder>
            {terminal && row && "outcome" in row.state ? (
              <>
                <Text c={row.state.outcome === "failed" ? "red" : undefined}>
                  {commandOutcomeLabel(row.state.operation, row.state.outcome)}
                  {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
                </Text>
                {row.inputRef && <Body reference={row.inputRef} format="text" />}
                <Button variant="subtle" onClick={() => store.dismiss(row.entityId)}>
                  Dismiss
                </Button>
              </>
            ) : (
              <>
                <Text size="sm">{admitted ? "Saved · awaiting effect" : "Saved locally · awaiting admission"}</Text>
                {value.command.operation.case === "changeModel" && (
                  <Text>Change model to {value.command.operation.value.model}</Text>
                )}
                {value.command.operation.case === "interruptTurn" && (
                  <Text>Interrupt turn {value.command.operation.value.turnId}</Text>
                )}
                {value.command.operation.case === "stopRunnerSession" && <Text>Shut down harness</Text>}
                {!admitted && errors.get(value.command.commandId) && (
                  <Text c="red">{errors.get(value.command.commandId)}</Text>
                )}
                {!admitted && <Button onClick={() => void deliver(value)}>Retry</Button>}
              </>
            )}
          </Paper>
        );
      })}
    </Stack>
  );
}

function commandOutcomeLabel(operation: string, outcome: string): string {
  const subject =
    {
      submit_input: "Input",
      change_model: "Model change",
      change_reasoning_effort: "Reasoning effort change",
      interrupt_turn: "Interrupt",
      stop_runner_session: "Harness shutdown",
    }[operation] ?? "Command";
  return `${subject} ${outcome === "failed" ? "failed" : outcome === "noop" ? "not applied" : "applied"}`;
}

/** Server-side commands this browser did not retain locally, excluding submit-input commands
 * already displayed inline in the ordered history. */
export function ProjectedCommandRows({
  threadId,
  entities,
  localCommands,
  dismissedCommandIds,
}: {
  threadId: string;
  entities: ThreadEntity[];
  localCommands: LocalCommand[];
  dismissedCommandIds: string[];
}): JSX.Element | null {
  const localCommandIds = new Set(localCommands.map((value) => value.command.commandId));
  const dismissedIds = new Set(dismissedCommandIds);
  const confirmedCommandIds = new Set(
    entities
      .filter((row) => row.entityKind === "confirmed_input")
      .flatMap((row) => ("origin_command_ids" in row.state ? row.state.origin_command_ids : []))
  );
  const projectedCommands = entities.filter(
    (row): row is ThreadEntity & { state: Extract<ThreadEntity["state"], { outcome: string }> } =>
      row.entityKind === "command" &&
      "outcome" in row.state &&
      !localCommandIds.has(row.entityId) &&
      !dismissedIds.has(row.entityId) &&
      !confirmedCommandIds.has(row.entityId) &&
      !pendingSentMessage(row)
  );
  const otherCommands = projectedCommands.filter((row) => row.state.operation !== "submit_input");
  if (otherCommands.length === 0) return null;
  return (
    <Stack role="region" aria-label="Pending commands" gap="xs">
      {otherCommands.map((row) => (
        <Paper
          key={row.entityId}
          data-command-id={row.entityId}
          p="xs"
          withBorder
          className="agentplane-evidence-owner"
        >
          <EvidenceToggle entity={row} />
          <Text size="xs" c={row.pending ? "dimmed" : row.state.outcome === "failed" ? "red" : undefined}>
            {row.state.outcome === "pending"
              ? "Saved · awaiting effect"
              : commandOutcomeLabel(row.state.operation, row.state.outcome)}
            {row.state.outcome_reason ? `: ${row.state.outcome_reason}` : ""}
          </Text>
          {row.inputRef && <Body reference={row.inputRef} format="text" />}
          <EvidencePanel threadId={threadId} entity={row} />
        </Paper>
      ))}
    </Stack>
  );
}
