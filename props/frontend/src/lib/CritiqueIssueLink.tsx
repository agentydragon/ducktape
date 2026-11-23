import EntityLink from "./EntityLink";

interface Props {
  runId: string;
  issueId: string;
}

export default function CritiqueIssueLink({ runId, issueId }: Props) {
  return (
    <EntityLink
      to={`/runs/${runId}#critique-${issueId}`}
      title={`View critique issue ${issueId} in run ${runId}`}
      variant="monospace"
    >
      {issueId}
    </EntityLink>
  );
}
