import { type JSX, useMemo } from "react";

import { looksLikeJson } from "./transcript/text_block";
import { CodeBlock, type CodeBlockSurface } from "./code_block";

/** A JSON-serializable value, rendered by the shared read-only code widget. */
export function JsonView({ value, surface = "framed" }: { value: unknown; surface?: CodeBlockSurface }): JSX.Element {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  return <CodeBlock text={text} language="json" surface={surface} />;
}

/** A pre-serialized string rendered as JSON when it looks like JSON, else as plain text -- for a
 * caller holding text that might not be JSON (tool output, a streamed partial arguments blob). */
export function HighlightedText({
  text,
  surface = "framed",
  releaseWhenFar = false,
}: {
  text: string;
  surface?: CodeBlockSurface;
  releaseWhenFar?: boolean;
}): JSX.Element {
  return looksLikeJson(text) ? (
    <CodeBlock text={text} language="json" surface={surface} releaseWhenFar={releaseWhenFar} />
  ) : (
    <CodeBlock text={text} surface={surface} releaseWhenFar={releaseWhenFar} />
  );
}
