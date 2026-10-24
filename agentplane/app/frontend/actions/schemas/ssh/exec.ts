import { zSshExecArguments as generatedArguments, zSshExecResult as generatedResult } from "./exec.zod";

// Zod objects strip unknown fields by default. These schemas validate entire tool payloads, so a
// widget must fall back to JSON rather than silently hiding fields it does not know how to show.
export const zSshExecArguments: ReturnType<typeof generatedArguments.strict> = generatedArguments.strict();
export const zSshExecResult: ReturnType<typeof generatedResult.strict> = generatedResult.strict();

/** Settings that change how an SSH command runs; the opened card and the notification share them. */
export function sshExecNotes(args: { timeout_seconds?: number | null | undefined }): string[] {
  return args.timeout_seconds == null ? [] : [`Timeout ${args.timeout_seconds} s`];
}
