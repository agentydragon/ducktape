import EntityLink from "./EntityLink";
import { formatUuid } from "./formatters";

interface Props {
  id: string;
}

export default function RunIdLink({ id }: Props) {
  return (
    <EntityLink to={`/runs/${id}`} title={id} variant="monospace">
      {formatUuid(id)}
    </EntityLink>
  );
}
