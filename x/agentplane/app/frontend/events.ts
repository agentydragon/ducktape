/**
 * Folds the runner's events into what the session view renders. The events are the runner
 * protocol's own (`Event` from protocol.proto); this is a projection for one screen, not a second
 * vocabulary. Every event stays attached to the row it fed — an item, an input, a turn, or the
 * loose pool — so the raw frames can be shown beside what they produced instead of as a separate
 * list.
 */
import { ItemKind, TurnStatus, type Event } from "./protocol_pb";

/**
 * The events one row was built from: the derived events themselves, and the native frames they
 * name in `source_sequences`. Every event the session has seen sits in exactly one row's list, so
 * showing them all hides nothing.
 */
export type Frames = Event[];

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
  frames: Frames;
}

export interface Turn {
  id: string;
  status: TurnStatus | null;
  error: string;
  itemIds: string[];
  frames: Frames;
}

export interface InputState {
  id: string;
  state: "submitted" | "accepted" | "rejected" | "uncertain";
  detail: string;
  /** What was asked; empty for events logged before the runner carried it. */
  text: string;
  /** The turn the harness took it into, once accepted. */
  turnId: string | null;
  frames: Frames;
}

export interface SessionState {
  harness: "running" | "stopped" | "lost" | null;
  turns: Turn[];
  items: Record<string, Item>;
  inputs: InputState[];
  stderr: string[];
  /**
   * Events no row owns: harness lifecycle, stderr, and native frames no derived event has named.
   * A native frame lands here when it arrives and leaves for a row once one cites it.
   */
  looseFrames: Frames;
  /** The wire's decimal string: a uint64 is not safely a JS number. */
  lastSequence: string;
}

export const EMPTY: SessionState = {
  harness: null,
  turns: [],
  items: {},
  inputs: [],
  stderr: [],
  looseFrames: [],
  lastSequence: "0",
};

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
      frames: [],
    }
  );
}

function withItem(state: SessionState, frames: Frames, next: Item): SessionState {
  return { ...state, items: { ...state.items, [next.id]: { ...next, frames: [...next.frames, ...frames] } } };
}

function withInput(state: SessionState, frames: Frames, id: string, update: Partial<InputState>): SessionState {
  const existing = state.inputs.find((input) => input.id === id);
  const inputs = existing
    ? state.inputs.map((input) =>
        input.id === id ? { ...input, ...update, frames: [...input.frames, ...frames] } : input
      )
    : [...state.inputs, { id, state: "submitted" as const, detail: "", text: "", turnId: null, ...update, frames }];
  return { ...state, inputs };
}

function loose(state: SessionState, frames: Frames): SessionState {
  return { ...state, looseFrames: [...state.looseFrames, ...frames] };
}

export function reduce(previous: SessionState, event: Event): SessionState {
  const cited = (frame: Event): boolean => event.sourceSequences.includes(frame.sequence);
  // What the row this event feeds shows: the native frames it came from, which leave the loose
  // pool, then the event itself. The runner cites only the frame it is translating, so a row's
  // frames stay in sequence order.
  const frames = [...previous.looseFrames.filter(cited), event];
  const state: SessionState = {
    ...previous,
    lastSequence: String(event.sequence),
    looseFrames: previous.looseFrames.filter((frame) => !cited(frame)),
  };
  const observation = event.observation;
  switch (observation.case) {
    case "harnessStarted":
      return { ...loose(state, frames), harness: "running" };
    case "harnessExited":
      return { ...loose(state, frames), harness: "stopped" };
    case "harnessLost":
      return { ...loose(state, frames), harness: "lost" };
    case "harnessStderr":
      return { ...loose(state, frames), stderr: [...state.stderr, observation.value.text] };
    case "inputSubmitted":
      return withInput(state, frames, observation.value.inputId, { state: "submitted", text: observation.value.text });
    case "inputAccepted":
      return withInput(state, frames, observation.value.inputId, {
        state: "accepted",
        turnId: observation.value.turnId,
      });
    case "inputRejected":
      return withInput(state, frames, observation.value.inputId, {
        state: "rejected",
        detail: observation.value.reason,
      });
    case "inputUncertain":
      return withInput(state, frames, observation.value.inputId, { state: "uncertain" });
    case "turnStarted":
      return {
        ...state,
        turns: [...state.turns, { id: observation.value.turnId, status: null, error: "", itemIds: [], frames }],
      };
    case "turnCompleted": {
      const { turnId, status, error } = observation.value;
      // A turn the projection never saw start would drop its frames; the pool keeps them instead.
      if (!state.turns.some((turn) => turn.id === turnId)) return loose(state, frames);
      return {
        ...state,
        turns: state.turns.map((turn) =>
          turn.id === turnId ? { ...turn, status, error, frames: [...turn.frames, ...frames] } : turn
        ),
      };
    }
    case "itemStarted": {
      const { itemId, kind, toolName } = observation.value;
      const started = { ...item(state, itemId), kind, toolName };
      const turns = state.turns.map((turn, index) =>
        index === state.turns.length - 1 ? { ...turn, itemIds: [...turn.itemIds, started.id] } : turn
      );
      return withItem({ ...state, turns }, frames, started);
    }
    case "textDelta": {
      const current = item(state, observation.value.itemId);
      return withItem(state, frames, { ...current, text: current.text + observation.value.text });
    }
    case "toolArgumentsDelta": {
      const current = item(state, observation.value.itemId);
      return withItem(state, frames, {
        ...current,
        argumentsJson: current.argumentsJson + observation.value.partialJson,
      });
    }
    case "toolArguments": {
      const current = item(state, observation.value.itemId);
      return withItem(state, frames, { ...current, argumentsJson: observation.value.argumentsJson });
    }
    case "toolOutputDelta": {
      const current = item(state, observation.value.itemId);
      return withItem(state, frames, { ...current, output: current.output + observation.value.text });
    }
    case "itemCompleted": {
      const { itemId, outcome } = observation.value;
      const current = item(state, itemId);
      return withItem(state, frames, {
        ...current,
        completed: true,
        text: outcome.case === "text" ? outcome.value : current.text,
        output: outcome.case === "tool" ? outcome.value.output : current.output,
        succeeded: outcome.case === "tool" ? outcome.value.succeeded : current.succeeded,
      });
    }
    // A native frame no derived event has named yet, and any observation this projection does not
    // model: the pool holds them, so the raw view still shows them.
    default:
      return loose(state, frames);
  }
}
