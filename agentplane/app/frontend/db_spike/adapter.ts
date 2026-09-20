/** Test-only sketches of projected data, not an app or runner wire protocol. */
import { equals } from "@bufbuild/protobuf";
import { createCollection, type Collection, type SyncConfig } from "@tanstack/db";

import { EventEntrySchema, type EventEntry } from "../../../protocol/event_log_pb";

export interface Entity {
  key: string;
  revision: bigint;
  value:
    | { kind: "item"; text: string }
    | { kind: "control"; model: string }
    | { kind: "command"; admission: EventEntry; outcome: EventEntry | null };
}

export interface Coverage {
  key: "$coverage";
  revision: bigint;
  value: { kind: "coverage"; source: string };
}

export type Row = Entity | Coverage;
export interface Snapshot {
  source: string;
  through: bigint;
  rows: readonly Entity[];
}

export interface Changes extends Snapshot {
  after: bigint;
  /** Window eviction, not deletion of authoritative history. */
  evict: readonly string[];
}

type Sync<T extends object> = Parameters<SyncConfig<T, string>["sync"]>[0];

function same(left: Row, right: Row): boolean {
  if (left.key !== right.key || left.revision !== right.revision) return false;
  const a = left.value;
  const b = right.value;
  switch (a.kind) {
    case "item":
      return b.kind === "item" && a.text === b.text;
    case "control":
      return b.kind === "control" && a.model === b.model;
    case "coverage":
      return b.kind === "coverage" && a.source === b.source;
    case "command":
      return (
        b.kind === "command" &&
        equals(EventEntrySchema, a.admission, b.admission) &&
        (a.outcome === null || b.outcome === null
          ? a.outcome === b.outcome
          : equals(EventEntrySchema, a.outcome, b.outcome))
      );
  }
}

function checkRows(rows: readonly Entity[], through: bigint): void {
  const keys = new Set<string>();
  for (const row of rows) {
    if (row.key === "$coverage" || keys.has(row.key) || row.revision < 0n || row.revision > through) {
      throw new Error("Invalid or duplicate row revision/key");
    }
    keys.add(row.key);
  }
}

/** One collection is the atomic boundary. No optimistic mutation handlers are installed. */
export class View {
  readonly rows: Collection<Row, string>;
  readonly evidence: Collection<EventEntry, string>;
  private sync!: Sync<Row>;
  private evidenceSync!: Sync<EventEntry>;
  private generation = 0;
  private closed = false;

  constructor() {
    this.rows = createCollection<Row, string>({
      getKey: (row) => row.key,
      startSync: true,
      gcTime: 0,
      sync: {
        rowUpdateMode: "full",
        sync: (sync) => {
          this.sync = sync;
        },
      },
    });
    this.evidence = createCollection<EventEntry, string>({
      getKey: (entry) => `${entry.origin?.sourceId}:${entry.cursor}`,
      startSync: true,
      gcTime: 0,
      sync: {
        sync: (sync) => {
          this.evidenceSync = sync;
          sync.markReady();
        },
      },
    });
  }

  get coverage(): Coverage {
    const row = this.rows.get("$coverage");
    if (row?.value.kind !== "coverage") throw new Error("No installed snapshot");
    return { key: "$coverage", revision: row.revision, value: row.value };
  }

  /** The returned follower belongs only to this bootstrap, including after a long-gap reset. */
  async bootstrap(response: Promise<Snapshot>): Promise<((changes: Changes) => Promise<void>) | null> {
    if (this.closed) throw new Error("View is closed");
    const generation = ++this.generation;
    let snapshot: Snapshot;
    try {
      snapshot = await response;
      if (generation !== this.generation) return null;
      checkRows(snapshot.rows, snapshot.through);
      if (!snapshot.source || snapshot.through < 0n) throw new Error("Invalid snapshot boundary");
      if (this.rows.has("$coverage")) {
        const previous = this.coverage;
        if (snapshot.source !== previous.value.source || snapshot.through < previous.revision) {
          throw new Error("Source changed or snapshot regressed");
        }
      }
    } catch (error) {
      if (generation !== this.generation) return null;
      // A failed replacement does not destroy the last usable view or its readiness.
      if (!this.rows.has("$coverage")) this.sync.markError(error);
      throw error;
    }
    this.sync.begin();
    this.sync.truncate();
    for (const row of snapshot.rows) this.sync.write({ type: "insert", value: row });
    this.sync.write({
      type: "insert",
      value: { key: "$coverage", revision: snapshot.through, value: { kind: "coverage", source: snapshot.source } },
    });
    await this.sync.commit();
    this.sync.markReady();
    return async (changes) => {
      if (generation !== this.generation) return;
      const current = this.coverage;
      if (
        changes.source !== current.value.source ||
        changes.after !== current.revision ||
        changes.through <= changes.after
      ) {
        throw new Error("Noncontiguous view changes; rebootstrap required");
      }
      checkRows(changes.rows, changes.through);
      for (const row of changes.rows) {
        if (row.revision <= changes.after) throw new Error("Live row predates its covered interval");
      }
      for (const key of changes.evict) {
        if (this.rows.get(key)?.value.kind !== "item") throw new Error("Only loaded items may be evicted");
        if (changes.rows.some((row) => row.key === key)) throw new Error("Cannot update and evict the same row");
      }
      this.sync.begin();
      for (const key of changes.evict) this.sync.write({ type: "delete", key });
      for (const row of changes.rows)
        this.sync.write({ type: this.rows.has(row.key) ? "update" : "insert", value: row });
      this.sync.write({ type: "update", value: { ...current, revision: changes.through } });
      await this.sync.commit();
    };
  }

  /** Explicit caller-owned request: no background full-history fetch or implicit cursor advance. */
  async history(response: Promise<readonly Entity[]>): Promise<void> {
    const generation = this.generation;
    const through = this.coverage.revision;
    const rows = await response;
    if (generation !== this.generation) return;
    checkRows(rows, through);
    const updates: Entity[] = [];
    for (const row of rows) {
      if (row.value.kind !== "item") throw new Error("History cannot replace current controls or commands");
      const existing = this.rows.get(row.key);
      if (existing && existing.value.kind !== "item") throw new Error("History cannot replace a different row kind");
      if (existing && existing.revision === row.revision && !same(existing, row))
        throw new Error("Conflicting row revision");
      if (!existing || row.revision > existing.revision) updates.push(row);
    }
    this.sync.begin();
    for (const row of updates) this.sync.write({ type: this.rows.has(row.key) ? "update" : "insert", value: row });
    await this.sync.commit();
  }

  async raw(response: Promise<readonly EventEntry[]>): Promise<void> {
    const generation = this.generation;
    const boundary = this.coverage;
    const entries = await response;
    if (generation !== this.generation) return;
    // Validate the whole response before beginning a transaction: sync has no public rollback.
    const additions = new Map<string, EventEntry>();
    for (const entry of entries) {
      if (
        entry.origin?.sourceId !== boundary.value.source ||
        entry.cursor < 1n ||
        entry.cursor > boundary.revision ||
        entry.origin.sequence !== entry.cursor ||
        !entry.event
      )
        throw new Error("Invalid evidence source/cursor");
      const key = `${entry.origin.sourceId}:${entry.cursor}`;
      const existing = additions.get(key) ?? this.evidence.get(key);
      if (existing && !equals(EventEntrySchema, existing, entry)) throw new Error("Conflicting evidence");
      if (!existing) additions.set(key, entry);
    }
    this.evidenceSync.begin();
    for (const value of additions.values()) this.evidenceSync.write({ type: "insert", value });
    await this.evidenceSync.commit();
  }

  async close(): Promise<void> {
    this.closed = true;
    ++this.generation;
    await Promise.all([this.rows.cleanup(), this.evidence.cleanup()]);
  }
}
