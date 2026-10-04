/**
 * Row heights read in this tab, so that on returning to a thread the virtualizer lays a row out
 * at the height it had instead of a flat estimate. A row's height depends on the width it wrapped
 * at, so a reading is only used at the width it was taken at.
 */

const CAPACITY = 20_000;

/** Oldest written first: a Map iterates in insertion order. */
const heights = new Map<string, number>();
/** The width of the last reading, for a lookup made before the history's element has a width. */
let lastWidth = 0;

const entry = (threadId: string, width: number, key: string): string => `${threadId}\0${width}\0${key}`;

export function rememberRowHeight(threadId: string, width: number, key: string, height: number): void {
  const rounded = Math.round(width);
  if (rounded <= 0 || height < 1) return;
  lastWidth = rounded;
  const id = entry(threadId, rounded, key);
  heights.delete(id);
  heights.set(id, height);
  if (heights.size > CAPACITY) {
    const [oldest] = heights.keys();
    if (oldest !== undefined) heights.delete(oldest);
  }
}

export function rememberedRowHeight(threadId: string, width: number, key: string): number | undefined {
  return heights.get(entry(threadId, Math.round(width) || lastWidth, key));
}
