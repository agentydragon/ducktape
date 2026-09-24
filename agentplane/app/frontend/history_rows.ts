/**
 * The thread history's rows. Consecutive tool calls and reasoning steps fold into one run, so a
 * long chain of intermediate steps does not bury the answer after it; any other entity (an
 * assistant's answer, confirmed input, a lifecycle observation) stands alone and ends the run
 * before it.
 */
import { ItemKind } from "../../protocol/event_pb";
import type { ThreadEntity } from "./thread_sync";

/** A row is identified by its first entity: its key and reading anchor stay put while later steps
 * stream into a run. */
export type HistoryRow =
  { kind: "entity"; entities: [ThreadEntity] } | { kind: "run"; entities: [ThreadEntity, ...ThreadEntity[]] };

function itemKind(entity: ThreadEntity): ItemKind | null {
  return entity.entityKind === "item" && "kind" in entity.state ? entity.state.kind : null;
}

function runStep(entity: ThreadEntity): boolean {
  const kind = itemKind(entity);
  return kind === ItemKind.TOOL_CALL || kind === ItemKind.REASONING;
}

/** `segments` in cursor order. */
export function historyRows(segments: readonly ThreadEntity[]): HistoryRow[] {
  const rows: HistoryRow[] = [];
  for (const entity of segments) {
    const last = rows.at(-1);
    if (!runStep(entity)) rows.push({ kind: "entity", entities: [entity] });
    else if (last?.kind === "run") last.entities.push(entity);
    else rows.push({ kind: "run", entities: [entity] });
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
