import { type JSX, useMemo } from "react";

import { CodeBlock } from "./code_block";

/** True when `payload` announces itself as JSON structurally (starts with `{`/`[`) -- for a caller
 * holding a string that might not be JSON (tool output, a streamed partial), so a plain-text/log
 * payload isn't tokenized as if it were JSON punctuation. */
export function looksLikeJson(payload: string): boolean {
  return /^\s*[[{]/.test(payload);
}

/** A JSON-serializable value, rendered by the shared read-only code widget. */
export function JsonView({ value }: { value: unknown }): JSX.Element {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  return <CodeBlock text={text} language="json" />;
}

/** A pre-serialized string rendered as JSON when it looks like JSON, else as plain text -- for a
 * caller holding text that might not be JSON (tool output, a streamed partial arguments blob). */
export function HighlightedText({ text }: { text: string }): JSX.Element {
  return looksLikeJson(text) ? <CodeBlock text={text} language="json" /> : <CodeBlock text={text} />;
}
