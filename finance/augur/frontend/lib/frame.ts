export function rowsFrom(frame: unknown) {
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
  return Array.from({ length: rowCount }, (_, index) =>
    Object.fromEntries(entries.map(([key, values]) => [key, (values as unknown[])[index]]))
  );
}
