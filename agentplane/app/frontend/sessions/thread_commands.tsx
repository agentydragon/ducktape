import { Button, Stack, Text } from "@mantine/core";
import { type JSX, type ReactNode, useEffect } from "react";

import { UserMessage, VerbatimText } from "../../../../util/frontend/transcript/message";
import { Body, UserInputBubble, pendingSentMessage } from "./thread_cards";
import { CommandProgress, type CommandStage, type CommandSubject } from "./command_progress";
import { EvidencePanel, EvidenceToggle, revealEvidenceOnTap } from "./thread_evidence";
import { type CommandIssue } from "./command_delivery";
import type { LocalCommands, LocalCommand, PendingCommand } from "./local_commands";
import { useThreadSync } from "./thread_sync";
import { normalizeDeliveryState } from "./client";
import { deliveryOperationLabel } from "./delivery_labels";
import { DispatchReceiptProgress, useDispatchOriginals } from "./delivery_notice";
import { isDeliveryIssue, noticeKey } from "./command_notice_attention";
import type { ThreadDeliveryState } from "./thread_delivery";
import {
  ThreadCommandOutcome,
  type ThreadCommand,
  type ThreadEntity,
} from "../../../history_service/api/history/history_service_pb";
import { Operation } from "../../../proto/command_pb";
import {
  DeliveryState,
  type DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";

type CommandEntity = ThreadEntity & { entity: { case: "command"; value: ThreadCommand } };
export type LocalNotice = (command: LocalCommand) =>
  | {
      acknowledged: boolean;
      hasDispatcherReceipt: boolean;
      progress?: JSX.Element;
      failed?: boolean;
    }
  | undefined;

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
  const issue = errors.get(value.command.commandId);
  if (issue) return issue;
  if (value.destination.kind === "reconcile-only" && !value.admission && !value.dispatch && !value.interruptResult) {
    return { kind: "unconfirmed", message: "Saved by an older app version. Waiting for History evidence." };
  }
  return undefined;
}

function localReceiptReason(receipt: LocalCommand["dispatch"]): string | undefined {
  if (receipt?.reason === "sandbox_deleted" && !receipt.updatedAt && receipt.operation === Operation.UNSPECIFIED) {
    return "Dispatcher did not return a receipt for this command";
  }
  return receipt?.reason;
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
  return (
    <CommandProgress
      stage={stage}
      subject={commandSubject}
      reason={reason === "sandbox_deleted" ? "Sandbox was deleted" : reason}
      local={local}
      unsent={unsent}
    />
  );
}

export function LocalControlCommands({
  commands,
  entities,
  store,
  errors,
  deliver,
  localNotice,
}: {
  commands: LocalCommand[];
  entities: ThreadEntity[];
  store: LocalCommands;
  errors: ReadonlyMap<string, CommandIssue>;
  deliver: (value: LocalCommand) => Promise<void>;
  localNotice: LocalNotice;
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
      localNotice={localNotice}
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
  localNotice,
}: {
  commands: LocalCommand[];
  entities: ThreadEntity[];
  inputEchoes?: ReadonlyMap<string, string>;
  onInputEchoLoaded?: (commandId: string) => void;
  errors: ReadonlyMap<string, CommandIssue>;
  store: LocalCommands;
  deliver: (value: LocalCommand) => Promise<void>;
  localNotice: LocalNotice;
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
        const notice = row ? undefined : localNotice(value);
        if (notice?.acknowledged) return null;
        const outcome = commandRow?.entity.value.outcome ?? null;
        const issue = row ? undefined : commandIssue(value, errors);
        const dispatchState = value.dispatch?.state;
        const dispatchAdmitted =
          dispatchState !== undefined && normalizeDeliveryState(dispatchState) === DeliveryState.RUNNER_ADMITTED;
        const admitted = row !== undefined || value.admission !== null || dispatchAdmitted;
        const canAct =
          (value.attempted || value.destination.kind === "session") &&
          !admitted &&
          !notice?.hasDispatcherReceipt &&
          value.dispatch === null &&
          !value.interruptResult;
        const stage = progressStage(outcome, admitted, value.dispatch, issue);
        const failed =
          notice?.failed ||
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
                <>
                  {notice?.progress ?? (
                    <CommandOutcome
                      stage={stage}
                      subject="input"
                      reason={outcomeReason ?? localReceiptReason(value.dispatch) ?? issue?.message}
                      local
                      unsent={!value.attempted}
                    />
                  )}
                </>
              )
            }
            action={
              canAct
                ? value.attempted
                  ? {
                      label: value.destination.kind === "reconcile-only" ? "Check status" : "Retry",
                      onClick: () => void deliver(value),
                    }
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
  localNotice,
}: {
  rows: ThreadEntity[];
  entities: ThreadEntity[];
  commands: LocalCommand[];
  store: LocalCommands;
  errors: ReadonlyMap<string, CommandIssue>;
  deliver: (value: LocalCommand) => Promise<void>;
  localNotice: LocalNotice;
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
          const notice = localNotice(value);
          if (notice?.acknowledged) return null;
          const operation = value.command.operation;
          const admitted = value.admission !== null;
          const issue = commandIssue(value, errors);
          return (
            <ControlCommandBubble
              key={value.command.commandId}
              id={value.command.commandId}
              description={controlDescription(operation)}
              modelChange={operation.case === "changeModel"}
              progress={
                notice?.progress ?? (
                  <CommandOutcome
                    stage={progressStage(null, admitted, value.dispatch ?? value.interruptResult, issue)}
                    subject={subject(operation.case)}
                    reason={
                      admitted
                        ? undefined
                        : (localReceiptReason(value.dispatch) ?? value.interruptResult?.reason ?? issue?.message)
                    }
                    local
                    unsent={!value.attempted}
                  />
                )
              }
              action={
                (value.attempted || value.destination.kind === "session") &&
                !admitted &&
                !notice?.hasDispatcherReceipt &&
                value.dispatch === null &&
                !value.interruptResult && (
                  <Button
                    size="compact-xs"
                    variant="subtle"
                    onClick={() =>
                      value.attempted ? void deliver(value) : store.cancelUnsent(value.command.commandId)
                    }
                  >
                    {value.attempted
                      ? value.destination.kind === "reconcile-only"
                        ? "Check status"
                        : "Retry"
                      : "Cancel"}
                  </Button>
                )
              }
            />
          );
        })}
    </Stack>
  );
}

function ControlCommandBubble({
  id,
  description,
  modelChange,
  progress,
  action,
  entity,
  children,
}: {
  id: string;
  description: string;
  modelChange: boolean;
  progress?: ReactNode;
  action?: ReactNode;
  entity?: ThreadEntity;
  children?: ReactNode;
}): JSX.Element {
  return (
    <UserMessage
      rowAttributes={{ "data-command-id": id }}
      bubbleAttributes={{
        className: "agentplane-control-command-bubble transcript-inspection-owner",
        "data-has-action": action ? "true" : undefined,
        "data-model-change": modelChange ? "true" : undefined,
        onClick: entity ? revealEvidenceOnTap : undefined,
      }}
      aside={
        progress || action ? (
          <>
            {progress}
            {action}
          </>
        ) : undefined
      }
      overlay={entity && <EvidenceToggle entity={entity} />}
    >
      <VerbatimText text={description} />
      {children}
    </UserMessage>
  );
}

/** The projected control is a chronological entry, whether pending or settled. */
export function ProjectedControlCommand({
  threadId,
  row,
  deliveryProgress,
}: {
  threadId: string;
  row: ThreadEntity;
  deliveryProgress?: JSX.Element | null;
}): JSX.Element | null {
  if (!isCommandEntity(row) || row.entity.value.operation === Operation.SUBMIT_INPUT) return null;
  const command = row.entity.value;
  const description =
    command.operation === Operation.CHANGE_MODEL && command.requestedValue
      ? modelChangeDescription(command.requestedValue)
      : command.operation === Operation.CHANGE_REASONING_EFFORT && command.requestedValue
        ? `Set reasoning effort to ${command.requestedValue}`
        : deliveryOperationLabel(command.operation);
  return (
    <Stack gap="xs">
      <ControlCommandBubble
        id={row.entityId}
        entity={row}
        description={description}
        modelChange={command.operation === Operation.CHANGE_MODEL}
        progress={
          deliveryProgress !== undefined ? (
            deliveryProgress
          ) : (
            <CommandOutcome
              stage={progressStage(command.outcome, true)}
              subject={subject(command.operation)}
              reason={command.outcomeReason}
            />
          )
        }
      >
        {command.inputRef && <Body reference={command.inputRef} format="text" />}
      </ControlCommandBubble>
      <EvidencePanel threadId={threadId} entity={row} />
    </Stack>
  );
}

function modelChangeDescription(model: string): string {
  return `Set model: ${model}`;
}

function controlDescription(operation: PendingCommand["operation"]): string {
  switch (operation.case) {
    case "changeModel":
      return modelChangeDescription(operation.value.model);
    case "changeReasoningEffort":
      return `Set reasoning effort to ${operation.value.effort}`;
    case "interruptTurn":
      return `Interrupt turn ${operation.value.turnId}`;
    case "compact":
      return "Compact conversation";
    default:
      return "Command";
  }
}

export function receiptProgress(delivery: ThreadDeliveryState, item: DispatchWorksetItem): JSX.Element {
  return (
    <DispatchReceiptProgress key={noticeKey(item)} delivery={delivery} item={item} subject={subject(item.operation)} />
  );
}

const MAX_INLINE_ACTIVE = 20;

export function inlineDispatches(delivery: ThreadDeliveryState): DispatchWorksetItem[] {
  const snapshot = delivery.workset.snapshot;
  if (!snapshot) return [];
  return [...snapshot.active.slice(0, MAX_INLINE_ACTIVE), ...snapshot.recentIssues].sort((a, b) => {
    const secondsA = a.updatedAt?.seconds ?? 0n;
    const secondsB = b.updatedAt?.seconds ?? 0n;
    return secondsA === secondsB ? (a.updatedAt?.nanos ?? 0) - (b.updatedAt?.nanos ?? 0) : secondsA < secondsB ? -1 : 1;
  });
}

export function selectedHistoryCommands(entities: readonly ThreadEntity[]): Set<string> {
  return new Set(
    entities.flatMap((row) => {
      if (row.entity.case === "confirmedInput") return row.entity.value.originCommandIds;
      return row.entity.case === "command" && row.entity.value.outcome !== ThreadCommandOutcome.UNSPECIFIED
        ? [row.entityId]
        : [];
    })
  );
}

export function OutstandingDispatchCommands({
  delivery,
  items,
  onOpenDeliveryHistory,
}: {
  delivery: ThreadDeliveryState;
  items: readonly DispatchWorksetItem[];
  onOpenDeliveryHistory: () => void;
}): JSX.Element {
  const { attention, workset } = delivery;
  const visible = items.filter((item) => !isDeliveryIssue(item.state) || !attention.acknowledged(item));
  const originals = useDispatchOriginals(
    delivery.readReceipts,
    visible.map((item) => item.commandId)
  );
  const snapshot = workset.snapshot;
  const olderIssues = snapshot?.moreRecentIssuesAvailable;
  const moreCommands = snapshot && (snapshot.active.length > MAX_INLINE_ACTIVE || snapshot.moreActiveAvailable);
  return (
    <>
      {visible.map((item) => {
        const id = item.commandId;
        const original = originals.commands?.get(id)?.command?.operation;
        const progress = receiptProgress(delivery, item);
        if (item.operation === Operation.SUBMIT_INPUT) {
          const issue = isDeliveryIssue(item.state) || item.state === DeliveryState.CANCELLED_IN_DISPATCHER;
          return (
            <UserInputBubble
              key={id}
              commandId={id}
              text={
                original?.case === "submitInput"
                  ? original.value.text
                  : originals.complete
                    ? "Original input unavailable"
                    : "Loading original input…"
              }
              phase={issue ? "failed" : "pending"}
              pending={!issue}
              progress={progress}
            />
          );
        }
        return (
          <ControlCommandBubble
            key={id}
            id={id}
            description={original ? controlDescription(original) : deliveryOperationLabel(item.operation)}
            modelChange={item.operation === Operation.CHANGE_MODEL}
            progress={progress}
          />
        );
      })}
      {workset.stale && (
        <Text size="xs" c="dimmed" role="status">
          {snapshot
            ? "Showing the last delivery update while reconnecting."
            : "Delivery updates are unavailable while reconnecting."}
        </Text>
      )}
      {workset.coverageLost && (
        <Text size="xs" c="dimmed" role="status">
          Some earlier delivery receipts may have expired.
        </Text>
      )}
      {attention.error && (
        <Text size="xs" role="alert">
          {attention.error}
        </Text>
      )}
      {(olderIssues || moreCommands) && (
        <Button size="compact-xs" variant="subtle" onClick={onOpenDeliveryHistory}>
          {olderIssues ? "Older delivery issues" : "More commands in Delivery history"}
        </Button>
      )}
    </>
  );
}
