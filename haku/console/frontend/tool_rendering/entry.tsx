// A registry entry: the zod schema for one tool's arguments paired with a renderer over the
// *parsed*, typed value. The registry (index.tsx) safeParses once per dispatch and hands the
// widget already-validated data, so no widget repeats `schema.safeParse(args)`. Leaf module (no
// widget deps) so index.tsx and every per-server module import it without a cycle.
import type { ReactNode } from "react";
import type { z } from "zod";

import type { PreviewProps, PreviewVariant } from "./vocabulary";

export type ToolPreview<S extends z.ZodTypeAny = z.ZodTypeAny> = {
  schema: S;
  // Stored with `never` args: the schema-specific type is checked at the `definePreview` call
  // site and erased here so a heterogeneous `Record<string, ToolPreview>` holds every tool's
  // entry. `renderPreview` is the one place that feeds them the parsed output.
  render: (args: never, variant: PreviewVariant) => ReactNode;
};

/** Bind a tool's argument schema to the widget that renders it. `Widget` takes the schema's parsed
 * output as `args`, plus the `variant`. Pass the component directly — this builds the `<Widget/>`
 * element, a real child component, so the widget's own hooks work. The tool's one-line action
 * description lives in `actions.ts`. */
export function definePreview<S extends z.ZodTypeAny>(
  schema: S,
  Widget: (props: PreviewProps<z.infer<S>>) => ReactNode
): ToolPreview<S> {
  const render = (args: z.infer<S>, variant: PreviewVariant): ReactNode => <Widget args={args} variant={variant} />;
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
