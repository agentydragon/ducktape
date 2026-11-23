import { useQuery } from "@tanstack/react-query";

import { Center, Loader, Text } from "@mantine/core";

import ExampleDetail from "$components/ExampleDetail";
import { useSearchParams } from "react-router";
import type { ExampleDetailResponse, ExampleKind } from "$lib/schema";
import { fetchExampleDetail } from "$lib/api/client";
import { exampleDetailQueryKey } from "$lib/queryKeys";

interface Props {
  initialData?: ExampleDetailResponse;
}

export default function ExamplesPage({ initialData }: Props) {
  const [searchParams] = useSearchParams();
  const snapshotSlug = searchParams.get("snapshot_slug") ?? "";
  const exampleKind = (searchParams.get("example_kind") ?? "whole_snapshot") as ExampleKind;
  const filesHash = searchParams.get("files_hash");

  const query = useQuery({
    queryKey: exampleDetailQueryKey(snapshotSlug, exampleKind, filesHash),
    queryFn: () => fetchExampleDetail(snapshotSlug, exampleKind, filesHash),
    enabled: !initialData && Boolean(snapshotSlug),
  });
  const example = initialData ?? query.data;

  if (!snapshotSlug) return <Text c="dimmed">Missing snapshot_slug parameter</Text>;
  if (query.error) {
    return <Text c="dimmed">{query.error instanceof Error ? query.error.message : "Failed to load example"}</Text>;
  }
  if (!example) {
    return (
      <Center mih={160}>
        <Loader size="sm" />
        <Text c="dimmed">Loading...</Text>
      </Center>
    );
  }
  return <ExampleDetail data={example} />;
}
