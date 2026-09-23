import { useEffect, useState } from "react";

import { Center, Loader, Text } from "@mantine/core";

import DefinitionDetail from "$components/DefinitionDetail";
import { fetchDefinitionDetail, type DefinitionDetailResponse } from "$lib/api/client";

interface Props {
  definitionId: string;
}

export default function DefinitionDetailPage({ definitionId }: Props) {
  const [definition, setDefinition] = useState<DefinitionDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!definitionId) {
      setLoading(false);
      return;
    }

    let current = true;
    setLoading(true);
    setError(null);

    void fetchDefinitionDetail(definitionId)
      .then((data) => {
        if (current) setDefinition(data);
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : "Failed to load definition");
      })
      .finally(() => {
        if (current) setLoading(false);
      });

    return () => {
      current = false;
    };
  }, [definitionId]);

  if (loading) {
    return (
      <Center mih={160}>
        <Loader size="sm" />
        <Text c="dimmed">Loading...</Text>
      </Center>
    );
  }
  if (error) return <Text c="red">{error}</Text>;
  if (definition) return <DefinitionDetail data={definition} />;
  return null;
}
