// Argument schemas for the `kubectl-passthrough-mcp` tools the console renders.
//
// kubectl-passthrough-mcp is a remote third-party binary (containers/kubernetes-mcp-server), so
// its tools/list schemas are not available to the build-time in-process catalog; these are
// hand-authored against the live advertised schema. They live in their own React-free module
// because two consumers need them: the widgets in `requests.tsx`, and the notification action
// registry in `../actions.ts`, which the service worker bundles.

import { z } from "zod";

export const zResourcesCreateOrUpdateArgs: ReturnType<typeof z.object<{ resource: z.ZodString }>> = z.object({
  resource: z.string(),
});

export const zResourcesGetArgs: ReturnType<
  typeof z.object<{
    apiVersion: z.ZodString;
    kind: z.ZodString;
    name: z.ZodString;
    namespace: z.ZodOptional<z.ZodString>;
  }>
> = z.object({
  apiVersion: z.string(),
  kind: z.string(),
  name: z.string(),
  namespace: z.string().optional(),
});

export const zResourcesDeleteArgs: ReturnType<
  typeof z.object<{
    apiVersion: z.ZodString;
    kind: z.ZodString;
    name: z.ZodString;
    namespace: z.ZodOptional<z.ZodString>;
    gracePeriodSeconds: z.ZodOptional<z.ZodNumber>;
  }>
> = z.object({
  apiVersion: z.string(),
  kind: z.string(),
  name: z.string(),
  namespace: z.string().optional(),
  gracePeriodSeconds: z.number().optional(),
});

export const zPodsDeleteArgs: ReturnType<
  typeof z.object<{ name: z.ZodString; namespace: z.ZodOptional<z.ZodString> }>
> = z.object({
  name: z.string(),
  namespace: z.string().optional(),
});
export const zPodsListInNamespaceArgs: ReturnType<
  typeof z.object<{
    fieldSelector: z.ZodOptional<z.ZodString>;
    labelSelector: z.ZodOptional<z.ZodString>;
    namespace: z.ZodString;
  }>
> = z.object({
  fieldSelector: z.string().optional(),
  labelSelector: z.string().optional(),
  namespace: z.string(),
});
export const zPodsExecArgs: ReturnType<
  typeof z.object<{
    command: z.ZodArray<z.ZodString>;
    container: z.ZodOptional<z.ZodString>;
    name: z.ZodString;
    namespace: z.ZodOptional<z.ZodString>;
  }>
> = z.object({
  command: z.array(z.string()),
  container: z.string().optional(),
  name: z.string(),
  namespace: z.string().optional(),
});
export const zPodsLogArgs: ReturnType<
  typeof z.object<{
    name: z.ZodString;
    namespace: z.ZodOptional<z.ZodString>;
    container: z.ZodOptional<z.ZodString>;
    previous: z.ZodOptional<z.ZodBoolean>;
    tail: z.ZodOptional<z.ZodNumber>;
  }>
> = z.object({
  name: z.string(),
  namespace: z.string().optional(),
  container: z.string().optional(),
  previous: z.boolean().optional(),
  tail: z.number().int().optional(),
});
