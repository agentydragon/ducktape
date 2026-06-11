import hljs from "highlight.js";

/**
 * Highlight source code and return array of highlighted lines.
 * Preserves multi-line syntax state by highlighting the entire file at once.
 *
 * @param lines - Array of source code lines to highlight
 * @param language - Programming language name (e.g., 'javascript', 'python')
 * @returns Array of HTML strings with syntax highlighting markup
 */
export function highlightLines(lines: string[], language: string): string[] {
  const fullCode = lines.join("\n");

  try {
    const result = hljs.highlight(fullCode, { language, ignoreIllegals: true });
    return result.value.split("\n");
  } catch {
    return lines;
  }
}
