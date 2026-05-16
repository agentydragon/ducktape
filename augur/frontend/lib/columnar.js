function rowsFromColumnar(table, rowCountKey) {
  const rowCount = table?.[rowCountKey];
  if (!table || typeof table !== "object" || typeof rowCount !== "number" || !table.columns) {
    throw new Error("Malformed columnar table payload");
  }
  const entries = Object.entries(table.columns);
  for (const [key, values] of entries) {
    if (!Array.isArray(values) || values.length !== rowCount) {
      throw new Error(`Malformed columnar table column: ${key}`);
    }
  }
  return Array.from({ length: rowCount }, (_, index) =>
    Object.fromEntries(entries.map(([key, values]) => [key, values[index]]))
  );
}

export function rowsFromSnakeColumnar(table) {
  return rowsFromColumnar(table, "row_count");
}

export function rowsFromCamelColumnar(table) {
  return rowsFromColumnar(table, "rowCount");
}
