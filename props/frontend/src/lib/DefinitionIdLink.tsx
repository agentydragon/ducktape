import EntityLink from "./EntityLink";
import { formatDigest } from "./formatters";

interface Props {
  id: string;
  display_name?: string | null;
}

export default function DefinitionIdLink({ id, display_name }: Props) {
  return (
    <EntityLink to={`/definitions/${id}`} title={id}>
      {display_name ?? formatDigest(id)}
    </EntityLink>
  );
}
