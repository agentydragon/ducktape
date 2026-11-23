export type SnapshotDetailQueryKey = readonly ["props", "snapshot-detail", string, string | null, string | null];

export function snapshotDetailQueryKey(
  snapshotSlug: string,
  exampleKind: string | null,
  filesHash: string | null
): SnapshotDetailQueryKey {
  return ["props", "snapshot-detail", snapshotSlug, exampleKind, filesHash];
}

export function overviewQueryKey() {
  return ["props", "overview"] as const;
}

export function snapshotListQueryKey() {
  return ["props", "snapshot-list"] as const;
}

export function exampleDetailQueryKey(snapshotSlug: string, exampleKind: string, filesHash: string | null) {
  return ["props", "example-detail", snapshotSlug, exampleKind, filesHash] as const;
}

export function definitionDetailQueryKey(definitionId: string) {
  return ["props", "definition-detail", definitionId] as const;
}
