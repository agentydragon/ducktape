import { Box, Button, Group, Paper, Stack, Text } from "@mantine/core";
import { type Command } from "../../../protocol/command_pb";
import { type JSX, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { command, CommandSubmissionRefused, displayableError } from "../client";
import { Body, UserInputBubble, pendingSentMessage } from "./thread_cards";
import { CommandProgress, type CommandStage, type CommandSubject } from "./command_progress";
import { EvidencePanel, EvidenceToggle, revealEvidenceOnTap } from "./thread_evidence";
import { LocalCommands, type LocalCommand, type LocalCommandSnapshot } from "./local_commands";
import { decimalBigInt, useThreadSync, type ThreadEntity } from "./thread_sync";

const EMPTY_LOCAL: LocalCommandSnapshot = { commands: [], inputEchoes: [], error: null };

export interface CommandIssue {
  kind: "refused" | "unconfirmed";
  message: string;
}

export function pruneCommandErrors<T>(errors: Map<string, T>, commandIds: ReadonlySet<string>): Map<string, T> {
  if (Array.from(errors.keys()).every((id) => commandIds.has(id))) return errors;
  return new Map(Array.from(errors).filter(([id]) => commandIds.has(id)));
}

interface ProjectedCommands {
  local: LocalCommandSnapshot;
  errors: ReadonlyMap<string, CommandIssue>;
  submissionError: string | null;
  submit: (value: Command) => boolean;
  deliver: (value: LocalCommand) => Promise<void>;
  store: LocalCommands;
}

export function useProjectedCommands(threadId: string, entities: ThreadEntity[]): ProjectedCommands {
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
      if (active.current.has(id)) return;
      active.current.add(id);
      try {
        if (!navigator.onLine || !store.markAttempted(id)) return;
        store.acknowledge(value.command, await command(threadId, value.command));
        setErrors((previous) => {
          if (!previous.has(id)) return previous;
          const next = new Map(previous);
          next.delete(id);
          return next;
        });
      } catch (reason) {
        if (store.getSnapshot().commands.some((command) => command.command.commandId === id))
          setErrors((previous) =>
            new Map(previous).set(id, {
              kind: reason instanceof CommandSubmissionRefused ? "refused" : "unconfirmed",
              message: displayableError(reason),
            })
          );
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
        if (value.admission === null && (!value.attempted || errors.has(value.command.commandId))) void deliver(value);
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

function subject(operation: string): CommandSubject {
  if (operation === "submit_input" || operation === "submitInput") return "input";
  if (operation === "change_model" || operation === "changeModel") return "model";
  if (operation === "change_reasoning_effort" || operation === "changeReasoningEffort") return "effort";
  return "other";
}

function progressStage(outcome: string | null, admitted: boolean, issue?: CommandIssue): CommandStage {
  if (outcome === "effected" || outcome === "failed" || outcome === "noop") return outcome;
  if (admitted) return "admitted";
  if (issue?.kind === "refused") return "refused";
  return issue?.kind === "unconfirmed" ? "unconfirmed" : "local";
}

/** No-op is a settled result, not a fourth progress state. Keep its reason readable,
 * with ellipsis in narrow gutters and a title for the complete text. */
function CommandOutcome({
  stage,
  subject: commandSubject,
  reason,
  local,
  unsent,
}: {
  stage: CommandStage;
  subject: CommandSubject;
  reason?: string | null;
  local?: boolean;
  unsent?: boolean;
}): JSX.Element {
  if (stage === "noop") {
    const label = reason ? `No-op: ${reason}` : "No-op";
    return (
      <Text size="xs" c="dimmed" className="agentplane-command-noop" role="status" title={reason ? label : undefined}>
        {label}
      </Text>
    );
  }
  return <CommandProgress stage={stage} subject={commandSubject} reason={reason} local={local} unsent={unsent} />;
}

export function LocalControlCommands({
  commands,
  entities,
  store,
  errors,
  deliver,
}: {
  commands: LocalCommand[];
  entities: ThreadEntity[];
  store: LocalCommands;
  errors: ReadonlyMap<string, CommandIssue>;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element | null {
  const controls = commands.filter((value) => value.command.operation.case !== "submitInput");
  const rows = useThreadSync().useCommandRows(controls.map((value) => value.command.commandId));
  if (controls.length === 0) return null;
  return (
    <LocalControlRows
      rows={rows}
      entities={entities}
      commands={controls}
      store={store}
      errors={errors}
      deliver={deliver}
    />
  );
}

/** Keep unadmitted input and input outcomes without confirmed messages at the history tail. */
export function PendingInputMessages({
  commands,
  entities,
  inputEchoes,
  onInputEchoLoaded,
  errors,
  store,
  deliver,
}: {
  commands: LocalCommand[];
  entities: ThreadEntity[];
  inputEchoes?: ReadonlyMap<string, string>;
  onInputEchoLoaded?: (commandId: string) => void;
  errors: ReadonlyMap<string, CommandIssue>;
  store: LocalCommands;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element | null {
  const inputCommands = commands.filter((value) => value.command.operation.case === "submitInput");
  const rows = useThreadSync().useCommandRows(inputCommands.map((value) => value.command.commandId));
  const byId = new Map(rows.map((row) => [row.entityId, row]));
  useEffect(() => store.observeCommandIds(new Set(rows.map((row) => row.entityId))), [rows, store]);
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
        !confirmedCommandIds.has(row.entityId)
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
        const issue = row ? undefined : errors.get(id);
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
            progress={
              effected ? undefined : (
                <CommandOutcome
                  stage={progressStage(outcome, admitted, issue)}
                  subject="input"
                  reason={failed || noop ? outcomeReason : issue?.message}
                  local
                  unsent={!value.attempted}
                />
              )
            }
            action={
              !admitted && !value.attempted
                ? { label: "Cancel", onClick: () => store.cancelUnsent(id) }
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
            fallbackText={inputEchoes?.get(row.entityId)}
            onFallbackLoaded={onInputEchoLoaded ? () => onInputEchoLoaded(row.entityId) : undefined}
            phase={failed ? "failed" : noop ? "noop" : "confirmed"}
            progress={
              <CommandOutcome
                stage={failed ? "failed" : noop ? "noop" : "effected"}
                subject="input"
                reason={row.state.outcome_reason}
              />
            }
          />
        );
      })}
    </Stack>
  );
}

/** Locally retained controls have no place in the server timeline until their projected row
 * arrives. Keep them at the history tail, then let that row take over at its cursor. */
function LocalControlRows({
  rows,
  entities,
  commands,
  store,
  errors,
  deliver,
}: {
  rows: ThreadEntity[];
  entities: ThreadEntity[];
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, CommandIssue>;
  deliver: (value: LocalCommand) => Promise<void>;
}): JSX.Element {
  useEffect(() => {
    store.observeCommandIds(new Set(rows.map((row) => row.entityId)));
  }, [rows, store]);
  const projectedIds = new Set(
    [...rows, ...entities.filter((row) => row.entityKind === "command")].map((row) => row.entityId)
  );
  return (
    <Stack gap="xs">
      {commands
        .filter((value) => !projectedIds.has(value.command.commandId))
        .map((value) => {
          const operation = value.command.operation;
          const admitted = value.admission !== null;
          const issue = errors.get(value.command.commandId);
          return (
            <ControlCommandFrame key={value.command.commandId} id={value.command.commandId}>
              <Group className="agentplane-command-card-line" gap="xs" wrap="nowrap">
                <Text size="sm" className="agentplane-command-card-description">
                  {operation.case === "changeModel"
                    ? `Change model to ${operation.value.model}`
                    : operation.case === "changeReasoningEffort"
                      ? `Set reasoning effort to ${operation.value.effort}`
                      : operation.case === "interruptTurn"
                        ? `Interrupt turn ${operation.value.turnId}`
                        : "Shut down harness"}
                </Text>
                <CommandOutcome
                  stage={progressStage(null, admitted, issue)}
                  subject={subject(operation.case ?? "")}
                  reason={admitted ? undefined : issue?.message}
                  local
                  unsent={!value.attempted}
                />
                {!admitted && (
                  <Button
                    size="xs"
                    variant="subtle"
                    onClick={() =>
                      value.attempted ? void deliver(value) : store.cancelUnsent(value.command.commandId)
                    }
                  >
                    {value.attempted ? "Retry" : "Cancel"}
                  </Button>
                )}
              </Group>
            </ControlCommandFrame>
          );
        })}
    </Stack>
  );
}

function ControlCommandFrame({ id, children }: { id: string; children: JSX.Element | JSX.Element[] }): JSX.Element {
  return (
    <Box className="agentplane-control-command-row" data-command-id={id}>
      <Paper p="xs" withBorder className="agentplane-control-command-card">
        {children}
      </Paper>
    </Box>
  );
}

/** The projected control is a chronological entry, whether pending or settled. */
export function ProjectedControlCommand({
  threadId,
  row,
}: {
  threadId: string;
  row: ThreadEntity;
}): JSX.Element | null {
  if (row.entityKind !== "command" || !("outcome" in row.state) || row.state.operation === "submit_input") return null;
  return (
    <ControlCommandFrame id={row.entityId}>
      <Box className="agentplane-evidence-owner" onClick={revealEvidenceOnTap}>
        <EvidenceToggle entity={row} />
        <Group className="agentplane-command-card-line" gap="xs" wrap="nowrap" pr="xl">
          <Text size="sm" className="agentplane-command-card-description">
            {row.state.operation === "change_model"
              ? row.state.requested_value
                ? `Change model to ${row.state.requested_value}`
                : "Model change"
              : row.state.operation === "change_reasoning_effort"
                ? row.state.requested_value
                  ? `Set reasoning effort to ${row.state.requested_value}`
                  : "Reasoning effort change"
                : row.state.operation === "interrupt_turn"
                  ? "Interrupt turn"
                  : "Shut down harness"}
          </Text>
          <CommandOutcome
            stage={progressStage(row.state.outcome, true)}
            subject={subject(row.state.operation)}
            reason={row.state.outcome_reason}
          />
        </Group>
        {row.inputRef && <Body reference={row.inputRef} format="text" />}
        <EvidencePanel threadId={threadId} entity={row} />
      </Box>
    </ControlCommandFrame>
  );
}
