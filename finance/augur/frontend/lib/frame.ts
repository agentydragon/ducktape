type FrameColumns = Record<string, readonly unknown[]>;
type FrameRow<T extends FrameColumns> = { [K in keyof T]: T[K][number] };

export function rowsFrom<T extends FrameColumns>(frame: T | null | undefined): FrameRow<T>[] {
  if (frame == null) return [];
  if (typeof frame !== "object") {
    throw new Error("Malformed frame payload");
  }
  const entries = Object.entries(frame as Record<string, unknown>);
  if (entries.length === 0) return [];
  const rowCount = (entries[0][1] as unknown[]).length;
  for (const [key, values] of entries) {
    if (!Array.isArray(values) || values.length !== rowCount) {
      throw new Error(`Malformed frame column: ${key}`);
    }
  }
  // The validation above confirms every source column is an array with this row count; each
  // constructed row therefore has exactly the keys and value types declared by `T`.
  return Array.from(
    { length: rowCount },
    (_, index) => Object.fromEntries(entries.map(([key, values]) => [key, (values as unknown[])[index]])) as FrameRow<T>
  );
}
