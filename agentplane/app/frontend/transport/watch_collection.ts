/** Common oneof shape of generated snapshot/upsert/remove watch responses. */
export type CollectionFrame<T, S, I> =
  | { case: "snapshot"; value: S }
  | { case: "snapshotComplete" }
  | { case: "upsert"; value: T }
  | { case: "removed"; value: I }
  | { case: undefined };

/** Assemble one connection's collection. Callers own how to display a disconnected snapshot. */
export class WatchCollection<T, I extends { uid: string }> {
  private readonly items = new Map<string, T>();
  private complete = false;

  constructor(
    private readonly kind: string,
    private readonly identity: (item: T) => I | undefined,
    private readonly key: (identity: I) => string | undefined
  ) {}

  beginSnapshot(): void {
    this.items.clear();
    this.complete = false;
  }

  /** Return whether the visible collection changed. */
  apply<S>(frame: CollectionFrame<T, S, I>, snapshotItems: (chunk: S) => readonly T[]): boolean {
    switch (frame.case) {
      case "snapshot":
        if (this.complete) throw new Error(`Unexpected ${this.kind} snapshot`);
        for (const item of snapshotItems(frame.value)) this.put(item);
        return false;
      case "snapshotComplete":
        if (this.complete) throw new Error(`Unexpected ${this.kind} snapshot completion`);
        this.complete = true;
        break;
      case "upsert":
        if (!this.complete) throw new Error(`${this.kind} upsert before snapshot completion`);
        this.put(frame.value);
        break;
      case "removed": {
        if (!this.complete) throw new Error(`${this.kind} removal before snapshot completion`);
        const key = this.key(frame.value);
        if (!key || !frame.value.uid) throw new Error(`${this.kind} removal missing identity`);
        const item = this.items.get(key);
        if (item === undefined || this.identity(item)?.uid !== frame.value.uid) return false;
        this.items.delete(key);
        break;
      }
      default:
        throw new Error(`Empty ${this.kind} watch frame`);
    }
    return true;
  }

  snapshotValues(): T[] {
    return [...this.items.values()];
  }

  snapshotByKey(): ReadonlyMap<string, T> {
    return new Map(this.items);
  }

  private put(item: T): void {
    const identity = this.identity(item);
    const key = identity?.uid ? this.key(identity) : undefined;
    if (!key) throw new Error(`${this.kind} missing identity`);
    if (!this.complete && this.items.has(key)) throw new Error(`Duplicate ${this.kind} snapshot key ${key}`);
    this.items.set(key, item);
  }
}
