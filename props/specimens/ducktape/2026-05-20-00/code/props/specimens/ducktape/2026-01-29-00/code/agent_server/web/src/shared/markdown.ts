import hljs from "highlight.js/lib/common";
import cppLang from "highlight.js/lib/languages/cpp";
import { Marked } from "marked";
import { markedHighlight } from "marked-highlight";

try {
  if (!hljs.getLanguage("cpp")) hljs.registerLanguage("cpp", cppLang);
} catch {
  // Ignore language registration errors
}

function normalizeLang(lang?: string): string | undefined {
  if (!lang) return undefined;
  const l = String(lang).toLowerCase();
  const aliases: Record<string, string> = {
    "c++": "cpp",
    cpp: "cpp",
    "c#": "csharp",
    cs: "csharp",
    csharp: "csharp",
    js: "javascript",
    node: "javascript",
    nodejs: "javascript",
    ts: "typescript",
    py: "python",
    sh: "bash",
    shell: "bash",
    yml: "yaml",
    html: "xml",
    xhtml: "xml",
    plaintext: "text",
    text: "text",
  };
  return aliases[l] || l;
}

const md = new Marked();
md.use(
  markedHighlight({
    langPrefix: "hljs language-",
    highlight(code: string, lang?: string) {
      try {
        const n = normalizeLang(lang);
        if (n && hljs.getLanguage(n)) {
          return hljs.highlight(code, { language: n }).value;
        }
        return hljs.highlightAuto(code).value;
      } catch {
        return code;
      }
    },
  })
);

export function renderMarkdown(src: string): string {
  return String(md.parse(src ?? ""));
}
