/**
 * Folds the runner's events into what the session view renders. The events are the runner
 * protocol's own (`Event` from protocol.proto); this is a projection for one screen, not a second
 * vocabulary, and the raw events stay available beside it.
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
}

export interface Turn {
  id: string;
  status: TurnStatus | null;
  error: string;
  itemIds: string[];
}

export interface InputState {
  id: string;
  state: "submitted" | "accepted" | "rejected" | "uncertain";
  detail: string;
  /** What was asked; empty for events logged before the runner carried it. */
  text: string;
  /** The turn the harness took it into, once accepted. */
  turnId: string | null;
}

export interface SessionState {
  harness: "running" | "stopped" | "lost" | null;
  turns: Turn[];
  items: Record<string, Item>;
  inputs: InputState[];
  stderr: string[];
  /** The wire's decimal string: a uint64 is not safely a JS number. */
  lastSequence: string;
}

export const EMPTY: SessionState = { harness: null, turns: [], items: {}, inputs: [], stderr: [], lastSequence: "0" };

function item(state: SessionState, id: string): Item {
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
    }
  );
}

function withItem(state: SessionState, next: Item): SessionState {
  return { ...state, items: { ...state.items, [next.id]: next } };
}

function withInput(state: SessionState, id: string, update: Partial<InputState>): SessionState {
  const existing = state.inputs.find((input) => input.id === id);
  const inputs = existing
    ? state.inputs.map((input) => (input.id === id ? { ...input, ...update } : input))
    : [...state.inputs, { id, state: "submitted" as const, detail: "", text: "", turnId: null, ...update }];
  return { ...state, inputs };
}

export function reduce(previous: SessionState, event: Event): SessionState {
  const state: SessionState = { ...previous, lastSequence: String(event.sequence) };
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
      return withInput(state, observation.value.inputId, { state: "submitted", text: observation.value.text });
    case "inputAccepted":
      return withInput(state, observation.value.inputId, { state: "accepted", turnId: observation.value.turnId });
    case "inputRejected":
      return withInput(state, observation.value.inputId, { state: "rejected", detail: observation.value.reason });
    case "inputUncertain":
      return withInput(state, observation.value.inputId, { state: "uncertain" });
    case "turnStarted":
      return {
        ...state,
        turns: [...state.turns, { id: observation.value.turnId, status: null, error: "", itemIds: [] }],
      };
    case "turnCompleted": {
      const { turnId, status, error } = observation.value;
      return { ...state, turns: state.turns.map((turn) => (turn.id === turnId ? { ...turn, status, error } : turn)) };
    }
    case "itemStarted": {
      const { itemId, kind, toolName } = observation.value;
      const started = { ...item(state, itemId), kind, toolName };
      const turns = state.turns.map((turn, index) =>
        index === state.turns.length - 1 ? { ...turn, itemIds: [...turn.itemIds, started.id] } : turn
      );
      return { ...withItem(state, started), turns };
    }
    case "textDelta": {
      const current = item(state, observation.value.itemId);
      return withItem(state, { ...current, text: current.text + observation.value.text });
    }
    case "toolArgumentsDelta": {
      const current = item(state, observation.value.itemId);
      return withItem(state, { ...current, argumentsJson: current.argumentsJson + observation.value.partialJson });
    }
    case "toolArguments": {
      const current = item(state, observation.value.itemId);
      return withItem(state, { ...current, argumentsJson: observation.value.argumentsJson });
    }
    case "toolOutputDelta": {
      const current = item(state, observation.value.itemId);
      return withItem(state, { ...current, output: current.output + observation.value.text });
    }
    case "itemCompleted": {
      const { itemId, outcome } = observation.value;
      const current = item(state, itemId);
      return withItem(state, {
        ...current,
        completed: true,
        text: outcome.case === "text" ? outcome.value : current.text,
        output: outcome.case === "tool" ? outcome.value.output : current.output,
        succeeded: outcome.case === "tool" ? outcome.value.succeeded : current.succeeded,
      });
    }
    default:
      return state;
  }
}
