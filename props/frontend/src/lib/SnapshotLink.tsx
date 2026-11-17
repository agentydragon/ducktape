import EntityLink from "./EntityLink";
import { formatSnapshotSlug } from "./formatters";

interface Props {
  slug: string;
  showFull?: boolean;
}

export default function SnapshotLink({ slug, showFull = false }: Props) {
  const displayText = showFull ? slug : formatSnapshotSlug(slug);

  return (
    <EntityLink to={`/snapshots/${slug}`} title={slug}>
      {displayText}
    </EntityLink>
  );
}
