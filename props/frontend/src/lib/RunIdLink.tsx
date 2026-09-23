import { resolve } from "./router";
import { formatUuid } from "./formatters";

interface Props {
  id: string;
}

export default function RunIdLink({ id }: Props) {
  return (
    <a
      href={resolve(`/runs/${id}`)}
      className="font-mono text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
      title={id}
    >
      {formatUuid(id)}
    </a>
  );
}
