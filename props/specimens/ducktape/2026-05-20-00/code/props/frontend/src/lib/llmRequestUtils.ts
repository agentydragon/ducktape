export function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

/** Try to parse a string as JSON; return original value on failure. */
export function tryParseJson(s: unknown): unknown {
  if (typeof s !== "string") return s;
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

/** Extract plain text from a content array of {text: string} parts. */
export function getContentText(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .filter((p): p is { text: string } => typeof (p as Record<string, unknown>)?.text === "string")
    .map((p) => p.text)
    .join("\n");
}

export const roleBadgeClass: Record<string, string> = {
  system: "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200",
  user: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300",
  assistant: "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300",
};
