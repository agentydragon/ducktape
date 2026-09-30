import { Text } from "@mantine/core";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { createElement, type JSX, type ReactNode, useMemo } from "react";

import { highlight, isRegisteredLanguage } from "./syntax_highlight";

import "./markdown.css";

export const STREAMING_CURSOR = "|";

const HTML_ESCAPES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escapeHtml = (text: string): string => text.replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
const INLINE_TAGS = new Set(["A", "CODE", "DEL", "EM", "SPAN", "STRONG"]);
const VOID_TAGS = new Set(["BR", "HR"]);

// The agent's own text, so the source is untrusted: sanitize the rendered HTML down to the tags a
// transcript needs, with no attributes that can navigate or script. The `code` renderer override
// highlights a fenced block by its declared language when that language is one of
// `syntax_highlight.tsx`'s registered grammars; an unrecognized or absent language, or a raw
// (non-fenced) code span, falls back to plain, escaped text rather than guessing.
const marked = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    code({ text, lang }) {
      // marked's own default renderer normalizes a fence's token text to end with exactly one
      // newline before the closing tag; matched here since this override replaces that renderer.
      const code = `${text.replace(/\n$/, "")}\n`;
      const language = lang?.match(/^\S*/)?.[0];
      if (language !== undefined && isRegisteredLanguage(language)) {
        return `<pre><code class="agentplane-hljs">${highlight(code, language)}</code></pre>`;
      }
      return `<pre><code>${escapeHtml(code)}</code></pre>`;
    },
  },
});
const ALLOWED_TAGS = [
  // keep-sorted start
  "a",
  "blockquote",
  "br",
  "code",
  "del",
  "em",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "li",
  "ol",
  "p",
  "pre",
  "span",
  "strong",
  "table",
  "tbody",
  "td",
  "th",
  "thead",
  "tr",
  "ul",
  // keep-sorted end
];

const STREAMING_CURSOR_MARKER = "data-agentplane-streaming-cursor";
const STREAMING_CURSOR_KEY = "agentplane-streaming-cursor";

function appendStreamingCursor(content: DocumentFragment): void {
  const cursor = document.createElement("span");
  cursor.className = "agentplane-streaming-cursor";
  cursor.setAttribute("role", "img");
  cursor.setAttribute("aria-label", "Streaming");
  cursor.setAttribute("data-character", STREAMING_CURSOR);
  cursor.setAttribute(STREAMING_CURSOR_MARKER, "");

  // Marked leaves whitespace between its top-level blocks. Skip whitespace-only nodes and descend
  // through block containers, but leave the cursor beside the final inline element. That keeps its
  // React parent stable while inline Markdown (for example, an unfinished emphasis span) changes.
  const lastContentChild = (parent: ParentNode): ChildNode | null =>
    [...parent.childNodes]
      .reverse()
      .find((node) => node.nodeType !== Node.TEXT_NODE || Boolean(node.textContent?.trim())) ?? null;
  let parent: ParentNode = content;
  let last = lastContentChild(content);
  while (last instanceof Element && !INLINE_TAGS.has(last.tagName) && !VOID_TAGS.has(last.tagName)) {
    parent = last;
    const child = lastContentChild(last);
    if (!child) {
      last = null;
      break;
    }
    last = child;
  }

  if (last?.nodeType === Node.TEXT_NODE) {
    const text = last.textContent ?? "";
    const trailingWhitespace = text.match(/\s+$/)?.[0] ?? "";
    if (trailingWhitespace) {
      last.textContent = text.slice(0, -trailingWhitespace.length);
      const whitespace = document.createTextNode(trailingWhitespace);
      parent.insertBefore(cursor, last.nextSibling);
      parent.insertBefore(whitespace, cursor.nextSibling);
    } else parent.insertBefore(cursor, last.nextSibling);
  } else if (last?.parentNode) parent.insertBefore(cursor, last.nextSibling);
  else parent.append(cursor);
}

function toReactNode(node: ChildNode, key: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (!(node instanceof Element)) return null;

  const cursor = node.hasAttribute(STREAMING_CURSOR_MARKER);
  const props = Object.fromEntries(
    [...node.attributes]
      .filter((attribute) => attribute.name !== STREAMING_CURSOR_MARKER)
      .map((attribute) => [attribute.name === "class" ? "className" : attribute.name, attribute.value])
  );
  const children = [...node.childNodes].map((child, index) => toReactNode(child, `${key}.${index}`));
  // This key stays fixed as text changes, so React reuses the cursor DOM node through streaming.
  return createElement(node.tagName.toLowerCase(), { ...props, key: cursor ? STREAMING_CURSOR_KEY : key }, ...children);
}

/**
 * Markdown passes through Marked and DOMPurify before its allowlisted fragment becomes React nodes.
 * This lets React reconcile the final cursor across body updates instead of replacing its DOM node
 * with each sanitized HTML string. The local class supplies the small amount of prose styling this
 * transcript needs; Mantine 9 removed the old `TypographyStylesProvider` wrapper. Render through
 * `Text` (at its default `md` size, matching `VerbatimText`) rather than a bare `div`, so this reads
 * `theme.fontSizes`/`lineHeights` like the rest of the app; `component="div"` because the content can
 * contain block-level tags that a `Text`'s default `<p>` can't legally contain.
 */
export function Markdown({ source, streaming = false }: { source: string; streaming?: boolean }): JSX.Element {
  const content = useMemo(() => {
    const rendered = marked.parse(source);
    if (typeof rendered !== "string") throw new Error("asynchronous Markdown rendering is not supported");
    const sanitized = DOMPurify.sanitize(rendered, {
      ALLOWED_TAGS,
      // `class` is here for the highlighter's `hljs-*` spans (see the `code` renderer above);
      // DOMPurify's allowlist isn't per-tag, so raw HTML in the source could also carry a `class`
      // on another allowed tag -- still just CSS, never navigation or script, so it stays within
      // this module's stated sanitizing goal.
      ALLOWED_ATTR: ["align", "class", "href", "title"],
      ALLOW_DATA_ATTR: false,
    });
    const template = document.createElement("template");
    template.innerHTML = sanitized;
    if (streaming) appendStreamingCursor(template.content);
    return [...template.content.childNodes].map((node, index) => toReactNode(node, String(index)));
  }, [source, streaming]);
  return (
    <Text component="div" className="agentplane-markdown">
      {content}
    </Text>
  );
}
