import EntityLink from "./EntityLink";

interface Props {
  snapshotSlug: string;
  issueId: string;
  occurrenceId: string;
  filePath?: string;
  displayText?: string;
}

export default function OccurrenceLink({ snapshotSlug, issueId, occurrenceId, filePath, displayText }: Props) {
  let urlPath = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}`;
  if (filePath) {
    urlPath += `?file=${encodeURIComponent(filePath)}`;
  }
  const text = displayText ?? `${issueId}/${occurrenceId}`;

  return (
    <EntityLink
      to={urlPath}
      title={`View occurrence ${issueId}/${occurrenceId} in ${snapshotSlug}`}
      variant="monospace"
    >
      {text}
    </EntityLink>
  );
}
