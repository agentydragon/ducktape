import { z } from "zod";

const name = z.string().min(1);
const namespace = name.optional();
const fieldSelector = name.optional();
const labelSelector = name.optional();
const pod = { name, namespace };
const container = name.optional();
const resourceKind = { apiVersion: name, kind: name };

// Keep unknown arguments out of both the structured review and the formatted notification.
export const eventsListArguments = z.strictObject({ namespace, fieldSelector });
export const podsGetArguments = z.strictObject(pod);
export const podsDeleteArguments = z.strictObject(pod);
export const podsExecArguments = z.strictObject({ ...pod, container, command: z.array(z.string()) });
export const podsLogArguments = z.strictObject({
  ...pod,
  container,
  previous: z.boolean().optional(),
  tail: z.int().optional(),
});
export const podsInNamespaceArguments = z.strictObject({ namespace: name, fieldSelector, labelSelector });

export const resourceApplyArguments = z.strictObject({ resource: name });
export const resourcesGetArguments = z.strictObject({ ...resourceKind, ...pod });
export const resourcesDeleteArguments = resourcesGetArguments.extend({
  gracePeriodSeconds: z.int().nonnegative().optional(),
});
export const resourcesListArguments = z.strictObject({ ...resourceKind, namespace, fieldSelector, labelSelector });
