import { Text } from "@mantine/core";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { createElement, type JSX, type ReactNode, useMemo } from "react";

import { CodeBlock, isRegisteredLanguage } from "./code_block";

import "./markdown.css";

export const STREAMING_CURSOR = "|";

const HTML_ESCAPES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escapeHtml = (text: string): string => text.replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
const INLINE_TAGS = new Set(["A", "CODE", "DEL", "EM", "SPAN", "STRONG"]);
const VOID_TAGS = new Set(["BR", "HR"]);

// The agent's own text, so the source is untrusted: sanitize the rendered HTML down to the tags a
// transcript needs, with no attributes that can navigate or script. The `code` renderer override
// marks fenced blocks so they can be mounted as the shared read-only CodeMirror widget after
// sanitization; unrecognized or absent languages stay plain text rather than being guessed.
const marked = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    code({ text, lang }) {
      // marked's own default renderer normalizes a fence's token text to end with exactly one
      // newline before the closing tag; matched here since this override replaces that renderer.
      const code = `${text.replace(/\n$/, "")}\n`;
      const language = lang?.match(/^\S*/)?.[0];
      const languageClass = language !== undefined && isRegisteredLanguage(language) ? ` language-${language}` : "";
      return `<pre><code class="agentplane-code-fence${languageClass}">${escapeHtml(code)}</code></pre>`;
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

function codeFence(node: Element, key: string): ReactNode | null {
  if (node.tagName !== "PRE") return null;
  const code = node.firstElementChild;
  if (code?.tagName !== "CODE" || !code.classList.contains("agentplane-code-fence")) return null;

  const languageClass = Array.from(code.classList).find((name) => name.startsWith("language-"));
  const language = languageClass?.slice("language-".length);
  const cursor = code.querySelector(`[${STREAMING_CURSOR_MARKER}]`);
  let textBeforeCursor = "";
  const findCursor = (parent: ParentNode): boolean => {
    for (const child of parent.childNodes) {
      if (child === cursor) return true;
      if (child.nodeType === Node.TEXT_NODE) textBeforeCursor += child.textContent ?? "";
      else if (child instanceof Element && findCursor(child)) return true;
    }
    return false;
  };
  const cursorOffset = cursor && findCursor(code) ? textBeforeCursor.length : null;

  return createElement(CodeBlock, {
    key,
    text: code.textContent ?? "",
    ...(language !== undefined && isRegisteredLanguage(language) ? { language } : {}),
    ...(cursorOffset !== null ? { streamingCursorOffset: cursorOffset } : {}),
  });
}

function toReactNode(node: ChildNode, key: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (!(node instanceof Element)) return null;

  const renderedCodeFence = codeFence(node, key);
  if (renderedCodeFence !== null) return renderedCodeFence;

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
      // `class` marks generated code fences and lets token CSS style permitted inline elements.
      // DOMPurify's allowlist isn't per-tag, so raw HTML in the source can also carry a `class` on
      // another allowed tag -- still just CSS, never navigation or script.
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
