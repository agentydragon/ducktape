import { Box, Button, Group, Paper, Stack, Text } from "@mantine/core";
import { type JSX, useEffect } from "react";

import { Body, UserInputBubble, pendingSentMessage } from "./thread_cards";
import { CommandProgress, dispatchReasonLabel, type CommandStage, type CommandSubject } from "./command_progress";
import type { RecentDispatchIssues } from "./dispatch_watch";
import { EvidencePanel, EvidenceToggle, revealEvidenceOnTap } from "./thread_evidence";
import { type CommandIssue } from "./command_delivery";
import type { LocalCommands, LocalCommand, PendingCommand } from "./local_commands";
import { useThreadSync } from "./thread_sync";
import { normalizeDeliveryState } from "./client";
import {
  ThreadCommandOutcome,
  type ThreadCommand,
  type ThreadEntity,
} from "../../../history_service/api/history/history_service_pb";
import { Operation } from "../../../proto/command_pb";
import {
  DeliveryState,
  type DispatchIssue,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";

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

function commandIssue(value: LocalCommand, errors: ReadonlyMap<string, CommandIssue>): CommandIssue | undefined {
  if (value.destination.kind === "reconcile-only" && !value.admission && !value.dispatch && !value.interruptResult) {
    return { kind: "unconfirmed", message: "Saved by an older app version. Waiting for History evidence." };
  }
  return errors.get(value.command.commandId);
}

function progressStage(
  outcome: ThreadCommandOutcome | null,
  admitted: boolean,
  receipt?: { state: DeliveryState } | null,
  issue?: CommandIssue
): CommandStage {
  if (outcome === ThreadCommandOutcome.EFFECTED) return "effected";
  if (outcome === ThreadCommandOutcome.FAILED) return "failed";
  if (outcome === ThreadCommandOutcome.NOOP) return "noop";
  if (admitted) return "admitted";
  switch (receipt ? normalizeDeliveryState(receipt.state) : undefined) {
    case DeliveryState.QUEUED_IN_DISPATCHER:
      return "queued";
    case DeliveryState.DELIVERING_TO_RUNNER:
      return "delivering";
    case DeliveryState.RUNNER_ADMITTED:
      return "admitted";
    case DeliveryState.FAILED:
      return "dispatch_failed";
    case DeliveryState.CANCELLED_IN_DISPATCHER:
      return "cancelled";
    case DeliveryState.UNKNOWN:
      return "unknown";
    default:
      return receipt ? "unknown" : (issue?.kind ?? "local");
  }
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
  issues,
}: {
  commands: LocalCommand[];
  entities: ThreadEntity[];
  store: LocalCommands;
  errors: ReadonlyMap<string, CommandIssue>;
  deliver: (value: LocalCommand) => Promise<void>;
  issues: ReadonlyMap<string, DispatchIssue>;
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
      issues={issues}
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
  issues,
}: {
  commands: LocalCommand[];
  entities: ThreadEntity[];
  inputEchoes?: ReadonlyMap<string, string>;
  onInputEchoLoaded?: (commandId: string) => void;
  errors: ReadonlyMap<string, CommandIssue>;
  store: LocalCommands;
  deliver: (value: LocalCommand) => Promise<void>;
  issues: ReadonlyMap<string, DispatchIssue>;
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
    <Stack role="region" aria-label="Input messages" gap="xs" pt="xs">
      {pending.map((value) => {
        if (value.command.operation.case !== "submitInput") return null;
        const id = value.command.commandId;
        const row = byId.get(id);
        const commandRow = row && isCommandEntity(row) ? row : null;
        const outcome = commandRow?.entity.value.outcome ?? null;
        const issue = row ? undefined : commandIssue(value, errors);
        const dispatchState = value.dispatch?.state;
        const dispatchAdmitted =
          dispatchState !== undefined && normalizeDeliveryState(dispatchState) === DeliveryState.RUNNER_ADMITTED;
        const admitted = row !== undefined || value.admission !== null || dispatchAdmitted;
        const receipt = value.attempted && !admitted ? (issues.get(id) ?? value.dispatch) : value.dispatch;
        const canAct = value.destination.kind === "session" && !admitted && receipt === null;
        const stage = progressStage(outcome, admitted, receipt, issue);
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
                  reason={outcomeReason ?? receipt?.reason ?? issue?.message}
                  local
                  unsent={!value.attempted}
                />
              )
            }
            action={
              canAct
                ? value.attempted
                  ? { label: "Retry", onClick: () => void deliver(value) }
                  : { label: "Cancel", onClick: () => store.cancelUnsent(id) }
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
  issues,
}: {
  rows: ThreadEntity[];
  entities: ThreadEntity[];
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, CommandIssue>;
  deliver: (value: LocalCommand) => Promise<void>;
  issues: ReadonlyMap<string, DispatchIssue>;
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
          const issue = commandIssue(value, errors);
          const receipt =
            (value.attempted && !admitted ? issues.get(value.command.commandId) : undefined) ??
            value.dispatch ??
            value.interruptResult;
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
                  stage={progressStage(null, admitted, receipt, issue)}
                  subject={subject(operation.case)}
                  reason={admitted ? undefined : (receipt?.reason ?? issue?.message)}
                  local
                  unsent={!value.attempted}
                />
                {value.destination.kind === "session" && !admitted && !receipt && (
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

function operationLabel(operation: Operation): string {
  switch (operation) {
    case Operation.SUBMIT_INPUT:
      return "Send message";
    case Operation.CHANGE_MODEL:
      return "Model change";
    case Operation.CHANGE_REASONING_EFFORT:
      return "Reasoning effort change";
    case Operation.INTERRUPT_TURN:
      return "Turn interrupt";
    default:
      return "Command delivery";
  }
}

export function RecentDeliveryIssues({
  issues,
  commands,
  entities,
}: {
  issues: RecentDispatchIssues;
  commands: readonly LocalCommand[];
  entities: readonly ThreadEntity[];
}): JSX.Element | null {
  const visibleLocally = new Set(
    commands
      .filter((row) => row.attempted && row.admission === null && row.dispatch?.state !== DeliveryState.RUNNER_ADMITTED)
      .map((row) => row.command.commandId)
  );
  const settledInHistory = new Set<string>();
  for (const row of entities) {
    if (row.entity.case === "confirmedInput") {
      for (const id of row.entity.value.originCommandIds) settledInHistory.add(id);
    } else if (
      row.entity.case === "command" &&
      row.entity.value.outcome !== ThreadCommandOutcome.PENDING &&
      row.entity.value.outcome !== ThreadCommandOutcome.UNSPECIFIED
    ) {
      settledInHistory.add(row.entityId);
    } else if (row.entity.case === "command") {
      // An admitted but unsettled History row cannot settle an uncertain delivery.
      visibleLocally.delete(row.entityId);
    }
  }
  const visible = issues.receipts.filter(
    (row) => !settledInHistory.has(row.commandId) && !visibleLocally.has(row.commandId)
  );
  const stale = issues.connection === "stale";
  if (!visible.length && !stale && !issues.moreAvailable && !issues.coverageLost) return null;
  return (
    <Paper component="section" aria-label="Recent delivery issues" p="xs" withBorder>
      <Stack gap="xs">
        <Text size="sm" fw={600}>
          Recent delivery issues
        </Text>
        {stale && (
          <Text size="xs" c="dimmed" role="status">
            {issues.receipts.length
              ? "Showing the last update while reconnecting."
              : "Delivery issues are unavailable while reconnecting."}
          </Text>
        )}
        {issues.coverageLost && (
          <Text size="xs" c="dimmed" role="status">
            Some earlier delivery receipts may have expired.
          </Text>
        )}
        {issues.moreAvailable && (
          <Text size="xs" c="dimmed">
            Older delivery issues are outside this view.
          </Text>
        )}
        {visible.map((row) => (
          <Stack key={row.commandId} gap={0} data-command-id={row.commandId} style={{ overflowWrap: "anywhere" }}>
            <Text size="sm">
              {operationLabel(row.operation)} ·{" "}
              {row.state === DeliveryState.FAILED ? "Delivery failed" : "Outcome unknown"}
            </Text>
            {row.reason && (
              <Text size="xs" c="dimmed">
                {row.reason === "sandbox_deleted" ? "Sandbox was deleted" : dispatchReasonLabel(row.reason)}
              </Text>
            )}
          </Stack>
        ))}
      </Stack>
    </Paper>
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
