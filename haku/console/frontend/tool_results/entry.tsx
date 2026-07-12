// A registry entry for one tool's *result*: the zod schema for the tool's unwrapped result
// payload paired with a renderer over the *parsed*, typed value — the result-side mirror of
// tool_previews/entry.tsx. The registry (index.tsx) safeParses once per dispatch and hands the
// widget already-validated data, so no widget repeats `schema.safeParse(payload)`. Leaf module
// (no widget deps) so index.tsx and every per-server module import it without a cycle.
import type { ReactNode } from "react";
import { z } from "zod";

import type { PreviewVariant } from "../tool_previews/variant.tsx";

export type ToolResultPreview<S extends z.ZodTypeAny = z.ZodTypeAny> = {
  schema: S;
  // Stored with `never` result: the schema-specific type is checked at the `defineResultPreview`
  // call site and erased here so a heterogeneous `Record<string, ToolResultPreview>` holds every
  // tool's entry. `renderResultPreview` is the one place that feeds it the parsed output.
  render: (result: never, variant: PreviewVariant) => ReactNode;
};

// The props every top-level result widget takes: the tool's parsed result payload plus the
// variant to render — the result-side counterpart of variant.tsx's PreviewProps.
export type ResultPreviewProps<Result> = { result: Result; variant: PreviewVariant };

/** Bind a tool's result schema to the widget that renders it. `Widget` takes the schema's
 * inferred `result` plus the `variant`. Pass the component directly — this builds the
 * `<Widget/>` element (a real child component, so the widget's own hooks work). */
export function defineResultPreview<S extends z.ZodTypeAny>(
  schema: S,
  Widget: (props: ResultPreviewProps<z.infer<S>>) => ReactNode
): ToolResultPreview<S> {
  const render = (result: z.infer<S>, variant: PreviewVariant): ReactNode => (
    <Widget result={result} variant={variant} />
  );
  return { schema, render: render as ToolResultPreview["render"] };
}

/** Parse `payload` with the entry's schema and render; `null` on a schema mismatch, so the
 * caller falls back to raw JSON (a server upgrade can reshape results under the console). */
export function renderResultPreview(
  preview: ToolResultPreview,
  payload: unknown,
  variant: PreviewVariant
): ReactNode | null {
  const parsed = preview.schema.safeParse(payload);
  // parsed.data is `unknown` (the schema is type-erased to ZodTypeAny in storage); the matching
  // `defineResultPreview` bound this render to exactly this schema's output, so the cast is sound.
  return parsed.success ? preview.render(parsed.data as never, variant) : null;
}

// A stored tool result is the executed call's CallToolResult JSON (mcp_approval.py's
// `_mcp_result_to_json`). Only the envelope fields the unwrap reads are typed; everything
// else passes through untouched.
const zStoredCallToolResult = z.looseObject({
  content: z.array(z.looseObject({ type: z.string(), text: z.string().optional() })).optional(),
  isError: z.boolean().optional(),
  structuredContent: z.record(z.string(), z.unknown()).nullish(),
  _meta: z.looseObject({ fastmcp: z.looseObject({ wrap_result: z.boolean().optional() }).optional() }).nullish(),
});

/** The tool's own return value, dug out of a stored CallToolResult envelope: prefer
 * `structuredContent` (un-wrapping FastMCP's `{"result": …}` envelope for non-dict returns,
 * flagged by `_meta.fastmcp.wrap_result`), else parse a single JSON text block. `null` when
 * there is no structured payload to dispatch on — including error results, whose message
 * already renders through the card's error line. */
export function unwrapToolResult(resultJson: unknown): unknown {
  const parsed = zStoredCallToolResult.safeParse(resultJson);
  if (!parsed.success || parsed.data.isError) return null;
  const structured = parsed.data.structuredContent;
  if (structured != null) {
    // Un-wrap only FastMCP's exact wrap shape (flag set, `result` the sole key) so a tool that
    // genuinely returns a `result` field isn't mangled.
    const wrapped =
      parsed.data._meta?.fastmcp?.wrap_result === true &&
      "result" in structured &&
      Object.keys(structured).length === 1;
    return wrapped ? structured.result : structured;
  }
  const blocks = parsed.data.content ?? [];
  if (blocks.length === 1 && blocks[0].type === "text" && blocks[0].text !== undefined) {
    try {
      return JSON.parse(blocks[0].text);
    } catch {
      // A prose text result is a foreseeable state, not a failure — the raw-JSON fallback
      // still shows it in full, so there is nothing to log.
      return null;
    }
  }
  return null;
}
