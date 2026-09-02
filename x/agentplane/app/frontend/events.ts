/**
 * Folds the runner's events into what the session view renders. The events are the runner
 * protocol's own (`Event` from the generated schema); this is a projection for one screen, not a
 * second vocabulary, and the raw events stay available beside it.
 */
import type { Event } from "./client";

export interface Item {
  id: string;
  kind: string;
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
  status: string | null;
  error: string;
  itemIds: string[];
}

export interface InputState {
  id: string;
  state: "submitted" | "accepted" | "rejected" | "uncertain";
  detail: string;
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
      kind: "",
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
    : [...state.inputs, { id, state: "submitted" as const, detail: "", ...update }];
  return { ...state, inputs };
}

export function reduce(previous: SessionState, event: Event): SessionState {
  const state: SessionState = { ...previous, lastSequence: event.sequence ?? "0" };
  if (event.harnessStarted) return { ...state, harness: "running" };
  if (event.harnessExited) return { ...state, harness: "stopped" };
  if (event.harnessLost) return { ...state, harness: "lost" };
  if (event.harnessStderr) return { ...state, stderr: [...state.stderr, event.harnessStderr.text ?? ""] };
  if (event.inputSubmitted) return withInput(state, event.inputSubmitted.inputId ?? "", { state: "submitted" });
  if (event.inputAccepted) return withInput(state, event.inputAccepted.inputId ?? "", { state: "accepted" });
  if (event.inputRejected)
    return withInput(state, event.inputRejected.inputId ?? "", {
      state: "rejected",
      detail: event.inputRejected.reason ?? "",
    });
  if (event.inputUncertain) return withInput(state, event.inputUncertain.inputId ?? "", { state: "uncertain" });
  if (event.turnStarted)
    return {
      ...state,
      turns: [...state.turns, { id: event.turnStarted.turnId ?? "", status: null, error: "", itemIds: [] }],
    };
  if (event.turnCompleted) {
    const { turnId, status, error } = event.turnCompleted;
    return {
      ...state,
      turns: state.turns.map((turn) =>
        turn.id === turnId ? { ...turn, status: status ?? null, error: error ?? "" } : turn
      ),
    };
  }
  if (event.itemStarted) {
    const { itemId, kind, toolName } = event.itemStarted;
    const started = { ...item(state, itemId ?? ""), kind: kind ?? "", toolName: toolName ?? "" };
    const turns = state.turns.map((turn, index) =>
      index === state.turns.length - 1 ? { ...turn, itemIds: [...turn.itemIds, started.id] } : turn
    );
    return { ...withItem(state, started), turns };
  }
  if (event.textDelta) {
    const current = item(state, event.textDelta.itemId ?? "");
    return withItem(state, { ...current, text: current.text + (event.textDelta.text ?? "") });
  }
  if (event.toolArgumentsDelta) {
    const current = item(state, event.toolArgumentsDelta.itemId ?? "");
    return withItem(state, {
      ...current,
      argumentsJson: current.argumentsJson + (event.toolArgumentsDelta.partialJson ?? ""),
    });
  }
  if (event.toolArguments) {
    const current = item(state, event.toolArguments.itemId ?? "");
    return withItem(state, { ...current, argumentsJson: event.toolArguments.argumentsJson ?? "" });
  }
  if (event.toolOutputDelta) {
    const current = item(state, event.toolOutputDelta.itemId ?? "");
    return withItem(state, { ...current, output: current.output + (event.toolOutputDelta.text ?? "") });
  }
  if (event.itemCompleted) {
    const { itemId, text, tool } = event.itemCompleted;
    const current = item(state, itemId ?? "");
    return withItem(state, {
      ...current,
      completed: true,
      text: text ?? current.text,
      output: tool?.output ?? current.output,
      succeeded: tool ? (tool.succeeded ?? false) : current.succeeded,
    });
  }
  return state;
}
