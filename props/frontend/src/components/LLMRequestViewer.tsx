import { ChevronDown, ChevronRight } from "../lib/icons";
import type { LLMRequestInfo } from "../lib/api/client";
import LLMRequestSection from "./LLMRequestSection";
import LLMResponseSection from "./LLMResponseSection";
import RawJsonSection from "./RawJsonSection";

interface Props {
  requests: LLMRequestInfo[];
  initialExpanded?: number[];
}

export default function LLMRequestViewer({ requests, initialExpanded = [] }: Props) {
  if (requests.length === 0) {
    return <p className="text-gray-500 dark:text-gray-400 italic">No LLM requests recorded</p>;
  }

  return (
    <div className="space-y-2">
      {requests.map((req) => (
        <details
          key={req.id}
          open={initialExpanded.includes(req.id)}
          className="border dark:border-gray-700 rounded group"
        >
          <summary className="px-4 py-2 flex items-center justify-between cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 list-none">
            <div className="flex items-center gap-4 text-sm">
              <span className="font-mono text-gray-500 dark:text-gray-400">#{req.id}</span>
              <span className="font-medium">{req.model}</span>
              {req.latency_ms ? <span className="text-gray-500 dark:text-gray-400">{req.latency_ms}ms</span> : null}
              {req.error ? <span className="text-red-600 dark:text-red-400">Error</span> : null}
            </div>
            <span className="text-gray-400 dark:text-gray-500">
              <ChevronDown size={16} className="hidden group-open:block" />
              <ChevronRight size={16} className="block group-open:hidden" />
            </span>
          </summary>

          <div className="border-t dark:border-gray-700 divide-y dark:divide-gray-700">
            {req.api_shape === "chat_completions" ? (
              <RawJsonSection title="Chat Request JSON" value={req.request_body} />
            ) : (
              <LLMRequestSection requestBody={req.request_body as Record<string, unknown>} />
            )}
            {req.response_body && req.api_shape === "chat_completions" ? (
              <RawJsonSection title="Chat Response JSON" value={req.response_body} />
            ) : req.response_body ? (
              <LLMResponseSection responseBody={req.response_body as Record<string, unknown>} />
            ) : null}
            {req.response_error_body ? (
              <div className="p-4">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-red-600 dark:text-red-400 mb-2">
                  Error Response
                </h4>
                <pre className="bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300 p-3 rounded text-xs overflow-auto">
                  {JSON.stringify(req.response_error_body, null, 2)}
                </pre>
              </div>
            ) : null}
            {req.error ? (
              <div className="p-4">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-red-600 dark:text-red-400 mb-2">
                  Error
                </h4>
                <pre className="bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300 p-3 rounded text-xs">
                  {req.error}
                </pre>
              </div>
            ) : null}
          </div>
        </details>
      ))}
    </div>
  );
}
