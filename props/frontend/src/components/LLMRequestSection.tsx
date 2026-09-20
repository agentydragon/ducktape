import CopyButton from "./CopyButton";
import ExpandableItem from "./ExpandableItem";
import CollapsibleSection from "./CollapsibleSection";
import { formatJson, tryParseJson, getContentText, roleBadgeClass } from "../lib/llmRequestUtils";

interface Props {
  requestBody: Record<string, unknown>;
}

export default function LLMRequestSection({ requestBody }: Props) {
  const inputItems = Array.isArray(requestBody.input) ? (requestBody.input as Record<string, unknown>[]) : null;
  const inputStr = typeof requestBody.input === "string" ? requestBody.input : null;
  const instructions = typeof requestBody.instructions === "string" ? requestBody.instructions : null;
  const paramKeys = Object.keys(requestBody).filter((key) => key !== "input" && key !== "instructions");

  return (
    <div className="p-4 space-y-2">
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Request</h4>
        <CopyButton text={formatJson(requestBody)} label="Copy JSON" />
      </div>

      {instructions && (
        <div className="flex gap-2">
          <span className={`shrink-0 px-2 py-0.5 text-xs font-medium rounded ${roleBadgeClass.system}`}>
            instructions
          </span>
          <p className="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{instructions}</p>
        </div>
      )}

      {inputItems ? (
        inputItems.map((item, index) => {
          const role = typeof item.role === "string" ? item.role : null;
          const itemType = typeof item.type === "string" ? item.type : null;

          if (role) {
            return (
              <ExpandableItem item={item} key={index}>
                <span
                  className={`shrink-0 px-2 py-0.5 text-xs font-medium rounded ${roleBadgeClass[role] ?? roleBadgeClass.system}`}
                >
                  {role}
                </span>
                <p className="flex-1 min-w-0 text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">
                  {getContentText(item.content)}
                </p>
              </ExpandableItem>
            );
          }

          if (itemType === "function_call") {
            return (
              <ExpandableItem item={item} alignItems="items-start" key={index}>
                <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300">
                  ⚙ {String(item.name ?? "")}
                </span>
                <pre className="flex-1 min-w-0 text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap overflow-auto max-h-32">
                  {formatJson(tryParseJson(item.arguments))}
                </pre>
              </ExpandableItem>
            );
          }

          if (itemType === "function_call_output") {
            return (
              <ExpandableItem item={item} alignItems="items-start" key={index}>
                <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300">
                  ↩ result
                </span>
                <pre className="flex-1 min-w-0 text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap overflow-auto max-h-32">
                  {formatJson(tryParseJson(item.output))}
                </pre>
              </ExpandableItem>
            );
          }

          if (itemType === "reasoning") {
            return (
              <ExpandableItem item={item} key={index}>
                <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300">
                  💭 reasoning
                </span>
                <p className="flex-1 text-sm text-gray-600 dark:text-gray-400 italic">{getContentText(item.summary)}</p>
              </ExpandableItem>
            );
          }

          return null;
        })
      ) : inputStr ? (
        <p className="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{inputStr}</p>
      ) : null}

      {paramKeys.length > 0 && (
        <CollapsibleSection
          label={`Request params (${paramKeys.join(", ")})`}
          jsonData={Object.fromEntries(paramKeys.map((key) => [key, requestBody[key]]))}
        />
      )}
    </div>
  );
}
