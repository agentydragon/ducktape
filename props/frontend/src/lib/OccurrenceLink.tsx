import { resolve } from "./router";

interface Props {
  snapshotSlug: string;
  issueId: string;
  occurrenceId: string;
  filePath?: string;
  displayText?: string;
}

export default function OccurrenceLink({ snapshotSlug, issueId, occurrenceId, filePath, displayText }: Props) {
  let urlPath = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}`;
  if (filePath) {
    urlPath += `?file=${encodeURIComponent(filePath)}`;
  }
  const text = displayText ?? `${issueId}/${occurrenceId}`;

  return (
    <a
      href={resolve(urlPath)}
      className="font-mono text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
      title={`View occurrence ${issueId}/${occurrenceId} in ${snapshotSlug}`}
    >
      {text}
    </a>
  );
}
