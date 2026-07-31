// A registry entry for one tool's *combined* argument + result rendering — used when a tool's
// pending and finished states are naturally one evolving view (a creation tool whose result
// mostly restates its own arguments) rather than two independent widgets (entry.tsx's args-only
// registry paired with result_entry.tsx's result-only registry). The registry (index.tsx)
// safeParses args once and, when a result is present, tries the result schema too. Depends on
// result_entry.tsx for the same CallToolResult-envelope unwrap every result widget uses, so a
// pending call, an error result, and a schema mismatch all resolve to `result: undefined` —
// the widget's own "pending" branch — rather than losing the (valid) args rendering.
import type { ReactNode } from "react";
import type { z } from "zod";

import { unwrapToolResult } from "./result_entry.tsx";
import type { PreviewVariant } from "./vocabulary.tsx";

export type ToolCallPreview<
  ArgsSchema extends z.ZodTypeAny = z.ZodTypeAny,
  ResultSchema extends z.ZodTypeAny = z.ZodTypeAny,
> = {
  argsSchema: ArgsSchema;
  resultSchema: ResultSchema;
  // Stored with `never` args/result: the schema-specific types are checked at the
  // `defineCallPreview` call site and erased here so a heterogeneous `Record<string,
  // ToolCallPreview>` holds every tool's entry. `renderCallPreview` is the one place that feeds
  // them the parsed output.
  render: (args: never, result: never | undefined, variant: PreviewVariant) => ReactNode;
};

/** Bind a tool's argument and result schemas to one widget that renders both states. `Widget`
 * takes the parsed args, the parsed result (`undefined` while pending, for an error result, or on
 * a result schema mismatch), and the variant. */
export function defineCallPreview<ArgsSchema extends z.ZodTypeAny, ResultSchema extends z.ZodTypeAny>(
  argsSchema: ArgsSchema,
  resultSchema: ResultSchema,
  Widget: (props: {
    args: z.infer<ArgsSchema>;
    result: z.infer<ResultSchema> | undefined;
    variant: PreviewVariant;
  }) => ReactNode
): ToolCallPreview<ArgsSchema, ResultSchema> {
  const render = (
    args: z.infer<ArgsSchema>,
    result: z.infer<ResultSchema> | undefined,
    variant: PreviewVariant
  ): ReactNode => <Widget args={args} result={result} variant={variant} />;
  return {
    argsSchema,
    resultSchema,
    render: render as ToolCallPreview["render"],
  };
}

/** Parse `args` (required) and, when `rawResult` carries a structured payload, the result too;
 * `null` when `args` doesn't parse (the caller falls back to raw JSON), matching entry.tsx's
 * `renderPreview` contract. A `rawResult` that's absent, an error result, or doesn't match the
 * result schema all render as `result: undefined` instead of failing the whole card. */
export function renderCallPreview(
  preview: ToolCallPreview,
  args: Record<string, unknown>,
  rawResult: unknown,
  variant: PreviewVariant
): ReactNode | null {
  const parsedArgs = preview.argsSchema.safeParse(args);
  if (!parsedArgs.success) return null;
  const unwrapped = rawResult == null ? null : unwrapToolResult(rawResult);
  const parsedResult = unwrapped == null ? undefined : preview.resultSchema.safeParse(unwrapped);
  const result = parsedResult?.success ? parsedResult.data : undefined;
  return preview.render(parsedArgs.data as never, result as never, variant);
}
