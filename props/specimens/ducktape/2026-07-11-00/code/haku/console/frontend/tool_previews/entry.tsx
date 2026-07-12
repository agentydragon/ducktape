// A registry entry: the zod schema for one tool's arguments paired with a renderer over the
// *parsed*, typed value. The registry (index.tsx) safeParses once per dispatch and hands the
// widget already-validated data, so no widget repeats `schema.safeParse(args)`. Leaf module (no
// widget deps) so index.tsx and every per-server module import it without a cycle.
import type { ReactNode } from "react";
import type { z } from "zod";

import type { PreviewProps, PreviewVariant } from "./variant.tsx";

/** A registered tool's own one-line action description for the card's identity line — a
 * server-labelled verb phrase computed from the parsed args ("Gmail: Draft email", "kubectl:
 * Delete Pod"). `destructive` colors it as a danger cue (irreversible deletes). */
export type ToolAction = { text: string; destructive?: boolean };

export type ToolPreview = {
  schema: z.ZodTypeAny;
  // Stored with `never` args: the schema-specific type is checked at the `definePreview` call
  // site and erased here so a heterogeneous `Record<string, ToolPreview>` holds every tool's
  // entry. `renderPreview`/`describeAction` are the one place that feed them the parsed output.
  render: (args: never, variant: PreviewVariant) => ReactNode;
  describe?: (args: never) => ToolAction;
};

/** Bind a tool's argument schema to the widget that renders it (and, optionally, a `describe`
 * that turns the parsed args into the card's action-description line). `Widget` takes the
 * schema's inferred `args` plus the `variant`; both `render`- and `describe`-bound callbacks are
 * fed the schema's parsed output. Pass the component directly — this builds the `<Widget/>`
 * element (a real child component, so the widget's own hooks work). */
export function definePreview<S extends z.ZodTypeAny>(
  schema: S,
  Widget: (props: PreviewProps<z.infer<S>>) => ReactNode,
  describe?: (args: z.infer<S>) => ToolAction
): ToolPreview {
  const render = (args: z.infer<S>, variant: PreviewVariant): ReactNode => <Widget args={args} variant={variant} />;
  return { schema, render: render as ToolPreview["render"], describe: describe as ToolPreview["describe"] };
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

/** The tool's action description for the card identity line, or `null` if it has no `describe`
 * or its args don't parse (the caller then falls back to `serverId.toolName`). */
export function describeAction(preview: ToolPreview, args: Record<string, unknown>): ToolAction | null {
  if (!preview.describe) return null;
  const parsed = preview.schema.safeParse(args);
  return parsed.success ? preview.describe(parsed.data as never) : null;
}
