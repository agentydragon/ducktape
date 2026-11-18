import EntityLink from "./EntityLink";
import { formatSnapshotSlug } from "./formatters";

interface Props {
  slug: string;
}

export default function SnapshotLink({ slug }: Props) {
  return (
    <EntityLink to={`/snapshots/${slug}`} title={slug}>
      {formatSnapshotSlug(slug)}
    </EntityLink>
  );
}
