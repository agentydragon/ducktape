import DOMPurify from "dompurify";
import { marked } from "marked";

// Render an item's Markdown `body` to sanitized HTML. `marked` is a CommonMark
// renderer; DOMPurify strips any embedded HTML so a body can't inject markup.
export function renderMarkdown(md: string): string {
  return DOMPurify.sanitize(marked.parse(md, { async: false }) as string);
}
