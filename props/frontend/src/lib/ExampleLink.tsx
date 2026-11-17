// Link component for examples.
import EntityLink from "./EntityLink";
import type { SingleFileSetExampleOutput, WholeSnapshotExampleOutput } from "./schema";

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
    <EntityLink
      to={`/examples?${queryString}`}
      title={`${example.snapshot_slug} (${example.kind})`}
      variant="smallMonospace"
    >
      {displayText}
    </EntityLink>
  );
}
