import { z } from "zod";

export type CreateOrUpdateFileArguments = {
  owner: string;
  repo: string;
  path: string;
  content: string;
  message: string;
  branch: string;
  sha?: string | undefined;
  allow_symlink_write?: boolean | undefined;
};

// Unknown arguments fail closed so card decisions only appear when the compact view shows the full call.
export const zCreateOrUpdateFileArguments: z.ZodType<CreateOrUpdateFileArguments> = z.strictObject({
  owner: z.string(),
  repo: z.string(),
  path: z.string(),
  content: z.string(),
  message: z.string(),
  branch: z.string(),
  sha: z.string().optional(),
  allow_symlink_write: z.boolean().optional(),
});
