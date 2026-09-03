import { TypographyStylesProvider } from "@mantine/core";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { useMemo } from "react";

// The agent's own text, so the source is untrusted: sanitize the rendered HTML down to the tags a
// transcript needs, with no attributes that can navigate or script.
const marked = new Marked({ gfm: true, breaks: true });
const ALLOWED_TAGS = [
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

/**
 * Markdown as HTML, styled by Mantine's `TypographyStylesProvider` — without it Mantine's reset
 * leaves paragraphs and lists with no spacing of their own.
 */
export function Markdown({ source }: { source: string }): JSX.Element {
  const html = useMemo(() => {
    const rendered = marked.parse(source);
    if (typeof rendered !== "string") throw new Error("asynchronous Markdown rendering is not supported");
    return DOMPurify.sanitize(rendered, {
      ALLOWED_TAGS,
      ALLOWED_ATTR: ["align", "href", "title"],
      ALLOW_DATA_ATTR: false,
    });
  }, [source]);
  return (
    <TypographyStylesProvider>
      <div dangerouslySetInnerHTML={{ __html: html }} />
    </TypographyStylesProvider>
  );
}
