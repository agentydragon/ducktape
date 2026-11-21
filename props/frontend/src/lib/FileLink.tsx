import EntityLink from "./EntityLink";

interface Props {
  snapshotSlug: string;
  filePath: string;
}

export default function FileLink({ snapshotSlug, filePath }: Props) {
  return (
    <EntityLink
      to={`/snapshots/${snapshotSlug}?file=${encodeURIComponent(filePath)}`}
      title={`View ${filePath} in ${snapshotSlug}`}
      variant="smallMonospace"
    >
      {filePath}
    </EntityLink>
  );
}
