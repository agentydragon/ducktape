import DOMPurify from "dompurify";
import { Marked } from "marked";
import { type JSX, useMemo } from "react";

import "./markdown.css";

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
 * Markdown as HTML. The local class supplies the small amount of prose styling this transcript
 * needs; Mantine 9 removed the old `TypographyStylesProvider` wrapper.
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
  return <div className="agentplane-markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}
