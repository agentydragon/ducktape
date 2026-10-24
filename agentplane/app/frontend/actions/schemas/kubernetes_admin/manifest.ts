// React-free, so the service worker's notification formatters share the app's reading of a manifest.
import { parseAllDocuments } from "yaml";

import { kubernetesTarget } from "./target";

export type KubernetesResource = Record<string, unknown>;

/** A value is a Kubernetes resource only when its identity fields have the standard API shape. */
export function isKubernetesResource(value: unknown): value is KubernetesResource {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const resource = value as Record<string, unknown>;
  if (typeof resource.apiVersion !== "string" || typeof resource.kind !== "string") return false;
  const metadata = resource.metadata;
  return metadata === undefined || (metadata !== null && typeof metadata === "object" && !Array.isArray(metadata));
}

/** Known Kubernetes YAML may be a document stream or a YAML sequence of resources. */
export function parseKubernetesYaml(text: string): KubernetesResource[] | undefined {
  try {
    const documents = parseAllDocuments(text, { prettyErrors: false, uniqueKeys: true });
    if (documents.some((document) => document.errors.length > 0)) return undefined;
    const values = documents.flatMap((document) => (document.contents === null ? [] : [document.toJS()]));
    const resources = values.flatMap((value) => (Array.isArray(value) ? value : [value]));
    return resources.length > 0 && resources.every(isKubernetesResource) ? resources : undefined;
  } catch {
    return undefined;
  }
}

/** `Kind namespace/name`, or the kind alone when the manifest names no object. */
export function kubernetesResourceTarget(resource: KubernetesResource): string {
  const metadata = resource.metadata;
  if (metadata === null || typeof metadata !== "object" || Array.isArray(metadata)) return String(resource.kind);
  const fields = metadata as Record<string, unknown>;
  const name = typeof fields.name === "string" && fields.name.length > 0 ? fields.name : undefined;
  const namespace = typeof fields.namespace === "string" && fields.namespace.length > 0 ? fields.namespace : undefined;
  return name === undefined ? String(resource.kind) : kubernetesTarget(String(resource.kind), name, namespace);
}

/** A compact operation summary for a manifest; malformed or non-resource YAML has no inferred identity. */
export function kubernetesResourceSummary(text: string): string | undefined {
  const resources = parseKubernetesYaml(text);
  if (resources === undefined) return undefined;
  if (resources.length === 1) return `Apply ${kubernetesResourceTarget(resources[0])}`;
  const listed = resources.slice(0, 2).map(kubernetesResourceTarget).join(", ");
  const remaining = resources.length - 2;
  return `Apply ${resources.length} Kubernetes resources: ${listed}${remaining > 0 ? `, and ${remaining} more` : ""}`;
}
