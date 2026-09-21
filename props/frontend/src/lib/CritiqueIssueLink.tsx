import { resolve } from "./router";

interface Props {
  runId: string;
  issueId: string;
  displayText?: string;
}

export default function CritiqueIssueLink({ runId, issueId, displayText }: Props) {
  const text = displayText ?? issueId;

  return (
    <a
      href={resolve(`/runs/${runId}#critique-${issueId}`)}
      className="font-mono text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
      title={`View critique issue ${issueId} in run ${runId}`}
    >
      {text}
    </a>
  );
}
