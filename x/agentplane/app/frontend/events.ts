/**
 * Folds the runner's events into what the session view renders. The events are the runner
 * protocol's own (`Event` from protocol.proto); this is a projection for one screen, not a second
 * vocabulary. It keeps every event and the sequence each item, input and turn began at, so the raw
 * view can lay the whole session out as one stream in sequence order with the items in their
 * places.
 */
import { ItemKind, TurnStatus, type Event } from "./protocol_pb";

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
  firstSequence: bigint;
}

export interface Turn {
  id: string;
  status: TurnStatus | null;
  error: string;
  itemIds: string[];
  firstSequence: bigint;
}

export interface InputState {
  id: string;
  state: "submitted" | "accepted" | "rejected" | "uncertain";
  detail: string;
  /** What was asked; empty for events logged before the runner carried it. */
  text: string;
  /** The turn the harness took it into, once accepted. */
  turnId: string | null;
  firstSequence: bigint;
}

export interface SessionState {
  harness: "running" | "stopped" | "lost" | null;
  turns: Turn[];
  items: Record<string, Item>;
  inputs: InputState[];
  stderr: string[];
  /** Every event the session has produced: what the raw view renders, in `timeline` order. */
  events: Event[];
  /** The wire's decimal string: a uint64 is not safely a JS number. */
  lastSequence: string;
}

export const EMPTY: SessionState = {
  harness: null,
  turns: [],
  items: {},
  inputs: [],
  stderr: [],
  events: [],
  lastSequence: "0",
};

/** What a sequence brought into being, if it brought anything: rendered above that event's frame. */
export type Row = { kind: "turn"; turn: Turn } | { kind: "input"; input: InputState } | { kind: "item"; item: Item };

export interface TimelineStep {
  event: Event;
  row: Row | null;
}

/**
 * The session as one stream in sequence order: every event once, at its own position, carrying the
 * row it started. A row's card therefore sits where the log first mentions it, with the frames that
 * went on filling it following at their own positions — an item's card shows the text it has
 * accumulated by now, which is ahead of the frames below it.
 */
export function timeline(state: SessionState): TimelineStep[] {
  // One event starts at most one row, so a sequence names at most one.
  const rows = new Map<bigint, Row>();
  for (const turn of state.turns) rows.set(turn.firstSequence, { kind: "turn", turn });
  for (const input of state.inputs) rows.set(input.firstSequence, { kind: "input", input });
  for (const item of Object.values(state.items)) rows.set(item.firstSequence, { kind: "item", item });
  return [...state.events]
    .sort((left, right) => (left.sequence === right.sequence ? 0 : left.sequence < right.sequence ? -1 : 1))
    .map((event) => ({ event, row: rows.get(event.sequence) ?? null }));
}

function item(state: SessionState, id: string, firstSequence: bigint): Item {
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
      firstSequence,
    }
  );
}

function withItem(state: SessionState, next: Item): SessionState {
  return { ...state, items: { ...state.items, [next.id]: next } };
}

function withInput(state: SessionState, id: string, firstSequence: bigint, update: Partial<InputState>): SessionState {
  const existing = state.inputs.find((input) => input.id === id);
  const inputs = existing
    ? state.inputs.map((input) => (input.id === id ? { ...input, ...update } : input))
    : [
        ...state.inputs,
        { id, state: "submitted" as const, detail: "", text: "", turnId: null, firstSequence, ...update },
      ];
  return { ...state, inputs };
}

export function reduce(previous: SessionState, event: Event): SessionState {
  const state: SessionState = {
    ...previous,
    lastSequence: String(event.sequence),
    events: [...previous.events, event],
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
    case "inputSubmitted":
      return withInput(state, observation.value.inputId, event.sequence, {
        state: "submitted",
        text: observation.value.text,
      });
    case "inputAccepted":
      return withInput(state, observation.value.inputId, event.sequence, {
        state: "accepted",
        turnId: observation.value.turnId,
      });
    case "inputRejected":
      return withInput(state, observation.value.inputId, event.sequence, {
        state: "rejected",
        detail: observation.value.reason,
      });
    case "inputUncertain":
      return withInput(state, observation.value.inputId, event.sequence, { state: "uncertain" });
    case "turnStarted":
      return {
        ...state,
        turns: [
          ...state.turns,
          { id: observation.value.turnId, status: null, error: "", itemIds: [], firstSequence: event.sequence },
        ],
      };
    case "turnCompleted": {
      const { turnId, status, error } = observation.value;
      return { ...state, turns: state.turns.map((turn) => (turn.id === turnId ? { ...turn, status, error } : turn)) };
    }
    case "itemStarted": {
      const { itemId, kind, toolName } = observation.value;
      const started = { ...item(state, itemId, event.sequence), kind, toolName };
      const turns = state.turns.map((turn, index) =>
        index === state.turns.length - 1 ? { ...turn, itemIds: [...turn.itemIds, started.id] } : turn
      );
      return { ...withItem(state, started), turns };
    }
    case "textDelta": {
      const current = item(state, observation.value.itemId, event.sequence);
      return withItem(state, { ...current, text: current.text + observation.value.text });
    }
    case "toolArgumentsDelta": {
      const current = item(state, observation.value.itemId, event.sequence);
      return withItem(state, { ...current, argumentsJson: current.argumentsJson + observation.value.partialJson });
    }
    case "toolArguments": {
      const current = item(state, observation.value.itemId, event.sequence);
      return withItem(state, { ...current, argumentsJson: observation.value.argumentsJson });
    }
    case "toolOutputDelta": {
      const current = item(state, observation.value.itemId, event.sequence);
      return withItem(state, { ...current, output: current.output + observation.value.text });
    }
    case "itemCompleted": {
      const { itemId, outcome } = observation.value;
      const current = item(state, itemId, event.sequence);
      return withItem(state, {
        ...current,
        completed: true,
        text: outcome.case === "text" ? outcome.value : current.text,
        output: outcome.case === "tool" ? outcome.value.output : current.output,
        succeeded: outcome.case === "tool" ? outcome.value.succeeded : current.succeeded,
      });
    }
    // A Native frame, and any observation this projection does not model: it renders in the raw
    // stream at its own position like every other event, and changes nothing else.
    default:
      return state;
  }
}
