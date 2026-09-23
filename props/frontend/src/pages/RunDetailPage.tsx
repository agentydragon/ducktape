import { Text } from "@mantine/core";

import RunDetail from "$components/RunDetail";

interface Props {
  runId: string;
}

export default function RunDetailPage({ runId }: Props) {
  if (!runId) return <Text c="dimmed">Invalid run ID</Text>;
  return <RunDetail runId={runId} />;
}
