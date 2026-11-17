import EntityLink from "./EntityLink";

interface Props {
  runId: string;
  issueId: string;
  displayText?: string;
}

export default function CritiqueIssueLink({ runId, issueId, displayText }: Props) {
  const text = displayText ?? issueId;

  return (
    <EntityLink
      to={`/runs/${runId}#critique-${issueId}`}
      title={`View critique issue ${issueId} in run ${runId}`}
      variant="monospace"
    >
      {text}
    </EntityLink>
  );
}
