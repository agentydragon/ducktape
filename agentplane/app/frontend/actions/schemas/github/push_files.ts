import { z } from "zod";

export type PushedFile = { path: string; content: string };

export type PushFilesArguments = {
  owner: string;
  repo: string;
  branch: string;
  message: string;
  files: PushedFile[];
};

// Unknown arguments fail closed rather than hiding a parameter the views do not show.
export const zPushFilesArguments: z.ZodType<PushFilesArguments> = z.strictObject({
  owner: z.string().min(1),
  repo: z.string().min(1),
  branch: z.string().min(1),
  message: z.string().min(1),
  files: z.array(z.strictObject({ path: z.string().min(1), content: z.string() })).min(1),
});

/** How many files a summary lists by path before counting the rest. */
export const PUSHED_FILES_SUMMARY_LIMIT = 3;

export function fileCount(count: number): string {
  return `${count} ${count === 1 ? "file" : "files"}`;
}

/** The UTF-8 size GitHub stores, in decimal units: "812 B", "1.9 KB", "72 KB". */
export function pushedFileSize(file: PushedFile): string {
  const bytes = new TextEncoder().encode(file.content).length;
  if (bytes < 1000) return `${bytes} B`;
  const [value, unit] = bytes < 1_000_000 ? [bytes / 1000, "KB"] : [bytes / 1_000_000, "MB"];
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
}

/** One line per listed file, then how many the summary leaves out. */
export function pushedFilesSummary(files: readonly PushedFile[]): string[] {
  const listed = files.slice(0, PUSHED_FILES_SUMMARY_LIMIT).map((file) => `${file.path} · ${pushedFileSize(file)}`);
  const rest = files.length - listed.length;
  return rest > 0 ? [...listed, `and ${rest} more ${rest === 1 ? "file" : "files"}`] : listed;
}
