import { z } from "zod";

export type CreateBranchArguments = {
  owner: string;
  repo: string;
  branch: string;
  from_branch?: string | undefined;
};

// Unknown arguments fail closed: card decision buttons rely on the label showing every argument.
export const zCreateBranchArguments: z.ZodType<CreateBranchArguments> = z.strictObject({
  owner: z.string().min(1),
  repo: z.string().min(1),
  branch: z.string().min(1),
  from_branch: z.string().min(1).optional(),
});
