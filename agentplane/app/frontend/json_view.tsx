import { Code } from "@mantine/core";
import { type JSX, useMemo } from "react";

import { HighlightedCode } from "./syntax_highlight";

/** True when `payload` announces itself as JSON structurally (starts with `{`/`[`) -- for a caller
 * holding a string that might not be JSON (tool output, a streamed partial), so a plain-text/log
 * payload isn't tokenized as if it were JSON punctuation. */
export function looksLikeJson(payload: string): boolean {
  return /^\s*[[{]/.test(payload);
}

/** A JSON-serializable value, rendered as syntax-highlighted, sanitized JSON inside a `<Code
 * block>`. Untrusted-content safe: see `highlight` in `syntax_highlight.tsx`. */
export function JsonView({ value }: { value: unknown }): JSX.Element {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  return <HighlightedCode text={text} language="json" />;
}

/** A pre-serialized string rendered as syntax-highlighted JSON when it looks like JSON, else as
 * plain text -- for a caller holding text that might not be JSON (tool output, a streamed partial
 * arguments blob) rather than a value it should serialize itself. */
export function HighlightedText({ text }: { text: string }): JSX.Element {
  return looksLikeJson(text) ? (
    <HighlightedCode text={text} language="json" />
  ) : (
    <Code block className="agentplane-hljs">
      {text}
    </Code>
  );
}
