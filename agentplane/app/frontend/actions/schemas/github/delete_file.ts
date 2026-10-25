import { z } from "zod";

export type DeleteFileArguments = {
  owner: string;
  repo: string;
  path: string;
  message: string;
  branch: string;
};

// Unknown arguments fail closed: compact-card decisions rely on showing the complete call.
export const zDeleteFileArguments: z.ZodType<DeleteFileArguments> = z.strictObject({
  owner: z.string().min(1),
  repo: z.string().min(1),
  path: z.string().min(1),
  message: z.string().min(1),
  branch: z.string().min(1),
});
