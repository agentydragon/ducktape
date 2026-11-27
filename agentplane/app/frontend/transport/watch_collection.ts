import { useCallback, useEffect, useRef, useState } from "react";

import type { StreamConnection } from "./stream_reader";

/** Common oneof shape of generated snapshot/upsert/remove watch responses. */
export type CollectionFrame<T, S, R> =
  | { case: "snapshot"; value: S }
  | { case: "snapshotComplete" }
  | { case: "upsert"; value: T }
  | { case: "removed"; value: R }
  // Planned renewal: the server ends the stream, and the next connection starts a fresh snapshot.
  | { case: "reconnectRequired" }
  | { case: undefined };

/** How a watched collection keys its items. */
export type CollectionConfig<T, R> = {
  kind: string;
  /** The item's key; undefined or empty rejects the item as missing identity. */
  key: (item: T) => string | undefined;
  /** How a removal names a held item; a watch without one fails on a removal. */
  removal?: {
    /** The key of the item it removes; undefined or empty rejects the removal as missing identity. */
    key: (removed: R) => string | undefined;
    /** Whether it still names the held item, e.g. by UID; by default it does. */
    matches?: (held: T, removed: R) => boolean;
  };
  /** Whether `incoming` replaces the held item, e.g. a version fence; by default it does. Snapshots are not fenced. */
  supersedes?: (held: T, incoming: T) => boolean;
};

/**
 * Assemble one connection's collection. An upsert or removal before the snapshot completes breaks the
 * watch contract and throws. Callers own how to display a disconnected snapshot.
 */
export class WatchCollection<T, R = never> {
  private readonly items = new Map<string, T>();
  private complete = false;

  constructor(private readonly config: CollectionConfig<T, R>) {}

  get completed(): boolean {
    return this.complete;
  }

  beginSnapshot(): void {
    this.items.clear();
    this.complete = false;
  }

  /** Return whether the visible collection changed. */
  apply<S>(frame: CollectionFrame<T, S, R>, snapshotItems: (chunk: S) => readonly T[]): boolean {
    const { kind, removal } = this.config;
    switch (frame.case) {
      case "snapshot":
        if (this.complete) throw new Error(`Unexpected ${kind} snapshot`);
        for (const item of snapshotItems(frame.value)) {
          const key = this.keyOf(item);
          if (this.items.has(key)) throw new Error(`Duplicate ${kind} snapshot key ${key}`);
          this.items.set(key, item);
        }
        return false;
      case "snapshotComplete":
        if (this.complete) throw new Error(`Unexpected ${kind} snapshot completion`);
        this.complete = true;
        return true;
      case "upsert":
        if (!this.complete) throw new Error(`${kind} upsert before snapshot completion`);
        return this.put(frame.value);
      case "removed": {
        if (!this.complete) throw new Error(`${kind} removal before snapshot completion`);
        if (removal === undefined) throw new Error(`${kind} watch sent a removal`);
        const key = removal.key(frame.value);
        if (!key) throw new Error(`${kind} removal missing identity`);
        const held = this.items.get(key);
        if (held === undefined || (removal.matches && !removal.matches(held, frame.value))) return false;
        this.items.delete(key);
        return true;
      }
      case "reconnectRequired":
        return false;
      default:
        throw new Error(`Empty ${kind} watch frame`);
    }
  }

  /**
   * Replace a held item with a copy read outside the watch, e.g. a mutation's answer. Return whether
   * the visible collection changed. Before the snapshot completes it never does: the snapshot is newer.
   */
  replace(item: T): boolean {
    return this.complete && this.items.has(this.keyOf(item)) && this.put(item);
  }

  /** Replace the collection with a full read made outside the watch; like `replace`, only once complete. */
  reset(items: readonly T[]): boolean {
    if (!this.complete) return false;
    this.items.clear();
    for (const item of items) this.items.set(this.keyOf(item), item);
    return true;
  }

  snapshotValues(): T[] {
    return [...this.items.values()];
  }

  snapshotByKey(): ReadonlyMap<string, T> {
    return new Map(this.items);
  }

  private keyOf(item: T): string {
    const key = this.config.key(item);
    if (!key) throw new Error(`${this.config.kind} missing identity`);
    return key;
  }

  private put(item: T): boolean {
    const key = this.keyOf(item);
    const held = this.items.get(key);
    if (held !== undefined && this.config.supersedes && !this.config.supersedes(held, item)) return false;
    this.items.set(key, item);
    return true;
  }
}

export type CollectionWatchHandlers<F> = {
  onOpen: () => void;
  onFrame: (frame: F) => void;
  onConnection: (connection: StreamConnection) => void;
};

export type CollectionWatch<T> = {
  /** The last complete collection, kept through a reconnect until the next snapshot completes; null before the first. */
  items: T[] | null;
  connection: StreamConnection;
  /** Show a held item's copy read outside the watch, e.g. a mutation's answer. */
  replace: (item: T) => void;
  /** Show a full read of the collection made outside the watch. */
  reset: (items: readonly T[]) => void;
};

/**
 * Follow one snapshot/upsert/remove watch for the component's lifetime. `config`, `follow` and
 * `snapshotItems` must be stable (module-level or memoized): a changed one reopens the watch. A null
 * `follow` follows nothing.
 *
 * While a snapshot is replaying, `replace` and `reset` change only what is shown; its completion
 * then shows the newer snapshot.
 */
export function useWatchCollection<T, S, R = never>(
  config: CollectionConfig<T, R>,
  follow: ((handlers: CollectionWatchHandlers<CollectionFrame<T, S, R>>) => () => void) | null,
  snapshotItems: (chunk: S) => readonly T[]
): CollectionWatch<T> {
  const [items, setItems] = useState<T[] | null>(null);
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  const collection = useRef<WatchCollection<T, R> | null>(null);
  useEffect(() => {
    if (follow === null) return;
    const watched = new WatchCollection(config);
    collection.current = watched;
    const stop = follow({
      onOpen: () => watched.beginSnapshot(),
      onFrame: (frame) => {
        if (watched.apply(frame, snapshotItems)) setItems(watched.snapshotValues());
      },
      onConnection: setConnection,
    });
    return () => {
      collection.current = null;
      stop();
    };
  }, [config, follow, snapshotItems]);
  const replace = useCallback(
    (item: T): void => {
      const watched = collection.current;
      if (watched?.completed) {
        if (watched.replace(item)) setItems(watched.snapshotValues());
        return;
      }
      const key = config.key(item);
      setItems(
        (current) =>
          current?.map((held) =>
            config.key(held) === key && (config.supersedes?.(held, item) ?? true) ? item : held
          ) ?? null
      );
    },
    [config]
  );
  const reset = useCallback((next: readonly T[]): void => {
    const watched = collection.current;
    setItems(watched?.reset(next) ? watched.snapshotValues() : [...next]);
  }, []);
  return { items, connection, replace, reset };
}
