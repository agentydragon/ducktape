import { z } from "zod";

// Unknown arguments fail closed rather than making an unshown parameter actionable.
export const zPodsInNamespaceArguments = z.strictObject({
  namespace: z.string().min(1),
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
});
