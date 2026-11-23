import { useQuery } from "@tanstack/react-query";

import { Center, Loader, Text } from "@mantine/core";

import DefinitionDetail from "$components/DefinitionDetail";
import { fetchDefinitionDetail } from "$lib/api/client";
import { definitionDetailQueryKey } from "$lib/queryKeys";

interface Props {
  definitionId: string;
}

export default function DefinitionDetailPage({ definitionId }: Props) {
  const query = useQuery({
    queryKey: definitionDetailQueryKey(definitionId),
    queryFn: () => fetchDefinitionDetail(definitionId),
    enabled: Boolean(definitionId),
  });

  if (!definitionId) return null;
  if (query.error) {
    return <Text c="red">{query.error instanceof Error ? query.error.message : "Failed to load definition"}</Text>;
  }
  if (!query.data) {
    return (
      <Center mih={160}>
        <Loader size="sm" />
        <Text c="dimmed">Loading...</Text>
      </Center>
    );
  }
  return <DefinitionDetail data={query.data} />;
}
