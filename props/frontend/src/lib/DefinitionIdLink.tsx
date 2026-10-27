import { Link } from "react-router";
import { formatDigest } from "./formatters";

interface Props {
  id: string;
  display_name?: string | null;
}

export default function DefinitionIdLink({ id, display_name }: Props) {
  return (
    <Link
      to={`/definitions/${id}`}
      className="text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
      title={id}
    >
      {display_name ?? formatDigest(id)}
    </Link>
  );
}
