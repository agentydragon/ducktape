export function kubernetesTarget(kind: string, name: string, namespace?: string): string {
  return `${kind} ${namespace ? `${namespace}/` : ""}${name}`;
}
