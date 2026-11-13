import { Box, Button, Group, Paper, Stack, Text } from "@mantine/core";
import { type JSX, useEffect } from "react";

import { Body, UserInputBubble, pendingSentMessage } from "./thread_cards";
import { CommandProgress, type CommandStage, type CommandSubject } from "./command_progress";
import { EvidencePanel, EvidenceToggle, revealEvidenceOnTap } from "./thread_evidence";
import { type CommandIssue } from "./command_delivery";
import type { LocalCommands, LocalCommand, PendingCommand } from "./local_commands";
import { useThreadSync } from "./thread_sync";
import {
  ThreadCommandOutcome,
  type ThreadCommand,
  type ThreadEntity,
} from "../../../history_service/api/history/history_service_pb";
import { Operation } from "../../../proto/command_pb";
import { DeliveryState } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

type CommandEntity = ThreadEntity & { entity: { case: "command"; value: ThreadCommand } };

function isCommandEntity(entity: ThreadEntity): entity is CommandEntity {
  return entity.entity.case === "command";
}

/** A projected command's `Operation`, or a local command's operation case. */
function subject(operation: Operation | PendingCommand["operation"]["case"]): CommandSubject {
  if (operation === Operation.SUBMIT_INPUT || operation === "submitInput") return "input";
  if (operation === Operation.CHANGE_MODEL || operation === "changeModel") return "model";
  if (operation === Operation.CHANGE_REASONING_EFFORT || operation === "changeReasoningEffort") return "effort";
  return "other";
}

function progressStage(
  outcome: ThreadCommandOutcome | null,
  admitted: boolean,
  receipt?: LocalCommand["dispatch"] | LocalCommand["interruptResult"],
  issue?: CommandIssue
): CommandStage {
  if (outcome === ThreadCommandOutcome.EFFECTED) return "effected";
  if (outcome === ThreadCommandOutcome.FAILED) return "failed";
  if (outcome === ThreadCommandOutcome.NOOP) return "noop";
  if (admitted) return "admitted";
  switch (receipt?.state) {
    case DeliveryState.QUEUED_IN_DISPATCHER:
      return "queued";
    case DeliveryState.DELIVERING_TO_RUNNER:
      return "delivering";
    case DeliveryState.RUNNER_ADMITTED:
    case DeliveryState.HARNESS_CONFIRMED:
      return "admitted";
    case DeliveryState.NO_OP:
      return "noop";
    case DeliveryState.FAILED:
      return "dispatch_failed";
    case DeliveryState.CANCELLED_IN_DISPATCHER:
      return "cancelled";
    case DeliveryState.UNKNOWN:
      return "unknown";
  }
  return issue?.kind ?? "local";
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
      .filter((row) => row.entity.case === "confirmedInput")
      .flatMap((row) => (row.entity.case === "confirmedInput" ? row.entity.value.originCommandIds : []))
  );
  const historyMessageIds = new Set(entities.filter(pendingSentMessage).map((row) => row.entityId));
  const projectedOutcomes = entities
    .filter(
      (row): row is CommandEntity =>
        isCommandEntity(row) &&
        row.entity.value.operation === Operation.SUBMIT_INPUT &&
        row.entity.value.outcome !== ThreadCommandOutcome.PENDING &&
        row.entity.value.outcome !== ThreadCommandOutcome.UNSPECIFIED &&
        !localCommandIds.has(row.entityId) &&
        !confirmedCommandIds.has(row.entityId)
    )
    .sort((left, right) => {
      const leftCursor = left.cursor;
      const rightCursor = right.cursor;
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
        const commandRow = row && isCommandEntity(row) ? row : null;
        const outcome = commandRow?.entity.value.outcome ?? null;
        const issue = row ? undefined : errors.get(id);
        const dispatchState = value.dispatch?.state;
        const dispatchAdmitted =
          dispatchState === DeliveryState.RUNNER_ADMITTED || dispatchState === DeliveryState.HARNESS_CONFIRMED;
        const admitted = row !== undefined || value.admission !== null || dispatchAdmitted;
        const stage = progressStage(outcome, admitted, value.dispatch, issue);
        const failed =
          outcome === ThreadCommandOutcome.FAILED ||
          stage === "dispatch_failed" ||
          stage === "cancelled" ||
          stage === "unknown";
        const noop = outcome === ThreadCommandOutcome.NOOP || stage === "noop";
        const effected = outcome === ThreadCommandOutcome.EFFECTED;
        const outcomeReason = commandRow?.entity.value.outcomeReason ?? null;
        return (
          <UserInputBubble
            key={id}
            commandId={id}
            text={value.command.operation.value.text}
            phase={failed ? "failed" : noop ? "noop" : effected ? "confirmed" : admitted ? "pending" : "local"}
            pending={(outcome === null || outcome === ThreadCommandOutcome.PENDING) && !failed && !noop}
            progress={
              effected ? undefined : (
                <CommandOutcome
                  stage={stage}
                  subject="input"
                  reason={outcomeReason ?? value.dispatch?.reason ?? issue?.message}
                  local
                  unsent={!value.attempted}
                />
              )
            }
            action={
              !admitted && !value.attempted && value.dispatch === null
                ? { label: "Cancel", onClick: () => store.cancelUnsent(id) }
                : !admitted && value.dispatch === null && (value.sessionAddressed || value.target !== null)
                  ? { label: "Retry", onClick: () => void deliver(value) }
                  : undefined
            }
          />
        );
      })}
      {projectedOutcomes.map((row) => {
        const command = row.entity.value;
        const failed = command.outcome === ThreadCommandOutcome.FAILED;
        const noop = command.outcome === ThreadCommandOutcome.NOOP;
        return (
          <UserInputBubble
            key={row.entityId}
            threadId={row.sessionId}
            entity={row}
            fallbackText={inputEchoes?.get(row.entityId)}
            onFallbackLoaded={onInputEchoLoaded ? () => onInputEchoLoaded(row.entityId) : undefined}
            phase={failed ? "failed" : noop ? "noop" : "confirmed"}
            progress={
              <CommandOutcome
                stage={failed ? "failed" : noop ? "noop" : "effected"}
                subject="input"
                reason={command.outcomeReason}
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
  const projectedIds = new Set([...rows, ...entities.filter(isCommandEntity)].map((row) => row.entityId));
  return (
    <Stack gap="xs">
      {commands
        .filter((value) => !projectedIds.has(value.command.commandId))
        .map((value) => {
          const operation = value.command.operation;
          const admitted = value.admission !== null;
          const settled =
            Boolean(value.interruptResult) ||
            (value.dispatch !== null &&
              ![DeliveryState.QUEUED_IN_DISPATCHER, DeliveryState.DELIVERING_TO_RUNNER].includes(value.dispatch.state));
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
                        : "Thread action"}
                </Text>
                <CommandOutcome
                  stage={progressStage(null, admitted, value.dispatch ?? value.interruptResult, issue)}
                  subject={subject(operation.case)}
                  reason={
                    admitted ? undefined : (value.dispatch?.reason ?? value.interruptResult?.reason ?? issue?.message)
                  }
                  local
                  unsent={!value.attempted}
                />
                {!admitted &&
                  !settled &&
                  value.dispatch === null &&
                  !value.interruptResult &&
                  (!value.attempted || value.sessionAddressed || value.target !== null) && (
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
  if (!isCommandEntity(row) || row.entity.value.operation === Operation.SUBMIT_INPUT) return null;
  const command = row.entity.value;
  return (
    <ControlCommandFrame id={row.entityId}>
      <Box className="transcript-inspection-owner" onClick={revealEvidenceOnTap}>
        <EvidenceToggle entity={row} />
        <Group className="agentplane-command-card-line" gap="xs" wrap="nowrap" pr="xl">
          <Text size="sm" className="agentplane-command-card-description">
            {command.operation === Operation.CHANGE_MODEL
              ? command.requestedValue
                ? `Change model to ${command.requestedValue}`
                : "Model change"
              : command.operation === Operation.CHANGE_REASONING_EFFORT
                ? command.requestedValue
                  ? `Set reasoning effort to ${command.requestedValue}`
                  : "Reasoning effort change"
                : command.operation === Operation.INTERRUPT_TURN
                  ? "Interrupt turn"
                  : "Thread action"}
          </Text>
          <CommandOutcome
            stage={progressStage(command.outcome, true)}
            subject={subject(command.operation)}
            reason={command.outcomeReason}
          />
        </Group>
        {command.inputRef && <Body reference={command.inputRef} format="text" />}
        <EvidencePanel threadId={threadId} entity={row} />
      </Box>
    </ControlCommandFrame>
  );
}
