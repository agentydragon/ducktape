/**
 * What the thread view reads of a thread, whatever keeps it in sync: the thread's rows, the rows of
 * commands this client sent, and bodies by reference, each kept current. The app root provides the
 * implementation through `ThreadSyncContext`.
 */
import { createContext, useContext, type Context, type JSX, type ReactNode } from "react";

import type { StreamConnection } from "../live_stream";
import type { zThreadEntityView } from "./thread_entity_view.zod";
import type { z } from "zod";

type ThreadEntityView = z.infer<typeof zThreadEntityView>;

export type Decimal = string | bigint;
export function decimalBigInt(value: Decimal): bigint {
  return typeof value === "bigint" ? value : BigInt(value);
}
export type PayloadRef = NonNullable<ThreadEntityView["text_ref"]>;

type ThreadEntityFor<View extends ThreadEntityView> = {
  threadId: View["thread_id"];
  projectionEpoch: View["projection_epoch"];
  entityKind: View["entity_kind"];
  entityId: View["entity_id"];
  entityIndex: Decimal;
  cursor: View["cursor"];
  revisionCursor: View["revision_cursor"];
  pending: View["pending"];
  turnId: View["turn_id"];
  state: View["state"];
  textRef: View["text_ref"];
  argumentsRef: View["arguments_ref"];
  outputRef: View["output_ref"];
  inputRef: View["input_ref"];
};

/** The entity discriminator and its state stay correlated after adapting the wire field names. */
export type ThreadEntity = ThreadEntityView extends infer View
  ? View extends ThreadEntityView
    ? ThreadEntityFor<View>
    : never
  : never;

/** The rows a reader holds of a thread: its tail, and as far back as it has loaded. */
export interface ThreadWindow {
  /** The thread's entities and pending commands, in no particular order. */
  rows: ThreadEntity[];
  /** Whether `rows` reach the thread as it was when the window opened. */
  caughtUp: boolean;
  /** Whether rows exist before the oldest held; `loadOlder` loads the next page of them. */
  olderAvailable: boolean;
  /** Whether that page is loading. `loadOlder` does nothing meanwhile, nor once the window has
   * stopped or its epoch is gone, so a view may call it whenever the reader nears the oldest row. */
  loadingOlder: boolean;
  loadOlder: () => void;
  /** `reconnecting` while a failed read of the thread is being retried, and until one succeeds
   * `rows` may be out of date; `live` otherwise. */
  connection: StreamConnection;
  /** Why the window stopped following the thread. It follows on again by itself from where it
   * stopped, after a pause that grows with each failure in a row, and shows the error until it is
   * current again; `retry` tries at once. */
  error: string | null;
  retry: () => void;
}

export interface ThreadState {
  /** Null until the thread's first window opens; from then on there is always one. */
  window: ThreadWindow | null;
  /** Why reaching the thread last failed. The implementation retries, keeping any window it has. */
  error: string | null;
}

export interface Payload {
  /** The longest prefix of the referenced revision that has arrived, itself an earlier revision;
   * null before any has. */
  body: string | null;
  /** Why the body stopped loading short of the whole revision; null while it loads and once it is
   * whole. It is read again by itself after a pause that grows with each failure in a row; `retry`
   * starts it again at once. */
  error: string | null;
  retry: () => void;
}

export interface ThreadSync {
  /** Syncs `threadId` while mounted. The hooks read the nearest enclosing `Thread`. */
  Thread: (props: { threadId: string; children: ReactNode }) => JSX.Element;
  useThread: () => ThreadState;
  /** The command rows among `commandIds`, pending or settled, however old. */
  useCommandRows: (commandIds: readonly string[]) => ThreadEntity[];
  usePayload: (reference: PayloadRef) => Payload;
}

export const ThreadSyncContext: Context<ThreadSync | null> = createContext<ThreadSync | null>(null);

export function useThreadSync(): ThreadSync {
  const sync = useContext(ThreadSyncContext);
  if (sync === null) throw new Error("thread sync is read inside a ThreadSyncContext provider");
  return sync;
}
