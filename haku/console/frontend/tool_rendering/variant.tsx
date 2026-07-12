// Compact vs detailed rendering, shared by the per-server preview widgets. A **compact**
// preview is the scannable form shown on a drawer approval card (and anywhere space is
// tight): list-shaped arguments collapse to their first few items, long bodies to their
// first few lines. A **detailed** preview is the full form shown in the expanded detail
// view. Leaf module (no widget deps) so index.tsx and every widget can import the type
// without a cycle.
import { Badge, type BadgeProps, Text, type TextProps } from "@mantine/core";
import type { PropsWithChildren } from "react";

export type PreviewVariant = "compact" | "detailed";

// The props every top-level preview widget takes: the tool's parsed arguments plus the variant
// to render. Shared so widgets (and `definePreview`) don't re-spell `{ args; variant }`.
export type PreviewProps<Args> = { args: Args; variant: PreviewVariant };

/** Tool-widget body copy. Keeping the default here prevents Mantine's larger application-level
 * default from leaking back into one server when a renderer omits `size`. */
export function PreviewText({ size = "sm", ...props }: PropsWithChildren<TextProps>) {
  return <Text size={size} {...props} />;
}

/** A tool widget's primary identity: same type scale as its body, distinguished by weight rather
 * than a one-off larger font. */
export function PreviewTitle({ size = "sm", fw = 600, ...props }: PropsWithChildren<TextProps>) {
  return <Text size={size} fw={fw} {...props} />;
}

/** Attribute/state pill for tool widgets. Variants and semantic colors remain call-site choices. */
export function PreviewBadge({ size = "sm", ...props }: PropsWithChildren<BadgeProps>) {
  return <Badge size={size} {...props} />;
}

// In compact previews, list-shaped arguments show only the first few items; the rest
// collapse to a single "… +N more" line so a card stays scannable.
export const COMPACT_ITEM_LIMIT = 3;

/** "4 threads" / "1 item" — a count plus its naively pluralized noun (append "s" unless
 * singular), for the action descriptions ("Add 5 items to stock"). */
export function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** A dimmed "… +N more" line for the items a compact preview elided; renders nothing at 0. */
export function MoreLine({ count }: { count: number }) {
  if (count <= 0) return null;
  return (
    <PreviewText size="xs" c="dimmed">
      … +{count} more
    </PreviewText>
  );
}

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
