import { File, FileText, FileCode, FileJson, Settings, type Icon } from "lucide-svelte";

/**
 * Mapping from file extension to programming language for syntax highlighting.
 * Used with highlight.js.
 */
export const FILE_EXTENSION_TO_LANGUAGE: Record<string, string> = {
  js: "javascript",
  ts: "typescript",
  jsx: "javascript",
  tsx: "typescript",
  py: "python",
  rb: "ruby",
  java: "java",
  c: "c",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  h: "c",
  hpp: "cpp",
  go: "go",
  rs: "rust",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  xml: "xml",
  html: "html",
  css: "css",
  scss: "scss",
  sql: "sql",
  md: "markdown",
  txt: "plaintext",
};

/**
 * Detect programming language from file path based on extension.
 */
export function detectLanguage(filePath: string): string {
  const ext = filePath.split(".").pop()?.toLowerCase();
  return FILE_EXTENSION_TO_LANGUAGE[ext || ""] || "plaintext";
}

/**
 * Get appropriate Lucide icon component for a file based on its extension.
 */
export function getFileIcon(filename: string): typeof Icon {
  const ext = filename.split(".").pop()?.toLowerCase();

  if (ext === "json" || ext === "yaml" || ext === "yml") {
    return FileJson;
  }

  if (
    ext === "js" ||
    ext === "ts" ||
    ext === "jsx" ||
    ext === "tsx" ||
    ext === "py" ||
    ext === "rb" ||
    ext === "java" ||
    ext === "c" ||
    ext === "cpp" ||
    ext === "go" ||
    ext === "rs"
  ) {
    return FileCode;
  }

  if (ext === "md" || ext === "txt") {
    return FileText;
  }

  if (ext === "config" || ext === "conf" || ext === "cfg") {
    return Settings;
  }

  return File;
}
