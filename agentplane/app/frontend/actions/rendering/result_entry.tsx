// A registry entry for one Action's result: the zod schema the tool's returned value must parse with,
// paired with a widget over the parsed, typed value -- the result-side mirror of entry.tsx. Leaf
// module, so index.tsx and every group's module import it without a cycle.
import type { ReactNode } from "react";
import type { z } from "zod";

export type ResultPreview<S extends z.ZodType = z.ZodType> = {
  schema: S;
  // Stored with `never` result, for the reason entry.tsx's `render` is.
  render: (result: never, context?: ResultPreviewContext) => ReactNode;
};

/** The props every result widget takes: the tool's parsed return value. */
export interface ResultPreviewContext {
  requestId: string;
  arguments: unknown;
}

export type ResultPreviewProps<Result> = { result: Result; context?: ResultPreviewContext };

/** Bind the schema of an Action's returned value to the widget that draws it. */
export function defineResultPreview<S extends z.ZodType>(
  schema: S,
  Widget: (props: ResultPreviewProps<z.infer<S>>) => ReactNode
): ResultPreview<S> {
  const render = (result: z.infer<S>, context?: ResultPreviewContext): ReactNode => (
    <Widget result={result} context={context} />
  );
  return { schema, render: render as ResultPreview["render"] };
}

/** Parse `value` with the entry's schema and render; `undefined` on a mismatch, so the caller falls back
 * to the generic rendering. A server upgrade can reshape what a tool returns. */
export function renderResultPreview(
  preview: ResultPreview,
  value: unknown,
  context?: ResultPreviewContext
): ReactNode | null | undefined {
  const parsed = preview.schema.safeParse(value);
  return parsed.success ? preview.render(parsed.data as never, context) : undefined;
}
