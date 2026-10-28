// Link component for examples.
import { Link } from "react-router";
import type { SingleFileSetExampleOutput, WholeSnapshotExampleOutput } from "./api/schema";

type Example = WholeSnapshotExampleOutput | SingleFileSetExampleOutput;

interface Props {
  example: Example;
}

export default function ExampleLink({ example }: Props) {
  const displayText =
    example.kind === "whole_snapshot"
      ? `whole@${example.snapshot_slug}`
      : `files@${example.snapshot_slug}/${example.files_hash.slice(0, 6)}`;

  const params = new URLSearchParams({
    snapshot_slug: example.snapshot_slug,
    example_kind: example.kind,
  });
  if (example.kind === "file_set") {
    params.set("files_hash", example.files_hash);
  }
  const queryString = params.toString();

  return (
    <Link
      to={`/examples?${queryString}`}
      className="font-mono text-xs text-blue-600 dark:text-blue-400 underline hover:text-blue-800 dark:hover:text-blue-300"
      title={`${example.snapshot_slug} (${example.kind})`}
    >
      {displayText}
    </Link>
  );
}
