import { z } from "zod";

type OptionalText = z.ZodOptional<z.ZodString>;
type PodTarget = { name: z.ZodString; namespace: OptionalText };
type ResourceKind = { apiVersion: z.ZodString; kind: z.ZodString };
type Selectors = { fieldSelector: OptionalText; labelSelector: OptionalText };

const name = z.string().min(1);
const namespace = name.optional();
const fieldSelector = name.optional();
const labelSelector = name.optional();
const pod = { name, namespace };
const container = name.optional();
const resourceKind = { apiVersion: name, kind: name };

// Keep unknown arguments out of both the structured review and the formatted notification.
export const eventsListArguments: z.ZodObject<{ namespace: OptionalText; fieldSelector: OptionalText }> =
  z.strictObject({ namespace, fieldSelector });
export const podsGetArguments: z.ZodObject<PodTarget> = z.strictObject(pod);
export const podsDeleteArguments: z.ZodObject<PodTarget> = z.strictObject(pod);
export const podsExecArguments: z.ZodObject<PodTarget & { container: OptionalText; command: z.ZodArray<z.ZodString> }> =
  z.strictObject({ ...pod, container, command: z.array(z.string()) });
export const podsLogArguments: z.ZodObject<
  PodTarget & { container: OptionalText; previous: z.ZodOptional<z.ZodBoolean>; tail: z.ZodOptional<z.ZodNumber> }
> = z.strictObject({
  ...pod,
  container,
  previous: z.boolean().optional(),
  tail: z.int().optional(),
});
export const podsInNamespaceArguments: z.ZodObject<Selectors & { namespace: z.ZodString }> = z.strictObject({
  namespace: name,
  fieldSelector,
  labelSelector,
});

export const resourceApplyArguments: z.ZodObject<{ resource: z.ZodString }> = z.strictObject({ resource: name });
export const resourcesGetArguments: z.ZodObject<ResourceKind & PodTarget> = z.strictObject({ ...resourceKind, ...pod });
export const resourcesDeleteArguments: z.ZodObject<
  ResourceKind & PodTarget & { gracePeriodSeconds: z.ZodOptional<z.ZodNumber> }
> = resourcesGetArguments.extend({ gracePeriodSeconds: z.int().nonnegative().optional() });
export const resourcesListArguments: z.ZodObject<ResourceKind & Selectors & { namespace: OptionalText }> =
  z.strictObject({ ...resourceKind, namespace, fieldSelector, labelSelector });
