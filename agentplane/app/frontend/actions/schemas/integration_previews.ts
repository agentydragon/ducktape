import { z } from "zod";

export const modifyThreadLabelsArguments: z.ZodType<{
  thread_ids: string[];
  add?: string[] | null;
  remove?: string[] | null;
}> = z.strictObject({
  thread_ids: z.array(z.string().min(1)).min(1),
  add: z.array(z.string()).nullish(),
  remove: z.array(z.string()).nullish(),
});

export const moveNodeArguments: z.ZodType<{
  nodeId: string;
  targetNodeId: string;
  sourceParentId?: string;
  referenceNodeId?: string;
  position: "start" | "end" | "before" | "after";
  keepSourceReference: boolean;
}> = z.strictObject({
  nodeId: z.string().min(1),
  targetNodeId: z.string().min(1),
  sourceParentId: z.string().optional(),
  referenceNodeId: z.string().optional(),
  position: z.enum(["start", "end", "before", "after"]).default("end"),
  keepSourceReference: z.boolean().default(false),
});
