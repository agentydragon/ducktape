import { CodeBlock } from "./code_block";
import type { PreviewVariant } from "./tool_rendering/vocabulary";

// Width-aware pretty-print: a value whose one-line form fits within `maxLength` (accounting for the
// current indent) stays inline, and only larger arrays/objects break one child per line, each child
// re-evaluated the same way — so `[1, 2, 3, 4, 5]` stays one line while a big nested object expands.
// That layout is what gives CodeMirror meaningful structure to fold in compact mode. A local take on
// json-stringify-pretty-compact, kept in-repo rather than taken as an npm dependency.
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

// MAX_WIDTH is the fit-or-expand threshold: short arrays/objects stay inline, larger ones break one
// child per line. Detailed shows the value in full; compact auto-folds it to fill the block.
const MAX_WIDTH = 72;

/** A tool call's arguments as a syntax-highlighted, foldable JSON code block. Compact auto-folds to
 * fill the block with leading entries (see CodeBlock); detailed shows the full value with line
 * numbers. Width-aware pretty-print keeps short collections inline either way. */
export function JsonPreview({ value, variant }: { value: unknown; variant: PreviewVariant }) {
  return (
    <CodeBlock
      language="json"
      value={compactStringify(value, MAX_WIDTH, "")}
      compact={variant === "compact"}
      lineNumbers={variant === "detailed"}
    />
  );
}
