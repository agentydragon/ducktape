/**
 * The thread history's rows. Consecutive tool calls and reasoning steps fold into one run, and
 * consecutive mundane lifecycle observations (turn started, harness started, an ordinary turn
 * completion, ...) fold into one group, so neither a long chain of intermediate steps nor routine
 * housekeeping buries the entities around it. Any other entity (an assistant's answer, confirmed
 * input, a lifecycle observation worth its own line) stands alone and ends whichever it interrupts.
 */
import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import { EventSchema, ItemKind, TurnStatus } from "../../../protocol/event_pb";
import type { ThreadEntity } from "./thread_sync";

/** A row is identified by its first entity: its key and reading anchor stay put while later steps
 * stream into a run or group. */
export type HistoryRow =
  | { kind: "entity"; entities: [ThreadEntity] }
  | { kind: "run"; entities: [ThreadEntity, ...ThreadEntity[]] }
  | { kind: "lifecycle_group"; entities: [ThreadEntity, ...ThreadEntity[]] };

function itemKind(entity: ThreadEntity): ItemKind | null {
  return entity.entityKind === "item" && "kind" in entity.state ? entity.state.kind : null;
}

function runStep(entity: ThreadEntity): boolean {
  const kind = itemKind(entity);
  return kind === ItemKind.TOOL_CALL || kind === ItemKind.REASONING;
}

// An interrupt is usually the operator's own doing, so an interrupted turn reads as ordinary.
const TURN_OUTCOMES: Record<TurnStatus, { label: string; prominent: boolean }> = {
  [TurnStatus.UNSPECIFIED]: { label: "Turn ended without a status", prominent: true },
  [TurnStatus.COMPLETED]: { label: "Turn completed", prominent: false },
  [TurnStatus.INTERRUPTED]: { label: "Turn interrupted", prominent: false },
  [TurnStatus.FAILED]: { label: "Turn failed", prominent: true },
  [TurnStatus.PROCESS_LOST]: { label: "Turn lost", prominent: true },
};

export interface LifecyclePresentation {
  label: string;
  /** A prominent observation is an alert on its own row; any other reads as dimmed text and groups
   * with its mundane neighbors. */
  prominent: boolean;
  diagnostic: string | null;
}

export function lifecyclePresentation(observation: string, event: unknown): LifecyclePresentation {
  const parsed = fromJson(EventSchema, event as JsonValue).observation;
  switch (parsed.case) {
    case "turnStarted":
      return { label: "Turn started", prominent: false, diagnostic: null };
    case "turnCompleted": {
      const { status, error } = parsed.value;
      const diagnostic = error || (status === TurnStatus.FAILED ? "The harness reported no error details." : null);
      return { ...TURN_OUTCOMES[status], diagnostic };
    }
    case "modelChanged":
      return { label: `Model changed to ${parsed.value.model}`, prominent: false, diagnostic: null };
    case "reasoningEffortChanged":
      return { label: `Reasoning effort changed to ${parsed.value.effort}`, prominent: false, diagnostic: null };
    case "harnessStarted":
      return { label: "Harness started", prominent: false, diagnostic: null };
    case "harnessExited":
      return {
        label: parsed.value.exitCode ? `Harness exited with code ${parsed.value.exitCode}` : "Harness exited",
        prominent: false,
        diagnostic: null,
      };
    case "harnessLost":
      return { label: "Harness lost", prominent: true, diagnostic: null };
    default:
      return { label: observation.replaceAll("_", " "), prominent: false, diagnostic: null };
  }
}

/** A lifecycle entity plain enough to fold into a group: no alert to show and nothing beyond its
 * one-line label. */
function groupableLifecycle(entity: ThreadEntity): boolean {
  if (entity.entityKind !== "lifecycle" || !("observation" in entity.state)) return false;
  const { prominent, diagnostic } = lifecyclePresentation(entity.state.observation, entity.state.event);
  return !prominent && diagnostic === null;
}

/** `segments` in cursor order. */
export function historyRows(segments: readonly ThreadEntity[]): HistoryRow[] {
  const rows: HistoryRow[] = [];
  for (const entity of segments) {
    const last = rows.at(-1);
    if (runStep(entity)) {
      if (last?.kind === "run") last.entities.push(entity);
      else rows.push({ kind: "run", entities: [entity] });
    } else if (groupableLifecycle(entity)) {
      if (last?.kind === "lifecycle_group") last.entities.push(entity);
      else rows.push({ kind: "lifecycle_group", entities: [entity] });
    } else {
      rows.push({ kind: "entity", entities: [entity] });
    }
  }
  return rows;
}

export function rowKey(row: HistoryRow): string {
  return `${row.entities[0].entityKind}:${row.entities[0].entityId}`;
}

/** "12 tool calls, 3 reasoning steps": each kind counted separately, so a mixed run reads at a
 * glance without claiming a false total. */
export function summarizeRun(entities: readonly ThreadEntity[]): string {
  const toolCalls = entities.filter((entity) => itemKind(entity) === ItemKind.TOOL_CALL).length;
  const reasoning = entities.filter((entity) => itemKind(entity) === ItemKind.REASONING).length;
  const parts: string[] = [];
  if (toolCalls > 0) parts.push(`${toolCalls} tool call${toolCalls === 1 ? "" : "s"}`);
  if (reasoning > 0) parts.push(`${reasoning} reasoning step${reasoning === 1 ? "" : "s"}`);
  return parts.join(", ");
}

/** "Turn started, Harness started, Turn completed": each grouped entity's own label, comma-joined
 * in the order they happened. */
export function summarizeLifecycleGroup(entities: readonly ThreadEntity[]): string {
  return entities
    .map((entity) =>
      entity.entityKind === "lifecycle" && "observation" in entity.state
        ? lifecyclePresentation(entity.state.observation, entity.state.event).label
        : ""
    )
    .join(", ");
}
