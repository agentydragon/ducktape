/**
 * What the thread view reads of a thread, whatever keeps it in sync: the thread's rows, the rows of
 * commands this client sent, and bodies by reference, each kept current. The app root provides the
 * implementation through `ThreadSyncContext`.
 */
import { createContext, useContext, type Context, type JSX, type ReactNode } from "react";

import type { ThreadEntityView } from "./client";

export type Decimal = string | bigint;
export function decimalBigInt(value: Decimal): bigint {
  return typeof value === "bigint" ? value : BigInt(value);
}
export type PayloadRef = NonNullable<ThreadEntityView["text_ref"]>;

export interface ThreadEntity {
  threadId: string;
  projectionEpoch: string;
  entityKind: "view_state" | "item" | "confirmed_input" | "lifecycle" | "command";
  entityId: string;
  entityIndex: Decimal;
  cursor: Decimal;
  revisionCursor: Decimal;
  pending: boolean;
  turnId: string | null;
  state: ThreadEntityView["state"];
  textRef: PayloadRef | null;
  argumentsRef: PayloadRef | null;
  outputRef: PayloadRef | null;
  inputRef: PayloadRef | null;
}

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
  /** Whether a read of the thread failed and is being retried. Until one succeeds, `rows` may be
   * out of date. */
  reconnecting: boolean;
  /** Why the window stopped following the thread; `refresh` opens it again. */
  error: string | null;
  refresh: () => void;
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
  /** Why the body stopped loading; `retry` starts it again. */
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
