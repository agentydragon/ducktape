export type PreviewVariant = "compact" | "detailed";

// A single line longer than this is clamped with a trailing "…", so one pathological line
// (a base64 blob, a minified payload) can't blow out a compact preview even within the line cap.
const COMPACT_LINE_CHARS = 200;

function clampLine(line: string): string {
  return line.length > COMPACT_LINE_CHARS ? `${line.slice(0, COMPACT_LINE_CHARS)}…` : line;
}

// The first `n` non-blank lines of a text block, for compact bodies (email drafts, applied
// manifests) that would otherwise render in full. Each kept line is also clamped by character
// count. `truncated` says whether whole lines were dropped (a per-line clamp shows its own "…").
export function firstLines(text: string, n: number): { text: string; truncated: boolean } {
  const nonBlank = text.split("\n").filter((line) => line.trim() !== "");
  return { text: nonBlank.slice(0, n).map(clampLine).join("\n"), truncated: nonBlank.length > n };
}

/** First `n` lines of a text block for a compact `<pre>`, with a trailing "…" line when more
 * was dropped. For prose (not a code block) use `firstLines` and append the ellipsis inline. */
export function clampBlock(text: string, n: number): string {
  const { text: head, truncated } = firstLines(text, n);
  return truncated ? `${head}\n…` : head;
}
