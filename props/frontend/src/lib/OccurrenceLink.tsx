import { useHref } from "react-router";
import CopyButton from "../components/CopyButton";
import EntityLink from "./EntityLink";

interface Props {
  snapshotSlug: string;
  issueId: string;
  occurrenceId: string;
  filePath?: string;
}

function occurrencePath({ snapshotSlug, issueId, occurrenceId, filePath }: Props): string {
  const path = `/snapshots/${snapshotSlug}/${issueId}/${occurrenceId}`;
  return filePath ? `${path}?file=${encodeURIComponent(filePath)}` : path;
}

export function CopyOccurrenceLink(props: Props & { label: string }) {
  const href = useHref(occurrencePath(props));
  return <CopyButton text={new URL(href, window.location.href).href} label={props.label} />;
}

export default function OccurrenceLink(props: Props) {
  const { snapshotSlug, issueId, occurrenceId } = props;
  return (
    <EntityLink
      to={occurrencePath(props)}
      title={`View occurrence ${issueId}/${occurrenceId} in ${snapshotSlug}`}
      variant="monospace"
    >
      {issueId}/{occurrenceId}
    </EntityLink>
  );
}
