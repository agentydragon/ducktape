import { Link } from "react-router";
import { formatSnapshotSlug } from "./formatters";

interface Props {
  slug: string;
  showFull?: boolean;
}

export default function SnapshotLink({ slug, showFull = false }: Props) {
  const displayText = showFull ? slug : formatSnapshotSlug(slug);

  return (
    <Link
      to={`/snapshots/${slug}`}
      className="text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
      title={slug}
    >
      {displayText}
    </Link>
  );
}
