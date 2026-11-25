import { readFileSync } from "node:fs";

export function readJsonLines(paths) {
  return paths.flatMap((file) =>
    readFileSync(file, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line))
  );
}

// Match Prettier's padding so generated tables can be checked in directly.
export function markdownTable(header, body) {
  const all = [header, ...body];
  const widths = header.map((_, column) => Math.max(3, ...all.map((row) => String(row[column]).length)));
  const line = (cells) => `| ${cells.map((cell, column) => String(cell).padEnd(widths[column])).join(" | ")} |`;
  return [line(header), line(widths.map((width) => "-".repeat(width))), ...body.map(line)].join("\n");
}
