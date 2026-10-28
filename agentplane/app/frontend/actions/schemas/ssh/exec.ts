import { z } from "zod";
import argumentsSchema from "./exec.arguments.schema.json";
import resultSchema from "./exec.result.schema.json";
import type { SshExecArguments } from "./exec.arguments";
import type { SshExecResult } from "./exec.result";

type JsonSchema = Parameters<typeof z.fromJSONSchema>[0];
const generatedArguments = z.fromJSONSchema(argumentsSchema as unknown as JsonSchema) as z.ZodObject;
const generatedResult = z.fromJSONSchema(resultSchema as unknown as JsonSchema) as z.ZodObject;

// Zod objects strip unknown fields by default. These schemas validate entire tool payloads, so a
// widget must fall back to JSON rather than silently hiding fields it does not know how to show.
// Keep this policy on both schemas regardless of `additionalProperties` in FastMCP's JSON Schema.
// `fromJSONSchema` returns ZodType<unknown>; these assertions join it to types generated from the
// same serialized schemas and are exercised by the renderer tests below the public boundary.
export const zSshExecArguments: z.ZodType<SshExecArguments> =
  generatedArguments.strict() as unknown as z.ZodType<SshExecArguments>;
export const zSshExecResult: z.ZodType<SshExecResult> = generatedResult.strict() as unknown as z.ZodType<SshExecResult>;

/** Settings that change how an SSH command runs; the opened card and the notification share them. */
export function sshExecNotes(args: { timeout_seconds?: number | null | undefined }): string[] {
  return args.timeout_seconds == null ? [] : [`Timeout ${args.timeout_seconds} s`];
}
