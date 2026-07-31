// Argument schemas for the `kubectl-passthrough-mcp` tools the console renders.
//
// kubectl-passthrough-mcp is a remote third-party binary (containers/kubernetes-mcp-server), so
// its tools/list schemas are not available to the build-time in-process catalog; these are
// hand-authored against the live advertised schema. They live in their own React-free module
// because two consumers need them: the widgets in `requests.tsx`, and the notification action
// registry in `../actions.ts`, which the service worker bundles.

import { z } from "zod";

export const zResourcesCreateOrUpdateArgs = z.object({
  resource: z.string(),
});

export const zResourcesDeleteArgs = z.object({
  apiVersion: z.string(),
  kind: z.string(),
  name: z.string(),
  namespace: z.string().optional(),
  gracePeriodSeconds: z.number().optional(),
});

export const zPodsDeleteArgs = z.object({
  name: z.string(),
  namespace: z.string().optional(),
});
export const zPodsLogArgs = z.object({
  name: z.string(),
  namespace: z.string().optional(),
  container: z.string().optional(),
  previous: z.boolean().optional(),
  tail: z.number().int().optional(),
});
