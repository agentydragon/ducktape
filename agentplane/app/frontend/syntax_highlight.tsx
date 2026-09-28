import { Code } from "@mantine/core";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import yaml from "highlight.js/lib/languages/yaml";
import { type JSX, useMemo } from "react";

import "./syntax_highlight.css";

// The core build plus the grammars used here: the package's default entry point registers every
// language it ships, which is most of a megabyte. This is a deliberately curated set for what a
// coding agent's transcripts actually contain (tool-call JSON/bash, plus the languages this repo
// is written in) -- add a grammar only when it earns its bundle-size cost.
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("python", python);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("yaml", yaml);

export type Language = "bash" | "javascript" | "json" | "python" | "typescript" | "yaml";

const REGISTERED_LANGUAGES: ReadonlySet<string> = new Set<Language>([
  "bash",
  "javascript",
  "json",
  "python",
  "typescript",
  "yaml",
]);

/** Whether `language` names one of the grammars registered above -- the only values `highlight`
 * accepts. Callers with a free-form language token (e.g. a Markdown fence's info string) narrow
 * through this before calling `highlight`, rather than risking its "Unknown language" throw. */
export function isRegisteredLanguage(language: string): language is Language {
  return REGISTERED_LANGUAGES.has(language);
}

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
