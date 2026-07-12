import hljs from "highlight.js/lib/core";
import json from "highlight.js/lib/languages/json";

import type { PreviewVariant } from "./tool_rendering/variant.tsx";

hljs.registerLanguage("json", json);

// Width-aware pretty-print: a value whose one-line form fits within `maxLength` (accounting for
// the current indent) stays inline; only larger arrays/objects break, one child per line, each
// child re-evaluated the same way. So `[1, 2, 3, 4, 5]` stays one line while a big nested object
// expands. (A local ~30-line take on json-stringify-pretty-compact — kept in-repo because adding
// that npm dep needs a pnpm-lock regen this environment's egress policy currently blocks.)
function inlineJson(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v) ?? "null";
  if (Array.isArray(v)) return `[${v.map(inlineJson).join(", ")}]`;
  return `{${Object.entries(v as Record<string, unknown>)
    .map(([k, val]) => `${JSON.stringify(k)}: ${inlineJson(val)}`)
    .join(", ")}}`;
}

function compactStringify(value: unknown, maxLength: number, indent: string, unit = "  "): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  const oneLine = inlineJson(value);
  if (indent.length + oneLine.length <= maxLength) return oneLine;
  const inner = indent + unit;
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return `[\n${value.map((v) => inner + compactStringify(v, maxLength, inner, unit)).join(",\n")}\n${indent}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return "{}";
  const body = entries
    .map(([k, v]) => `${inner}${JSON.stringify(k)}: ${compactStringify(v, maxLength, inner, unit)}`)
    .join(",\n");
  return `{\n${body}\n${indent}}`;
}

// Compact-mode caps so a preview stays scannable; detailed shows the value in full. The compact
// printer keeps short arrays/objects inline and only breaks larger ones, so `[1, 2, 3, 4, 5]`
// stays one line — MAX_WIDTH is the fit-or-expand threshold.
const COMPACT_ARRAY_ITEMS = 6;
const COMPACT_OBJECT_ENTRIES = 8;
const COMPACT_STRING_CHARS = 80;
const COMPACT_DEPTH = 3;
const MAX_WIDTH = 72;

// Structurally truncate a value for compact rendering. Elided array tails / object key sets and
// over-deep or over-long leaves are replaced with valid-JSON string sentinels (`…(+N more)`,
// `[…]`, `{…}`, `…`) so the printed result stays parseable and highlights cleanly — unlike
// clamping the printed string, which cuts mid-token and drops the highlighting on the tail.
function truncate(value: unknown, depth: number): unknown {
  if (typeof value === "string") {
    return value.length > COMPACT_STRING_CHARS ? `${value.slice(0, COMPACT_STRING_CHARS)}…` : value;
  }
  if (Array.isArray(value)) {
    if (depth <= 0) return "[…]";
    const head: unknown[] = value.slice(0, COMPACT_ARRAY_ITEMS).map((v) => truncate(v, depth - 1));
    if (value.length > COMPACT_ARRAY_ITEMS) head.push(`…(+${value.length - COMPACT_ARRAY_ITEMS} more)`);
    return head;
  }
  if (value && typeof value === "object") {
    if (depth <= 0) return "{…}";
    const entries = Object.entries(value as Record<string, unknown>);
    const kept: Record<string, unknown> = {};
    for (const [k, v] of entries.slice(0, COMPACT_OBJECT_ENTRIES)) kept[k] = truncate(v, depth - 1);
    if (entries.length > COMPACT_OBJECT_ENTRIES) kept["…"] = `(+${entries.length - COMPACT_OBJECT_ENTRIES} more)`;
    return kept;
  }
  return value;
}

/** A tool call's arguments as a syntax-highlighted JSON code block. Width-aware pretty-printing
 * keeps short collections inline and only breaks larger ones; highlight.js colors it (json grammar
 * only, registered above — the lean core import, not the all-languages barrel). In compact mode the
 * value is structurally truncated first, so the block stays short while keeping both highlighting
 * and structure. */
export function JsonPreview({ value, variant }: { value: unknown; variant: PreviewVariant }) {
  const shown = variant === "compact" ? truncate(value, COMPACT_DEPTH) : value;
  const code = compactStringify(shown, MAX_WIDTH, "");
  // hljs escapes the input, so its span-wrapped output is safe to inject.
  const html = hljs.highlight(code, { language: "json", ignoreIllegals: true }).value;
  return <pre className="haku-shell-json hljs" dangerouslySetInnerHTML={{ __html: html }} />;
}
