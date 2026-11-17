import ExpandableItem from "./ExpandableItem";
import JsonCode from "./JsonCode";
import { getContentText, tryParseJson } from "../lib/llmRequestUtils";

interface Props {
  item: Record<string, unknown>;
}

export function FunctionCallItem({ item }: Props) {
  return (
    <ExpandableItem item={item} alignItems="items-start">
      <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300">
        ⚙ {String(item.name ?? "")}
      </span>
      <JsonCode
        value={tryParseJson(item.arguments)}
        className="flex-1 min-w-0 text-xs text-gray-700 dark:text-gray-300 overflow-auto max-h-32"
      />
    </ExpandableItem>
  );
}

export function ReasoningItem({ item }: Props) {
  return (
    <ExpandableItem item={item}>
      <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300">
        💭 reasoning
      </span>
      <p className="flex-1 text-sm text-gray-600 dark:text-gray-400 italic">{getContentText(item.summary)}</p>
    </ExpandableItem>
  );
}
