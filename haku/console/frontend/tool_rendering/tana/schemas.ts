// Argument schemas for the `tana` tools the console renders.
//
// tana is a remote server, so its schemas are hand-authored rather than generated. They live
// in their own React-free module because both the widgets in `requests.tsx` and the action
// registry in `../actions.ts` need them.

import { z } from "zod";

export const zEditOperation: z.ZodType<{ old_string: string; new_string: string; replace_all?: boolean }> = z.object({
  old_string: z.string(),
  new_string: z.string(),
  replace_all: z.boolean().optional(),
});

export const zImportTanaPasteArgs: z.ZodType<{ parentNodeId: string; content: string }> = z.object({
  parentNodeId: z.string(),
  content: z.string(),
});
export const zTrashNodeArgs: z.ZodType<{ nodeId: string }> = z.object({ nodeId: z.string() });
export const zEditNodeArgs: z.ZodType<{
  nodeId: string;
  name?: { old_string: string; new_string: string; replace_all?: boolean };
  description?: { old_string: string; new_string: string; replace_all?: boolean };
}> = z
  .object({ nodeId: z.string(), name: zEditOperation.optional(), description: zEditOperation.optional() })
  .refine((args) => args.name !== undefined || args.description !== undefined);
export const zSetFieldOptionArgs: z.ZodType<{
  nodeId: string;
  attributeId: string;
  optionId: string;
  mode: "replace" | "append";
}> = z.object({
  nodeId: z.string(),
  attributeId: z.string(),
  optionId: z.string(),
  mode: z.enum(["replace", "append"]).default("replace"),
});
