import type { JsonValue } from "@bufbuild/protobuf";
import type { Offset } from "@electric-sql/client";

export interface SavedWindow {
  rows: [string, JsonValue][];
  lowest: string | null;
  exhausted: boolean;
  position: { handle: string; offset: Offset };
}
interface Entry {
  id: string;
  version: string;
  value: unknown;
}
const DATABASE = "agentplane-history-v1";
// Persisted values: the eviction index sorts bodies before windows.
enum EntryKind {
  Body = 0,
  Window = 1,
}
interface UsageRow {
  id: string;
  priority: EntryKind;
  bytes: number;
  used: number;
}
const emptyUsage = (): ThreadCacheUsage => ({ windows: 0, bodies: 0, bytes: 0 });

function accountFor(total: ThreadCacheUsage, kind: EntryKind, records: number, bytes: number): void {
  if (kind === EntryKind.Window) total.windows += records;
  else total.bodies += records;
  total.bytes += bytes;
}

let opening: Promise<IDBDatabase> | undefined;

function historyBuckets() {
  return (
    navigator as Navigator & {
      storageBuckets?: { keys(): Promise<string[]>; open(name: string): Promise<{ indexedDB: IDBFactory }> };
    }
  ).storageBuckets;
}

// Explicit logout removes this bucket via Clear-Site-Data. Never fall back to the
// default bucket, which also holds data unrelated to conversation history.
async function openHistoryDatabase(): Promise<IDBDatabase> {
  const buckets = historyBuckets();
  if (!buckets) throw new Error("Named storage buckets unavailable");
  const { indexedDB } = await buckets.open("agentplane-history");
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 2);
    request.onupgradeneeded = ({ oldVersion }) => {
      const db = request.result;
      if (oldVersion === 0) {
        db.createObjectStore("entries", { keyPath: "id" });
        db.createObjectStore("usage", { keyPath: "id" }).createIndex("eviction", ["priority", "used"]);
      }
      const totals = db.createObjectStore("totals");
      const total = emptyUsage();
      if (oldVersion === 0) totals.put(total, "history");
      else {
        // Backfill existing caches once. Subsequent writes and evictions maintain this record.
        const metadata = request.transaction!.objectStore("usage").openCursor();
        metadata.onsuccess = () => {
          const row = metadata.result;
          if (!row) {
            totals.put(total, "history");
            return;
          }
          const meta = row.value as UsageRow;
          accountFor(total, meta.priority, 1, meta.bytes);
          row.continue();
        };
      }
    };
    request.onblocked = () => reject(new Error("Cache upgrade blocked"));
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => {
        db.close();
        opening = undefined;
      };
      resolve(db);
    };
  });
}

function database(): Promise<IDBDatabase> {
  return (opening ??= openHistoryDatabase().catch((error: unknown) => {
    opening = undefined;
    throw error;
  }));
}

function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function finished(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error);
  });
}

export interface ThreadCacheUsage {
  windows: number;
  bodies: number;
  bytes: number;
}

/** Read the cache-wide counters without changing any entry's last-use time. */
export async function readThreadCacheUsage(): Promise<ThreadCacheUsage | null> {
  const buckets = historyBuckets();
  if (!buckets) return null;
  if (!(await buckets.keys()).includes("agentplane-history")) return emptyUsage();
  const tx = (await database()).transaction("totals");
  return result<ThreadCacheUsage>(tx.objectStore("totals").get("history"));
}

function updateTotals(tx: IDBTransaction, change: (total: ThreadCacheUsage) => void): void {
  const totals = tx.objectStore("totals");
  const get = totals.get("history");
  get.onsuccess = () => {
    const total = get.result as ThreadCacheUsage;
    change(total);
    totals.put(total, "history");
  };
}

// Evict complete bodies first, then windows, oldest use first within each group. Read only
// metadata while selecting victims; the origin's other stores (notably pending input) are untouched.
async function trim(db: IDBDatabase, bytes: number): Promise<void> {
  const tx = db.transaction(["entries", "usage", "totals"], "readwrite");
  const done = finished(tx);
  const removed = emptyUsage();
  const cursor = tx.objectStore("usage").index("eviction").openCursor();
  cursor.onsuccess = () => {
    const row = cursor.result;
    if (!row || bytes <= 0) {
      if (removed.windows || removed.bodies)
        updateTotals(tx, (total) => {
          total.windows -= removed.windows;
          total.bodies -= removed.bodies;
          total.bytes -= removed.bytes;
        });
      return;
    }
    const meta = row.value as UsageRow;
    accountFor(removed, meta.priority, 1, meta.bytes);
    bytes -= meta.bytes;
    tx.objectStore("entries").delete(meta.id);
    row.delete();
    row.continue();
  };
  await done;
}

let nextEstimate = 0;
async function makeRoom(db: IDBDatabase): Promise<void> {
  if (Date.now() < nextEstimate) return;
  nextEstimate = Date.now() + 60_000;
  const estimate = await navigator.storage?.estimate?.();
  if (estimate?.quota && estimate.usage && estimate.usage > estimate.quota * 0.9)
    await trim(db, estimate.usage - estimate.quota * 0.8);
}

export class ThreadCache {
  private constructor(
    private db: IDBDatabase,
    private prefix: string
  ) {}
  private version: string | null = null;
  private writable = true;

  static async open(account: string | null, threadId: string, epoch: string): Promise<ThreadCache | null> {
    if (!account) return null;
    try {
      return new ThreadCache(await database(), JSON.stringify([account, threadId, epoch]));
    } catch {
      return null;
    }
  }

  private async read<T>(name: string): Promise<T | null> {
    const id = this.prefix + name;
    const tx = this.db.transaction(["entries", "usage"], "readwrite");
    const store = tx.objectStore("usage");
    const reading = tx.objectStore("entries").get(id);
    const used = store.get(id);
    used.onsuccess = () => {
      if (used.result) store.put({ ...used.result, used: Date.now() });
    };
    const [entry] = await Promise.all([result<Entry | undefined>(reading), finished(tx)]);
    if (!entry) return null;
    if (name === "window") this.version = entry.version;
    return entry.value as T;
  }

  private async write(name: string, value: unknown): Promise<void> {
    const id = this.prefix + name;
    const bytes = new Blob([JSON.stringify(value)]).size;
    const version = crypto.randomUUID();
    const commit = async () => {
      const tx = this.db.transaction(["entries", "usage", "totals"], "readwrite");
      const done = finished(tx);
      const entries = tx.objectStore("entries");
      const request = entries.getKey(id);
      // Bodies are immutable by revision. A window may advance only from the version we read.
      request.onsuccess = () => {
        const put = () => {
          const kind = name === "window" ? EntryKind.Window : EntryKind.Body;
          const meta = tx.objectStore("usage").get(id);
          meta.onsuccess = () => {
            const previous = meta.result as UsageRow | undefined;
            updateTotals(tx, (total) => accountFor(total, kind, previous ? 0 : 1, bytes - (previous?.bytes ?? 0)));
          };
          entries.put({ id, version, value });
          tx.objectStore("usage").put({
            id,
            used: Date.now(),
            priority: kind,
            bytes,
          });
        };
        if (name !== "window") {
          if (!request.result) put();
          return;
        }
        const old = entries.get(id);
        old.onsuccess = () => {
          if ((old.result?.version ?? null) !== this.version) this.writable = false;
          else put();
        };
      };
      await done;
    };
    await makeRoom(this.db);
    try {
      await commit();
    } catch (error) {
      if (!(error instanceof DOMException) || error.name !== "QuotaExceededError") throw error;
      await trim(this.db, Math.max(4 * 1024 * 1024, bytes * 2));
      await commit();
    }
    if (name === "window" && this.writable) this.version = version;
  }

  async readWindow(): Promise<SavedWindow | null> {
    try {
      return await this.read<SavedWindow>("window");
    } catch {
      return null;
    }
  }
  async saveWindow(window: SavedWindow): Promise<void> {
    if (this.writable)
      try {
        await this.write("window", window);
      } catch {
        this.writable = false;
      }
  }
  async readBody(id: string): Promise<string[] | null> {
    try {
      return await this.read<string[]>(id);
    } catch {
      return null;
    }
  }
  async saveBody(id: string, chunks: string[]): Promise<void> {
    try {
      await this.write(id, chunks);
    } catch {
      /* Disk storage is optional. */
    }
  }
}
