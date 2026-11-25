/**
 * The thread history's rows. Consecutive tool calls and reasoning steps fold into one run; adjacent
 * reasoning steps inside a run also stay together as a nested group. Consecutive mundane lifecycle
 * observations (turn started, harness started, an ordinary turn completion, ...) fold into one
 * group, so neither a long chain of intermediate steps nor routine housekeeping buries the entities
 * around it. Any other entity (an assistant's answer, confirmed input, a lifecycle observation worth
 * its own line) stands alone and ends whichever it interrupts.
 */
import { ItemKind, TurnStatus, type Event } from "../../../proto/event_pb";
import type { ThreadEntity } from "../../../history_service/api/history/history_service_pb";

/** A row is identified by its first entity: its key and reading anchor stay put while later steps
 * stream into a run or group. */
export type HistoryRow =
  | { kind: "entity"; entities: [ThreadEntity] }
  | { kind: "run"; entities: [ThreadEntity, ...ThreadEntity[]]; contents: RunContent[] }
  | { kind: "setup"; entities: [ThreadEntity, ...ThreadEntity[]] }
  | { kind: "lifecycle_group"; entities: [ThreadEntity, ...ThreadEntity[]] };

/** Ordered content of a run: ordinary tool items and consecutive reasoning items kept together. */
export type RunContent =
  { kind: "item"; entity: ThreadEntity } | { kind: "reasoning_group"; entities: [ThreadEntity, ...ThreadEntity[]] };

const SETUP_OBSERVATIONS = new Set(["setup_started", "setup_output", "setup_finished", "setup_interrupted"]);

function lifecycleCase(entity: ThreadEntity): string | null {
  if (entity.entity.case !== "lifecycle") return null;
  const observation = entity.entity.value.event?.observation;
  return observation?.case?.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`) ?? null;
}

function setupStep(entity: ThreadEntity): boolean {
  const observation = lifecycleCase(entity);
  return observation !== null && SETUP_OBSERVATIONS.has(observation);
}

export function summarizeSetup(entities: readonly ThreadEntity[]): string {
  const terminal = [...entities]
    .reverse()
    .find((entity) => ["setup_finished", "setup_interrupted"].includes(lifecycleCase(entity) ?? ""));
  if (!terminal || terminal.entity.case !== "lifecycle") return "Thread setup running";
  const parsed = terminal.entity.value.event?.observation;
  if (!parsed) return "Thread setup running";
  if (parsed.case === "setupInterrupted") return "Thread setup interrupted";
  if (parsed.case === "setupFinished")
    return parsed.value.exitCode === 0
      ? "Thread setup complete"
      : `Thread setup failed (exit ${parsed.value.exitCode})`;
  return "Thread setup running";
}

/** Recent display lines only. The entities remain the unmodified, expandable raw evidence. */
export function setupProgress(entities: readonly ThreadEntity[]): string[] {
  const recent: { text: string; order: number }[] = [];
  const streams = {
    stdout: { decoder: new TextDecoder(), line: "", pendingReturn: false, order: 0 },
    stderr: { decoder: new TextDecoder(), line: "", pendingReturn: false, order: 0 },
  };
  let order = 0;
  const complete = (stream: (typeof streams)["stdout"]) => {
    const text = stream.line.trim();
    if (text) {
      recent.push({ text, order: ++order });
      if (recent.length > 3) recent.shift();
    }
    stream.line = "";
  };
  const write = (stream: (typeof streams)["stdout"], text: string) => {
    for (const char of text) {
      if (stream.pendingReturn) {
        stream.pendingReturn = false;
        if (char === "\n") {
          complete(stream); // CRLF is one newline; a bare CR overwrites the current line.
          continue;
        }
        stream.line = "";
      }
      if (char === "\r") stream.pendingReturn = true;
      else if (char === "\n") complete(stream);
      else {
        stream.line += char;
        stream.line = stream.line.slice(-240); // Bound the preview, never the retained output.
        stream.order = ++order;
      }
    }
  };
  let terminal = false;
  for (const entity of entities) {
    if (!setupStep(entity) || entity.entity.case !== "lifecycle") continue;
    const observation = entity.entity.value.event?.observation;
    if (!observation) continue;
    if (observation.case === "setupOutput") {
      const { case: channel, value } = observation.value.stream;
      if (channel === "stdout" || channel === "stderr") {
        write(streams[channel], streams[channel].decoder.decode(value, { stream: true }));
      }
    } else if (observation.case === "setupFinished" || observation.case === "setupInterrupted") {
      terminal = true;
    }
  }
  if (terminal) for (const stream of Object.values(streams)) write(stream, stream.decoder.decode());
  return [
    ...recent,
    ...Object.values(streams)
      .filter((stream) => stream.line.trim())
      .map((stream) => ({ text: stream.line.trim(), order: stream.order })),
  ]
    .sort((a, b) => a.order - b.order)
    .slice(-3)
    .map(({ text }) => text);
}

function itemKind(entity: ThreadEntity): ItemKind | null {
  return entity.entity.case === "item" ? entity.entity.value.kind : null;
}

function runStep(entity: ThreadEntity): boolean {
  const kind = itemKind(entity);
  return kind === ItemKind.TOOL_CALL || kind === ItemKind.REASONING;
}

function appendRunContent(contents: RunContent[], entity: ThreadEntity): void {
  if (itemKind(entity) !== ItemKind.REASONING) {
    contents.push({ kind: "item", entity });
    return;
  }
  const last = contents.at(-1);
  if (last?.kind === "reasoning_group") last.entities.push(entity);
  else contents.push({ kind: "reasoning_group", entities: [entity] });
}

// An interrupt is usually the operator's own doing, so an interrupted turn reads as ordinary. What is
// prominent here is also what marks an idle thread as `turn_error` (turnErrorLabel).
const TURN_OUTCOMES: Record<TurnStatus, { label: string; prominent: boolean }> = {
  [TurnStatus.UNSPECIFIED]: { label: "Turn ended without a status", prominent: true },
  [TurnStatus.COMPLETED]: { label: "Turn completed", prominent: false },
  [TurnStatus.INTERRUPTED]: { label: "Turn interrupted", prominent: false },
  [TurnStatus.FAILED]: { label: "Turn failed", prominent: true },
  [TurnStatus.PROCESS_LOST]: { label: "Turn lost", prominent: true },
};

export type TurnErrorLabel = { kind: "error"; label: string } | { kind: "ordinary" } | { kind: "unrecognised" };

/** Whether a generated TurnStatus is prominent or ordinary; unknown numeric values remain unrecognised. */
export function turnErrorLabel(status: TurnStatus): TurnErrorLabel {
  const outcome = TURN_OUTCOMES[status];
  if (!outcome) return { kind: "unrecognised" };
  return outcome.prominent ? { kind: "error", label: outcome.label } : { kind: "ordinary" };
}

export interface LifecyclePresentation {
  label: string;
  /** A prominent observation is an alert on its own row; any other reads as dimmed text and groups
   * with its mundane neighbors. */
  prominent: boolean;
  diagnostic: string | null;
}

export function lifecyclePresentation(event: Event): LifecyclePresentation {
  const parsed = event.observation;
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
    case "harnessLaunchFailed":
      return { label: "Harness launch failed", prominent: true, diagnostic: parsed.value.reason };
    case "setupStarted":
      return { label: "Setup started", prominent: false, diagnostic: null };
    case "setupOutput": {
      const stream = parsed.value.stream;
      return {
        label: stream.case === "stderr" ? "Setup stderr" : "Setup stdout",
        prominent: false,
        diagnostic: stream.value ? new TextDecoder().decode(stream.value) : null,
      };
    }
    case "setupFinished":
      return {
        label: parsed.value.exitCode === 0 ? "Setup completed" : `Setup failed (exit ${parsed.value.exitCode})`,
        prominent: parsed.value.exitCode !== 0,
        diagnostic: null,
      };
    case "setupInterrupted":
      return { label: "Setup interrupted", prominent: true, diagnostic: null };
    default:
      return {
        label: (parsed.case ?? "Unknown lifecycle event").replace(/[A-Z]/g, (letter) => ` ${letter.toLowerCase()}`),
        prominent: false,
        diagnostic: null,
      };
  }
}

/** A lifecycle entity plain enough to fold into a group: no alert to show and nothing beyond its
 * one-line label. */
function groupableLifecycle(entity: ThreadEntity): boolean {
  if (entity.entity.case !== "lifecycle" || !entity.entity.value.event) return false;
  const { prominent, diagnostic } = lifecyclePresentation(entity.entity.value.event);
  return !prominent && diagnostic === null;
}

/** `segments` in cursor order. */
export function historyRows(segments: readonly ThreadEntity[]): HistoryRow[] {
  const rows: HistoryRow[] = [];
  for (const entity of segments) {
    const last = rows.at(-1);
    if (setupStep(entity)) {
      if (last?.kind === "setup") last.entities.push(entity);
      else rows.push({ kind: "setup", entities: [entity] });
    } else if (runStep(entity)) {
      if (last?.kind === "run") {
        last.entities.push(entity);
        appendRunContent(last.contents, entity);
      } else {
        const contents: RunContent[] = [];
        appendRunContent(contents, entity);
        rows.push({ kind: "run", entities: [entity], contents });
      }
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
  return `${row.entities[0].entity.case}:${row.entities[0].entityId}`;
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
      entity.entity.case === "lifecycle" && entity.entity.value.event
        ? lifecyclePresentation(entity.entity.value.event).label
        : ""
    )
    .join(", ");
}
