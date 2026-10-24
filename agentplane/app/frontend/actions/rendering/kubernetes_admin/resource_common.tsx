import { z } from "zod";

// Cluster-scoped kinds (Namespace, PersistentVolume, Node) have no namespace.
export const resource: {
  apiVersion: z.ZodString;
  kind: z.ZodString;
  name: z.ZodString;
  namespace: z.ZodOptional<z.ZodString>;
} = {
  apiVersion: z.string().min(1),
  kind: z.string().min(1),
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
};
