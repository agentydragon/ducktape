import type { ReactNode } from "react";
import { formatJson } from "../lib/llmRequestUtils";

interface Props {
  item: Record<string, unknown>;
  /** Extra flex alignment class for the content row, e.g. "items-start" for <pre> content. */
  alignItems?: string;
  children: ReactNode;
}

export default function ExpandableItem({ item, alignItems, children }: Props) {
  return (
    <div className="space-y-1">
      <div className={`flex gap-2 ${alignItems ?? ""}`}>{children}</div>
      <details>
        <summary className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 cursor-pointer list-none">
          Raw
        </summary>
        <pre className="mt-1 bg-gray-900 text-gray-100 p-2 rounded text-xs overflow-auto max-h-40">
          {formatJson(item)}
        </pre>
      </details>
    </div>
  );
}
