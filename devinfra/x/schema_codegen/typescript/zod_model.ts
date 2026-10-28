import { z } from "zod";

export const taskSchema = z
  .object({
    requestId: z.string().min(1),
    submittedAt: z.iso.datetime({ offset: true }),
    tags: z.record(z.string(), z.string()),
    outcome: z.discriminatedUnion("kind", [
      z.object({ kind: z.literal("accepted"), sequence: z.number().int().nonnegative() }).strict(),
      z.object({ kind: z.literal("rejected"), reason: z.string().min(1) }).strict(),
    ]),
    optionalComment: z.string().optional(),
    nullableComment: z.string().nullable(),
  })
  .strict();
