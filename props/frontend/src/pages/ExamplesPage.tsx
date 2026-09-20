import { useEffect, useState } from "react";

import { Center, Loader, Text } from "@mantine/core";

import ExampleDetail from "$components/ExampleDetail";
import { useSearchParams } from "$lib/router";
import { fetchExampleDetail, type ExampleDetailResponse, type ExampleKind } from "$lib/api/client";

interface Props {
  initialData?: ExampleDetailResponse;
}

export default function ExamplesPage({ initialData }: Props) {
  const searchParams = useSearchParams();
  const snapshotSlug = searchParams.get("snapshot_slug") ?? "";
  const exampleKind = (searchParams.get("example_kind") ?? "whole_snapshot") as ExampleKind;
  const filesHash = searchParams.get("files_hash");

  const [example, setExample] = useState<ExampleDetailResponse | null>(initialData ?? null);
  const [loading, setLoading] = useState(!initialData);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!snapshotSlug) {
      setError("Missing snapshot_slug parameter");
      setLoading(false);
      return;
    }
    if (initialData) return;

    let current = true;
    setLoading(true);
    setError(null);
    void fetchExampleDetail(snapshotSlug, exampleKind, filesHash)
      .then((data) => {
        if (current) setExample(data);
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : "Failed to load example");
      })
      .finally(() => {
        if (current) setLoading(false);
      });

    return () => {
      current = false;
    };
  }, [snapshotSlug, exampleKind, filesHash, initialData]);

  if (loading) {
    return (
      <Center mih={160}>
        <Loader size="sm" />
        <Text c="dimmed">Loading...</Text>
      </Center>
    );
  }
  if (error) return <Text c="dimmed">{error}</Text>;
  if (example) return <ExampleDetail data={example} />;
  return null;
}
