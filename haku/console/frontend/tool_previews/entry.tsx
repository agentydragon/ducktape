// A registry entry: the zod schema for one tool's arguments paired with a renderer over the
// *parsed*, typed value. The registry (index.tsx) safeParses once per dispatch and hands the
// widget already-validated data, so no widget repeats `schema.safeParse(args)`. Leaf module (no
// widget deps) so index.tsx and every per-server module import it without a cycle.
import type { ReactNode } from "react";
import type { z } from "zod";

import type { PreviewVariant } from "./variant.tsx";

export type ToolPreview = {
  schema: z.ZodTypeAny;
  // Stored with `never` args: the schema-specific type is checked at the `definePreview` call
  // site and erased here so a heterogeneous `Record<string, ToolPreview>` holds every tool's
  // entry. `renderPreview` is the one place that feeds it the schema's parsed output.
  render: (args: never, variant: PreviewVariant) => ReactNode;
};

/** Bind a tool's argument schema to a renderer of that schema's inferred type. The generic
 * checks `render`'s argument against `schema` at each call site. */
export function definePreview<S extends z.ZodTypeAny>(
  schema: S,
  render: (args: z.infer<S>, variant: PreviewVariant) => ReactNode
): ToolPreview {
  return { schema, render: render as ToolPreview["render"] };
}

/** Parse `args` with the entry's schema and render; `null` on a schema mismatch, so the caller
 * falls back to raw JSON (arguments aren't validated until execution, so a pending call's may be
 * malformed). */
export function renderPreview(
  preview: ToolPreview,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  const parsed = preview.schema.safeParse(args);
  // parsed.data is `unknown` (the schema is type-erased to ZodTypeAny in storage); the matching
  // `definePreview` bound this render to exactly this schema's output, so the cast is sound.
  return parsed.success ? preview.render(parsed.data as never, variant) : null;
}
