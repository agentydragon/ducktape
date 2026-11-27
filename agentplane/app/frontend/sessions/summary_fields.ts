import type { SessionSummary } from "../../../history_service/api/history/history_service_pb";

/**
 * The `SessionSummary` fields the Thread views read, as proto field paths. The Threads watch asks History for
 * exactly these (`SelectedSessionSummaries.field_mask`), so a frame carries them and nothing else. Add a path
 * here with the first read of a new field; `ThreadSummary` keeps its top-level keys in step.
 */
export const THREAD_SUMMARY_FIELDS = [
  "last_event_at",
  "last_model_activity_at",
  "idle_since",
  "last_turn_status",
  "projection_epoch",
  "sandbox",
  "attached.spec.model",
  "attached.spec.reasoning_effort",
  "attached.spec.placement",
  "attached.historical_harness",
] as const;

/** A summary as the Thread views see it: reading a top-level field outside the set above is a type error. */
export type ThreadSummary = Pick<
  SessionSummary,
  | "sessionId"
  | "lastEventAt"
  | "lastModelActivityAt"
  | "idleSince"
  | "lastTurnStatus"
  | "projectionEpoch"
  | "sandbox"
  | "attached"
>;
