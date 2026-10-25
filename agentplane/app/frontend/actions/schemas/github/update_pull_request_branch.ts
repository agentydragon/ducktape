import { z } from "zod";

export type UpdatePullRequestBranchArguments = {
  owner: string;
  repo: string;
  pullNumber: number;
  expectedHeadSha?: string | undefined;
};

// Unknown arguments fail closed so card decisions only appear when the compact label shows the full call.
export const zUpdatePullRequestBranchArguments: z.ZodType<UpdatePullRequestBranchArguments> = z.strictObject({
  owner: z.string(),
  repo: z.string(),
  pullNumber: z.number(),
  expectedHeadSha: z.string().optional(),
});
