export type PreviewVariant = "compact" | "detailed";

// A single line longer than this is clamped with a trailing "…", so one pathological line
// (a base64 blob, a minified payload) can't blow out a compact preview even within the line cap.
const COMPACT_LINE_CHARS = 200;

function clampLine(line: string): string {
  return line.length > COMPACT_LINE_CHARS ? `${line.slice(0, COMPACT_LINE_CHARS)}…` : line;
}

/** First `n` non-blank lines of a text block for a compact `<pre>`, each clamped by character
 * count, with a trailing "…" line when whole lines were dropped. */
export function clampBlock(text: string, n: number): string {
  const nonBlank = text.split("\n").filter((line) => line.trim() !== "");
  const head = nonBlank.slice(0, n).map(clampLine).join("\n");
  return nonBlank.length > n ? `${head}\n…` : head;
}
