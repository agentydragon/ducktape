import EntityLink from "./EntityLink";

interface Props {
  snapshotSlug: string;
  filePath: string;
  displayText?: string;
}

export default function FileLink({ snapshotSlug, filePath, displayText }: Props) {
  const text = displayText ?? filePath;

  return (
    <EntityLink
      to={`/snapshots/${snapshotSlug}?file=${encodeURIComponent(filePath)}`}
      title={`View ${filePath} in ${snapshotSlug}`}
      variant="smallMonospace"
    >
      {text}
    </EntityLink>
  );
}
