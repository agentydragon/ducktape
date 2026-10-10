import { z } from "zod";

export type PodsInNamespaceArguments = {
  namespace: string;
  fieldSelector?: string | undefined;
  labelSelector?: string | undefined;
};

// Unknown arguments fail closed rather than making an unshown parameter actionable.
export const zPodsInNamespaceArguments: z.ZodType<PodsInNamespaceArguments> = z.strictObject({
  namespace: z.string().min(1),
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
});
