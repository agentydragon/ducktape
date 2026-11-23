import { readFileSync } from "node:fs";

export function readJsonLines(paths) {
  return paths.flatMap((file) =>
    readFileSync(file, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line))
  );
}

export function groupBy(items, key) {
  const groups = new Map();
  for (const item of items) {
    const value = key(item);
    const group = groups.get(value);
    if (group) group.push(item);
    else groups.set(value, [item]);
  }
  return groups;
}

// Match Prettier's padding so generated tables can be checked in directly.
export function markdownTable(header, body) {
  const all = [header, ...body];
  const widths = header.map((_, column) => Math.max(3, ...all.map((row) => String(row[column]).length)));
  const line = (cells) => `| ${cells.map((cell, column) => String(cell).padEnd(widths[column])).join(" | ")} |`;
  return [line(header), line(widths.map((width) => "-".repeat(width))), ...body.map(line)].join("\n");
}
