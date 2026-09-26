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

/** The props every arguments widget takes: the Action's parsed arguments. */
export type PreviewProps<Args> = { args: Args };

/** Bind an Action's argument schema to the widget that draws it. Pass the component itself: this
 * builds a `<Widget/>` element, a real child component, so the widget's own hooks work. */
export function definePreview<S extends z.ZodType>(
  schema: S,
  Widget: (props: PreviewProps<z.infer<S>>) => ReactNode
): ArgumentsPreview<S> {
  const render = (args: z.infer<S>): ReactNode => <Widget args={args} />;
  return { schema, render: render as ArgumentsPreview["render"] };
}

/** Parse `args` with the entry's schema and render; `null` on a mismatch, so the caller falls back to
 * their JSON. Arguments are not checked against the tool until it runs, so a pending call's may not
 * fit. */
export function renderPreview(preview: ArgumentsPreview, args: unknown): ReactNode | null {
  const parsed = preview.schema.safeParse(args);
  // `definePreview` bound this render to exactly this schema's output, so the cast is sound.
  return parsed.success ? preview.render(parsed.data as never) : null;
}
