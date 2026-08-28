import DOMPurify from "dompurify";
import { Marked } from "marked";
import { useMemo } from "react";

const markdown = new Marked({ gfm: true, breaks: true });
const MARKDOWN_TAGS = [
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
  "strong",
  "table",
  "tbody",
  "td",
  "th",
  "thead",
  "tr",
  "ul",
];

export function sanitizedMarkdown(source: string): string {
  const rendered = markdown.parse(source);
  if (typeof rendered !== "string") throw new Error("asynchronous Markdown rendering is not supported");
  return DOMPurify.sanitize(rendered, {
    ALLOWED_TAGS: MARKDOWN_TAGS,
    ALLOWED_ATTR: ["align", "href", "title"],
    ALLOW_DATA_ATTR: false,
  });
}

export function Markdown({ source, className = "" }: { source: string; className?: string }): JSX.Element {
  const html = useMemo(() => sanitizedMarkdown(source), [source]);
  return <div className={`md ${className}`.trim()} dangerouslySetInnerHTML={{ __html: html }} />;
}
