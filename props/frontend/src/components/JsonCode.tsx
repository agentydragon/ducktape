import { CodeBlock } from "../../../../util/frontend/code_block";
import { formatJson } from "../lib/llmRequestUtils";

import "./json_code.css";

interface Props {
  value: unknown;
  className: string;
}

export default function JsonCode({ value, className }: Props) {
  return (
    <div className={`props-json-code ${className}`}>
      <CodeBlock text={formatJson(value)} language="json" surface="plain" />
    </div>
  );
}
