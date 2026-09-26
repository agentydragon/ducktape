import { Code } from "@mantine/core";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import { type JSX, useMemo } from "react";

import "./syntax_highlight.css";

// The core build plus the grammars used here: the package's default entry point registers every
// language it ships, which is most of a megabyte.
hljs.registerLanguage("json", json);
hljs.registerLanguage("bash", bash);

export type Language = "json" | "bash";

/** Sanitized syntax-highlighted HTML for `text`, safe for `dangerouslySetInnerHTML`. highlight.js
 * escapes the text it wraps; DOMPurify is the belt, since what is highlighted here routinely comes
 * from untrusted agent or tool output. */
export function highlight(text: string, language: Language): string {
  return DOMPurify.sanitize(hljs.highlight(text, { language }).value, {
    ALLOWED_TAGS: ["span"],
    ALLOWED_ATTR: ["class"],
  });
}

/** `text` syntax-highlighted as `language` inside a `<Code block>`, wrapped where it sits.
 * Untrusted-content safe: see `highlight`. */
export function HighlightedCode({ text, language }: { text: string; language: Language }): JSX.Element {
  const html = useMemo(() => highlight(text, language), [text, language]);
  return (
    <Code block className="agentplane-hljs">
      <span dangerouslySetInnerHTML={{ __html: html }} />
    </Code>
  );
}
