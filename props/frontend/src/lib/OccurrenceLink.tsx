import EntityLink from "./EntityLink";

interface Props {
  snapshotSlug: string;
  issueId: string;
  occurrenceId: string;
  filePath?: string;
}

export default function OccurrenceLink({ snapshotSlug, issueId, occurrenceId, filePath }: Props) {
  let urlPath = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}`;
  if (filePath) {
    urlPath += `?file=${encodeURIComponent(filePath)}`;
  }

  return (
    <EntityLink
      to={urlPath}
      title={`View occurrence ${issueId}/${occurrenceId} in ${snapshotSlug}`}
      variant="monospace"
    >
      {issueId}/{occurrenceId}
    </EntityLink>
  );
}
