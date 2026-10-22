import { ChevronDown, ChevronRight } from "../lib/icons";
import JsonCode from "./JsonCode";

interface Props {
  label: string;
  jsonData: Record<string, unknown>;
}

export default function CollapsibleSection({ label, jsonData }: Props) {
  return (
    <details className="group">
      <summary className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 cursor-pointer list-none">
        <ChevronDown size={12} className="hidden group-open:block" />
        <ChevronRight size={12} className="block group-open:hidden" />
        {label}
      </summary>
      <JsonCode
        value={jsonData}
        className="mt-1 bg-gray-50 text-gray-900 p-2 rounded text-xs overflow-auto max-h-48 dark:bg-gray-900 dark:text-gray-100"
      />
    </details>
  );
}
