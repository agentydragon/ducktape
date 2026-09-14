/**
 * Folds shared EventEntries into what the session view renders. This is a projection for one
 * screen, not a second vocabulary. It keeps every entry and the cursor each item, input and turn
 * began at, so the raw view can lay the whole session out as one stream in cursor order with the items in their
 * places.
 */
import { ItemKind, TurnStatus, type Event } from "./x/agentplane/protocol/event_pb";
import type { EventEntry } from "./x/agentplane/protocol/event_log_pb";

export interface Item {
  id: string;
  kind: ItemKind;
  toolName: string;
  /** Streamed text or reasoning, or a tool call's streamed arguments. */
  text: string;
  argumentsJson: string;
  output: string;
  completed: boolean;
  succeeded: boolean | null;
  firstCursor: bigint;
}

export interface Turn {
  id: string;
  status: TurnStatus | null;
  error: string;
  itemIds: string[];
  firstCursor: bigint;
}

export interface InputState {
  id: string;
  state: "confirmed" | "failed";
  detail: string;
  /** The exact native harness message, not an app-owned pending command. */
  text: string;
  /** The turn the harness took it into. */
  turnId: string | null;
  firstCursor: bigint;
}

export interface SessionState {
  harness: "running" | "stopped" | "lost" | null;
  turns: Turn[];
  items: Record<string, Item>;
  inputs: InputState[];
  stderr: string[];
  /** Every source entry the session has produced: what the raw view renders, in `timeline` order. */
  entries: EventEntry[];
  /** The wire's decimal string: a uint64 is not safely a JS number. */
  lastCursor: string;
}

export const EMPTY: SessionState = {
  harness: null,
  turns: [],
  items: {},
  inputs: [],
  stderr: [],
  entries: [],
  lastCursor: "0",
};

/** EventEntry's Event is mandatory in the protocol contract but optional in generated TypeScript. */
export function eventOf(entry: EventEntry): Event {
  if (entry.event === undefined) throw new Error(`event entry ${entry.cursor} has no event`);
  return entry.event;
}

/** What a cursor brought into being, if it brought anything: rendered above that entry's frame. */
export type Row = { kind: "turn"; turn: Turn } | { kind: "input"; input: InputState } | { kind: "item"; item: Item };

export interface TimelineStep {
  entry: EventEntry;
  row: Row | null;
}

/**
 * The session as one stream in cursor order: every entry once, at its own position, carrying the
 * row it started. A row's card therefore sits where the log first mentions it, with the frames that
 * went on filling it following at their own positions — an item's card shows the text it has
 * accumulated by now, which is ahead of the frames below it.
 */
export function timeline(state: SessionState): TimelineStep[] {
  // One entry starts at most one row, so a cursor names at most one.
  const rows = new Map<bigint, Row>();
  for (const turn of state.turns) rows.set(turn.firstCursor, { kind: "turn", turn });
  for (const input of state.inputs) rows.set(input.firstCursor, { kind: "input", input });
  for (const item of Object.values(state.items)) rows.set(item.firstCursor, { kind: "item", item });
  return [...state.entries]
    .sort((left, right) => (left.cursor === right.cursor ? 0 : left.cursor < right.cursor ? -1 : 1))
    .map((entry) => ({ entry, row: rows.get(entry.cursor) ?? null }));
}

/** A turn's items, tool calls and reasoning steps run together into collapsible groups; anything
 * else (an assistant's answer) stands alone and ends the run before it. */
export type ItemGroup = { kind: "single"; item: Item } | { kind: "run"; items: Item[] };

const COLLAPSIBLE: ReadonlySet<ItemKind> = new Set([ItemKind.TOOL_CALL, ItemKind.REASONING]);

export function groupItems(items: Item[]): ItemGroup[] {
  const groups: ItemGroup[] = [];
  for (const current of items) {
    const last = groups.at(-1);
    if (!COLLAPSIBLE.has(current.kind)) {
      groups.push({ kind: "single", item: current });
    } else if (last?.kind === "run") {
      last.items.push(current);
    } else {
      groups.push({ kind: "run", items: [current] });
    }
  }
  return groups;
}

function item(state: SessionState, id: string, firstCursor: bigint): Item {
  return (
    state.items[id] ?? {
      id,
      kind: ItemKind.UNSPECIFIED,
      toolName: "",
      text: "",
      argumentsJson: "",
      output: "",
      completed: false,
      succeeded: null,
      firstCursor,
    }
  );
}

function withItem(state: SessionState, next: Item): SessionState {
  return { ...state, items: { ...state.items, [next.id]: next } };
}

function withInput(state: SessionState, id: string, firstCursor: bigint, update: Partial<InputState>): SessionState {
  const existing = state.inputs.find((input) => input.id === id);
  const inputs = existing
    ? state.inputs.map((input) => (input.id === id ? { ...input, ...update } : input))
    : [
        ...state.inputs,
        { id, state: "confirmed" as const, detail: "", text: "", turnId: null, firstCursor, ...update },
      ];
  return { ...state, inputs };
}

export function reduce(previous: SessionState, entry: EventEntry): SessionState {
  const event = eventOf(entry);
  const state: SessionState = {
    ...previous,
    lastCursor: String(entry.cursor),
    entries: [...previous.entries, entry],
  };
  const observation = event.observation;
  switch (observation.case) {
    case "harnessStarted":
      return { ...state, harness: "running" };
    case "harnessExited":
      return { ...state, harness: "stopped" };
    case "harnessLost":
      return { ...state, harness: "lost" };
    case "harnessStderr":
      return { ...state, stderr: [...state.stderr, observation.value.text] };
    case "harnessUserMessageConfirmed":
      return withInput(state, observation.value.harnessMessageId, entry.cursor, {
        state: "confirmed",
        text: observation.value.text,
        turnId: observation.value.turnId,
        detail: `from ${observation.value.originCommandIds.join(", ")}`,
      });
    case "commandFailed":
      return withInput(state, observation.value.commandId, entry.cursor, {
        state: "failed",
        detail: observation.value.reason,
      });
    case "turnStarted":
      return {
        ...state,
        turns: [
          ...state.turns,
          { id: observation.value.turnId, status: null, error: "", itemIds: [], firstCursor: entry.cursor },
        ],
      };
    case "turnCompleted": {
      const { turnId, status, error } = observation.value;
      return { ...state, turns: state.turns.map((turn) => (turn.id === turnId ? { ...turn, status, error } : turn)) };
    }
    case "itemStarted": {
      const { itemId, kind, toolName } = observation.value;
      const started = { ...item(state, itemId, entry.cursor), kind, toolName };
      const turns = state.turns.map((turn, index) =>
        index === state.turns.length - 1 ? { ...turn, itemIds: [...turn.itemIds, started.id] } : turn
      );
      return { ...withItem(state, started), turns };
    }
    case "textDelta": {
      const current = item(state, observation.value.itemId, entry.cursor);
      return withItem(state, { ...current, text: current.text + observation.value.text });
    }
    case "toolArgumentsDelta": {
      const current = item(state, observation.value.itemId, entry.cursor);
      return withItem(state, { ...current, argumentsJson: current.argumentsJson + observation.value.partialJson });
    }
    case "toolArguments": {
      const current = item(state, observation.value.itemId, entry.cursor);
      return withItem(state, { ...current, argumentsJson: observation.value.argumentsJson });
    }
    case "toolOutputDelta": {
      const current = item(state, observation.value.itemId, entry.cursor);
      return withItem(state, { ...current, output: current.output + observation.value.text });
    }
    case "itemCompleted": {
      const { itemId, outcome } = observation.value;
      const current = item(state, itemId, entry.cursor);
      return withItem(state, {
        ...current,
        completed: true,
        text: outcome.case === "text" ? outcome.value : current.text,
        output: outcome.case === "tool" ? outcome.value.output : current.output,
        succeeded: outcome.case === "tool" ? outcome.value.succeeded : current.succeeded,
      });
    }
    // A Native frame, command admission, and any observation this projection does not model: it
    // renders in raw mode at its own cursor like every other entry, and changes nothing else.
    default:
      return state;
  }
}
