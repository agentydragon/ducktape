import { Code } from "@mantine/core";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import json from "highlight.js/lib/languages/json";
import { useMemo } from "react";

import "./json_view.css";

// The core build plus one grammar: the package's default entry point registers every language it
// ships, which is most of a megabyte for the one we use.
hljs.registerLanguage("json", json);

/** True when `payload` announces itself as JSON structurally (starts with `{`/`[`) -- for a caller
 * holding a string that might not be JSON (tool output, a streamed partial), so a plain-text/log
 * payload isn't tokenized as if it were JSON punctuation. */
export function looksLikeJson(payload: string): boolean {
  return /^\s*[[{]/.test(payload);
}

/** Sanitized syntax-highlighted HTML for a JSON string, safe for `dangerouslySetInnerHTML`.
 * highlight.js escapes the text it wraps; DOMPurify is the belt, since JSON payloads here
 * routinely come from untrusted agent/tool output. */
export function highlightJson(text: string): string {
  return DOMPurify.sanitize(hljs.highlight(text, { language: "json" }).value, {
    ALLOWED_TAGS: ["span"],
    ALLOWED_ATTR: ["class"],
  });
}

/** True for the MCP tool-result content-block shape, `{content: [<json-encoded string>, ...]}` --
 * a structural check, not a type assumption: not every `execution.result` is an MCP tool result. */
function isContentBlock(value: unknown): value is { content: unknown[] } {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Array.isArray((value as { content?: unknown }).content)
  );
}

function unwrapContentEntry(entry: unknown): unknown {
  if (typeof entry !== "string") return unwrapMcpContent(entry);
  try {
    // The entry is itself a further-nested content block often enough (a tool call embedded in a
    // tool result) to warrant unwrapping the parse, not just pretty-printing it.
    return unwrapMcpContent(JSON.parse(entry));
  } catch {
    return entry;
  }
}

/**
 * Recursively parses a JSON-encoded string sitting inside an MCP content-block shape's `content`
 * array -- at any depth in `value`, not only at the top -- so it renders as structure instead of
 * one escaped-quote wall of text. A value that doesn't match the shape anywhere passes through
 * unchanged, since only MCP-tool-call Actions produce it.
 */
export function unwrapMcpContent(value: unknown): unknown {
  if (isContentBlock(value)) return { ...value, content: value.content.map(unwrapContentEntry) };
  if (Array.isArray(value)) return value.map(unwrapMcpContent);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, unwrapMcpContent(entry)]));
  }
  return value;
}

/** A JSON-serializable value, rendered as syntax-highlighted, sanitized JSON inside a `<Code
 * block>`. Untrusted-content safe: see `highlightJson`. */
export function JsonView({ value }: { value: unknown }): JSX.Element {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  const html = useMemo(() => highlightJson(text), [text]);
  return (
    <Code block className="agentplane-hljs">
      <span dangerouslySetInnerHTML={{ __html: html }} />
    </Code>
  );
}

/** A pre-serialized string rendered as syntax-highlighted JSON when it looks like JSON, else as
 * plain text -- for a caller holding text that might not be JSON (tool output, a streamed partial
 * arguments blob) rather than a value it should serialize itself. */
export function HighlightedText({ text }: { text: string }): JSX.Element {
  const html = useMemo(() => (looksLikeJson(text) ? highlightJson(text) : null), [text]);
  return (
    <Code block className="agentplane-hljs">
      {html === null ? text : <span dangerouslySetInnerHTML={{ __html: html }} />}
    </Code>
  );
}
