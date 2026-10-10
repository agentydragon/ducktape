// A registry entry for one Action's arguments: the zod schema they must parse with, paired with a
// widget over the parsed, typed value. The registry (index.tsx) safeParses once per dispatch and
// hands the widget already-validated data. Leaf module, so index.tsx and every group's module import
// it without a cycle.
import type { ReactNode } from "react";
import type { z } from "zod";

export type ArgumentsPreview<S extends z.ZodType = z.ZodType> = {
  schema: S;
  // Stored with `never` args: the schema-specific type is checked where `definePreview` is called
  // and erased here, so one registry holds every Action's entry. `renderPreview` is the one place
  // that feeds it the parsed output.
  render: (args: never) => ReactNode;
};

/** A call whose request and returned value form one view, such as creating a draft. */
export type CallPreview<ArgsSchema extends z.ZodType = z.ZodType, ResultSchema extends z.ZodType = z.ZodType> = {
  argumentSchema: ArgsSchema;
  resultSchema: ResultSchema;
  render: (args: never, result: never | undefined) => ReactNode;
};

/** The props every arguments widget takes: the Action's parsed arguments. */
export type PreviewProps<Args> = { args: Args };

/** The props a combined call widget takes. `result` is absent while the call is pending or failed. */
export type CallPreviewProps<Args, Result> = { args: Args; result: Result | undefined };

/** Bind an Action's argument schema to the widget that draws it. Pass the component itself: this
 * builds a `<Widget/>` element, a real child component, so the widget's own hooks work. */
export function definePreview<S extends z.ZodType>(
  schema: S,
  Widget: (props: PreviewProps<z.infer<S>>) => ReactNode
): ArgumentsPreview<S> {
  const render = (args: z.infer<S>): ReactNode => <Widget args={args} />;
  return { schema, render: render as ArgumentsPreview["render"] };
}

/** Bind both sides of a call to one widget, checking each side against its action-local schema. */
export function defineCallPreview<ArgsSchema extends z.ZodType, ResultSchema extends z.ZodType>(
  argumentSchema: ArgsSchema,
  resultSchema: ResultSchema,
  Widget: (props: CallPreviewProps<z.infer<ArgsSchema>, z.infer<ResultSchema>>) => ReactNode
): CallPreview<ArgsSchema, ResultSchema> {
  const render = (args: z.infer<ArgsSchema>, result: z.infer<ResultSchema> | undefined): ReactNode => (
    <Widget args={args} result={result} />
  );
  return {
    argumentSchema,
    resultSchema,
    render: render as CallPreview["render"],
  };
}

/** Parse `args` with the entry's schema and render; `null` on a mismatch, so the caller falls back to
 * their JSON. Arguments are not checked against the tool until it runs, so a pending call's may not
 * fit. */
export function renderPreview(preview: ArgumentsPreview, args: unknown): ReactNode | null {
  const parsed = preview.schema.safeParse(args);
  // `definePreview` bound this render to exactly this schema's output, so the cast is sound.
  return parsed.success ? preview.render(parsed.data as never) : null;
}
