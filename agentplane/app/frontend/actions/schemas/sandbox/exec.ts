/** Settings that change how a sandbox script runs; the opened card and the notification share them. */
export function sandboxExecNotes(args: {
  cwd?: string | null | undefined;
  timeout_seconds: number;
  max_output_bytes: number;
}): string[] {
  return [
    ...(args.cwd == null ? [] : [`Working directory ${args.cwd}`]),
    `Timeout ${args.timeout_seconds} s`,
    `Output limit ${args.max_output_bytes} bytes per stream`,
  ];
}
