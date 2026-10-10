import { z } from "zod";

export const resource: Record<"apiVersion" | "kind" | "name" | "namespace", z.ZodString> = {
  apiVersion: z.string().min(1),
  kind: z.string().min(1),
  name: z.string().min(1),
  namespace: z.string().min(1),
};

export function kubernetesTarget(kind: string, name: string, namespace?: string): string {
  return `${kind} ${namespace ? `${namespace}/` : ""}${name}`;
}
