import CopyButton from "./CopyButton";
import ExpandableItem from "./ExpandableItem";
import CollapsibleSection from "./CollapsibleSection";
import { formatJson, tryParseJson, getContentText } from "../lib/llmRequestUtils";

interface Props {
  responseBody: Record<string, unknown>;
}

export default function LLMResponseSection({ responseBody }: Props) {
  const outputItems = Array.isArray(responseBody.output) ? (responseBody.output as Record<string, unknown>[]) : null;
  const usage = responseBody.usage as Record<string, unknown> | null | undefined;
  const detailKeys = Object.keys(responseBody).filter((key) => key !== "output" && key !== "usage");

  const inputDetails = usage?.input_tokens_details as Record<string, unknown> | null | undefined;
  const outputDetails = usage?.output_tokens_details as Record<string, unknown> | null | undefined;
  const cached = typeof inputDetails?.cached_tokens === "number" ? inputDetails.cached_tokens : 0;
  const reasoning = typeof outputDetails?.reasoning_tokens === "number" ? outputDetails.reasoning_tokens : 0;

  return (
    <div className="p-4 space-y-2">
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Response</h4>
        <CopyButton text={formatJson(responseBody)} label="Copy JSON" />
      </div>

      {outputItems?.map((item, index) => {
        const itemType = typeof item.type === "string" ? item.type : null;

        if (itemType === "message") {
          return (
            <ExpandableItem item={item} key={index}>
              <p className="flex-1 text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">
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
      })}

      {usage && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          ↑ {String(usage.input_tokens)} in{cached > 0 ? ` (${cached} cached)` : ""}
          {" · "}↓ {String(usage.output_tokens)} out{reasoning > 0 ? ` (${reasoning} reasoning)` : ""}
        </p>
      )}

      {detailKeys.length > 0 && (
        <CollapsibleSection
          label={`Response details (${detailKeys.join(", ")})`}
          jsonData={Object.fromEntries(detailKeys.map((key) => [key, responseBody[key]]))}
        />
      )}
    </div>
  );
}
