import CopyButton from "./CopyButton";
import { formatJson } from "../lib/llmRequestUtils";

interface Props {
  title: string;
  value: unknown;
}

export default function RawJsonSection({ title, value }: Props) {
  const json = formatJson(value);

  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">{title}</h4>
        <CopyButton text={json} label="Copy JSON" />
      </div>
      <pre className="bg-gray-50 dark:bg-gray-900 p-3 rounded text-xs overflow-auto">{json}</pre>
    </div>
  );
}
