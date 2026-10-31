import { rememberRowHeight, rememberedRowHeight } from "./history_sizes";

const ESTIMATED_ROW_HEIGHT = 180;

const PLAN_OWNER: unique symbol = Symbol("HistoryGeometry plan owner");
const COMMITTED_OWNER: unique symbol = Symbol("HistoryGeometry committed owner");

export interface RowDescriptor {
  readonly key: string;
  readonly revision: string;
}

export type Height =
  | { readonly kind: "estimate"; readonly pixels: number; readonly remembered: boolean }
  | { readonly kind: "observed"; readonly pixels: number };

export interface PlannedRow {
  readonly index: number;
  readonly key: string;
  readonly revision: string;
  readonly generation: number;
  readonly start: number;
  readonly height: Height;
}

export type LayoutPlan = Readonly<{
  readonly width: number;
  readonly rows: readonly PlannedRow[];
  readonly total: number;
  readonly [PLAN_OWNER]: symbol;
}>;

export type CommittedPlan = Readonly<{
  readonly plan: LayoutPlan;
  readonly [COMMITTED_OWNER]: symbol;
}>;

interface Evidence {
  readonly revision: string;
  readonly generation: number;
  readonly width: number;
  readonly height: number;
}

export class HistoryGeometry {
  private readonly owner = Symbol("HistoryGeometry");
  private latestCommitted: CommittedPlan | undefined;
  private readonly generations = new Map<string, number>();
  private readonly currentRevisions = new Map<string, string>();
  private readonly evidence = new Map<string, Evidence>();
  // A revision change makes the shared height hint for that key ambiguous until this view reads it.
  private readonly needsCurrentRevisionObservation = new Set<string>();

  constructor(private readonly threadId: string) {}

  plan(rows: readonly RowDescriptor[], width: number): LayoutPlan {
    if (!Number.isFinite(width) || width < 0) throw new RangeError("History width must be finite and non-negative");

    const seen = new Set<string>();
    let start = 0;
    const plannedRows: PlannedRow[] = [];
    for (const [index, descriptor] of rows.entries()) {
      if (seen.has(descriptor.key)) throw new Error(`Duplicate history row key: ${descriptor.key}`);
      seen.add(descriptor.key);

      const generation = this.generations.get(descriptor.key) ?? 0;
      const evidence = this.evidence.get(descriptor.key);
      let height: Height;
      if (evidence?.width === width) {
        if (evidence.revision !== descriptor.revision || evidence.generation !== generation) {
          height = { kind: "estimate", pixels: evidence.height, remembered: true };
        } else {
          height = { kind: "observed", pixels: evidence.height };
        }
      } else {
        const knownRevision = this.currentRevisions.get(descriptor.key);
        const revisionChanged = knownRevision !== undefined && knownRevision !== descriptor.revision;
        const hint =
          width > 0 && !revisionChanged && !this.needsCurrentRevisionObservation.has(descriptor.key)
            ? rememberedRowHeight(this.threadId, width, descriptor.key)
            : undefined;
        height = {
          kind: "estimate",
          pixels: hint ?? ESTIMATED_ROW_HEIGHT,
          remembered: hint !== undefined,
        };
      }

      const frozenHeight: Height = Object.freeze(height);
      plannedRows.push(
        Object.freeze({
          index,
          key: descriptor.key,
          revision: descriptor.revision,
          generation,
          start,
          height: frozenHeight,
        })
      );
      start += frozenHeight.pixels;
    }

    return Object.freeze({
      width,
      rows: Object.freeze(plannedRows),
      total: start,
      [PLAN_OWNER]: this.owner,
    });
  }

  commit(plan: LayoutPlan): CommittedPlan {
    if (plan[PLAN_OWNER] !== this.owner) throw new TypeError("Cannot commit a plan from another HistoryGeometry");

    for (const row of plan.rows) {
      const previousRevision = this.currentRevisions.get(row.key);
      if (previousRevision !== undefined && previousRevision !== row.revision) {
        this.needsCurrentRevisionObservation.add(row.key);
      }
      this.currentRevisions.set(row.key, row.revision);
    }

    const committed: CommittedPlan = Object.freeze({
      plan,
      [COMMITTED_OWNER]: this.owner,
    });
    this.latestCommitted = committed;
    return committed;
  }

  observe(
    committed: CommittedPlan,
    observations: readonly { readonly key: string; readonly height: number; readonly width: number }[]
  ): boolean {
    if (committed[COMMITTED_OWNER] !== this.owner) {
      throw new TypeError("Cannot observe a plan committed by another HistoryGeometry");
    }
    if (committed !== this.latestCommitted) return false;

    const { plan } = committed;
    const plannedByKey = new Map<string, PlannedRow>();
    for (const row of plan.rows) plannedByKey.set(row.key, row);
    let changed = false;
    for (const observation of observations) {
      const row = plannedByKey.get(observation.key);
      if (
        !row ||
        observation.width !== plan.width ||
        !Number.isFinite(observation.width) ||
        observation.width <= 0 ||
        !Number.isFinite(observation.height) ||
        observation.height < 1 ||
        (this.generations.get(row.key) ?? 0) !== row.generation ||
        this.currentRevisions.get(row.key) !== row.revision
      ) {
        continue;
      }

      const previous = this.evidence.get(row.key);
      const isSameEvidence =
        previous?.revision === row.revision &&
        previous.generation === row.generation &&
        previous.width === plan.width &&
        previous.height === observation.height;
      if (!isSameEvidence) {
        this.evidence.set(row.key, {
          revision: row.revision,
          generation: row.generation,
          width: plan.width,
          height: observation.height,
        });
        changed = true;
      }
      this.needsCurrentRevisionObservation.delete(row.key);
      rememberRowHeight(this.threadId, plan.width, row.key, observation.height);
    }
    return changed;
  }

  invalidate(keys: Iterable<string>): void {
    for (const key of keys) this.generations.set(key, (this.generations.get(key) ?? 0) + 1);
  }
}

export function mountedRange(
  plan: LayoutPlan,
  scrollTop: number,
  viewportHeight: number,
  overscan: number,
  retainedKeys: ReadonlySet<string>
): readonly PlannedRow[] {
  const rows = plan.rows;
  const top = Number.isFinite(scrollTop) ? Math.max(0, scrollTop) : 0;
  const viewport = Number.isFinite(viewportHeight) ? Math.max(0, viewportHeight) : 0;
  const extra = Number.isFinite(overscan) ? Math.max(0, overscan) : 0;
  const low = Math.max(0, top - extra);
  const high = Math.min(plan.total, top + viewport + extra);
  const indexes = new Set<number>();

  if (high > low) {
    let left = 0;
    let right = rows.length;
    while (left < right) {
      const middle = Math.floor((left + right) / 2);
      const row = rows[middle];
      if (row.start + row.height.pixels <= low) left = middle + 1;
      else right = middle;
    }
    for (let index = left; index < rows.length && rows[index].start < high; index++) indexes.add(index);
  }

  for (const key of retainedKeys) {
    const index = rows.findIndex((row) => row.key === key);
    if (index >= 0) indexes.add(index);
  }

  return Object.freeze([...indexes].sort((first, second) => first - second).map((index) => rows[index]));
}
