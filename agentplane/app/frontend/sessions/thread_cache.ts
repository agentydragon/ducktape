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
let opening: Promise<IDBDatabase> | undefined;

// Explicit logout removes this bucket via Clear-Site-Data. Never fall back to the
// default bucket, which also holds data unrelated to conversation history.
async function openHistoryDatabase(): Promise<IDBDatabase> {
  const buckets = (
    navigator as Navigator & {
      storageBuckets?: { open(name: string): Promise<{ indexedDB: IDBFactory }> };
    }
  ).storageBuckets;
  if (!buckets) throw new Error("Named storage buckets unavailable");
  const { indexedDB } = await buckets.open("agentplane-history");
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore("entries", { keyPath: "id" });
      request.result.createObjectStore("usage", { keyPath: "id" }).createIndex("eviction", ["priority", "used"]);
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

// Evict complete bodies first, then windows, oldest use first within each group. Read only
// metadata while selecting victims; the origin's other stores (notably pending input) are untouched.
async function trim(db: IDBDatabase, bytes: number): Promise<void> {
  const tx = db.transaction(["entries", "usage"], "readwrite");
  const done = finished(tx);
  const cursor = tx.objectStore("usage").index("eviction").openCursor();
  cursor.onsuccess = () => {
    const row = cursor.result;
    if (!row || bytes <= 0) return;
    const meta = row.value as { id: string; bytes: number };
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
      const tx = this.db.transaction(["entries", "usage"], "readwrite");
      const done = finished(tx);
      const entries = tx.objectStore("entries");
      const request = entries.getKey(id);
      // Bodies are immutable by revision. A window may advance only from the version we read.
      request.onsuccess = () => {
        const put = () => {
          entries.put({ id, version, value });
          tx.objectStore("usage").put({
            id,
            used: Date.now(),
            priority: name === "window" ? 1 : 0,
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
