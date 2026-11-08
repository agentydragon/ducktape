export type SnapshotDetailQueryKey = readonly ["props", "snapshot-detail", string, string | null, string | null];

export function snapshotDetailQueryKey(
  snapshotSlug: string,
  exampleKind: string | null,
  filesHash: string | null
): SnapshotDetailQueryKey {
  return ["props", "snapshot-detail", snapshotSlug, exampleKind, filesHash];
}
