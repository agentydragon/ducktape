import { z } from "zod";

// Unknown arguments fail closed rather than making an unshown parameter actionable.
const podsInNamespaceArguments = z.strictObject({
  namespace: z.string().min(1),
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
});

export const zPodsInNamespaceArguments: typeof podsInNamespaceArguments = podsInNamespaceArguments;
