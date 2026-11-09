/**
 * The shape of a pushed view: a page renders the last snapshot its stream delivered, and the honesty
 * that goes with it. A stream that goes quiet looks exactly like nothing happening, so neither is
 * assumed: the stream's connection and the watch's health both go to the app's one connection
 * indicator (`stream_status.tsx`).
 */
import { type StreamStatus } from "./stream_status";

export type WatchHealth = {
  /** Whether the watch has synced and no watched kind is failing. */
  healthy: boolean;
  /** Each failing kind, with its error and since when (RFC 3339). */
  failing: Record<string, { error: string; since: string }>;
};
export interface Live<T> {
  /** The last snapshot, or null until the first frame arrives. */
  snapshot: T | null;
  /** The watch's health, from the last frame of either kind. */
  health: WatchHealth | null;
  stream: StreamStatus;
}
