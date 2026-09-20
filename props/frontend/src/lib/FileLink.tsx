import { resolve } from "./router";

interface Props {
  snapshotSlug: string;
  filePath: string;
  displayText?: string;
}

export default function FileLink({ snapshotSlug, filePath, displayText }: Props) {
  const text = displayText ?? filePath;

  return (
    <a
      href={resolve(`/snapshots/${snapshotSlug}?file=${encodeURIComponent(filePath)}`)}
      className="font-mono text-xs text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
      title={`View ${filePath} in ${snapshotSlug}`}
    >
      {text}
    </a>
  );
}
