// A registry entry for one Action's result: the zod schema the tool's returned value must parse with,
// paired with a widget over the parsed, typed value -- the result-side mirror of entry.tsx. Leaf
// module, so index.tsx and every group's module import it without a cycle.
import type { ReactNode } from "react";
import type { z } from "zod";

export type ResultPreview<S extends z.ZodType = z.ZodType> = {
  schema: S;
  // Stored with `never` result, for the reason entry.tsx's `render` is.
  render: (result: never) => ReactNode;
};

/** The props every result widget takes: the tool's parsed return value. */
export type ResultPreviewProps<Result> = { result: Result };

/** Bind the schema of an Action's returned value to the widget that draws it. */
export function defineResultPreview<S extends z.ZodType>(
  schema: S,
  Widget: (props: ResultPreviewProps<z.infer<S>>) => ReactNode
): ResultPreview<S> {
  const render = (result: z.infer<S>): ReactNode => <Widget result={result} />;
  return { schema, render: render as ResultPreview["render"] };
}

/** Parse `value` with the entry's schema and render; `null` on a mismatch, so the caller falls back
 * to the generic rendering. A server upgrade can reshape what a tool returns. */
export function renderResultPreview(preview: ResultPreview, value: unknown): ReactNode | null {
  const parsed = preview.schema.safeParse(value);
  return parsed.success ? preview.render(parsed.data as never) : null;
}
