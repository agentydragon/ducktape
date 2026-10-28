import { useEffect, useState } from "react";

import { Button, Card, Center, Group, Loader, SimpleGrid, Stack, Tabs, Text, Title } from "@mantine/core";

import CoverageHeatmap from "../components/stats/CoverageHeatmap";
import DefinitionsTable from "../components/stats/DefinitionsTable";
import DistributionChart from "../components/stats/DistributionChart";
import SummaryCards from "../components/stats/SummaryCards";
import { useNavigate } from "react-router";
import { useRunModal } from "../lib/runModalContext";
import { toast } from "../lib/toast";
import type { RunModalPrefill } from "../lib/types";
import type { CoverageResponse, OverviewResponse } from "../lib/schema";
import { fetchCoverage, fetchOverview } from "../lib/api/client";

interface Props {
  initialData?: OverviewResponse;
}

type CoverageRequest =
  | { split: "valid" | "train"; status: "loading" }
  | { split: "valid" | "train"; status: "loaded"; data: CoverageResponse }
  | { split: "valid" | "train"; status: "error" };

export default function OverviewPage({ initialData }: Props) {
  const navigate = useNavigate();
  const runModal = useRunModal();
  const [overview, setOverview] = useState<OverviewResponse | null>(initialData ?? null);
  const [loading, setLoading] = useState(!initialData);
  const [error, setError] = useState<string | null>(null);
  const [analysisSplit, setAnalysisSplit] = useState<"valid" | "train">("valid");
  const [coverageRequest, setCoverageRequest] = useState<CoverageRequest | null>(null);

  useEffect(() => {
    if (initialData) return;

    let current = true;
    setLoading(true);
    setError(null);
    void fetchOverview()
      .then((data) => {
        if (current) setOverview(data);
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : "Failed to load overview");
      })
      .finally(() => {
        if (current) setLoading(false);
      });

    return () => {
      current = false;
    };
  }, [initialData]);

  useEffect(() => {
    if (!overview) return;
    let current = true;
    setCoverageRequest({ split: analysisSplit, status: "loading" });

    void fetchCoverage(analysisSplit)
      .then((data) => {
        if (current) setCoverageRequest({ split: analysisSplit, status: "loaded", data });
      })
      .catch((reason: unknown) => {
        if (current) {
          setCoverageRequest({ split: analysisSplit, status: "error" });
          toast.error(reason instanceof Error ? reason.message : "Failed to load analysis");
        }
      });

    return () => {
      current = false;
    };
  }, [overview, analysisSplit]);

  function handleNavigateToRuns(filters: RunModalPrefill) {
    const params = new URLSearchParams();
    if (filters.definitionId) params.set("definition", filters.definitionId);
    if (filters.split) params.set("split", filters.split);
    if (filters.kind) params.set("kind", filters.kind);
    const query = params.toString();
    void navigate(query ? `/runs?${query}` : "/runs");
  }

  if (loading) {
    return (
      <Center mih={160}>
        <Loader size="sm" />
        <Text c="dimmed">Loading...</Text>
      </Center>
    );
  }
  if (error) return <Text c="red">{error}</Text>;
  if (!overview) return null;

  const currentCoverageRequest = coverageRequest?.split === analysisSplit ? coverageRequest : null;

  return (
    <Stack gap="md">
      <SummaryCards data={overview} />
      <Group justify="flex-end">
        <Button onClick={() => runModal.open()}>+ New Run</Button>
      </Group>
      <Card shadow="sm" radius="md" p={0}>
        <DefinitionsTable
          definitions={overview.definitions}
          exampleCounts={overview.example_counts}
          onCellClick={handleNavigateToRuns}
        />
      </Card>
      <Card shadow="sm" radius="md" p="md">
        <Group justify="space-between" mb="md">
          <Title order={3}>Stats &amp; Analysis</Title>
          <Tabs value={analysisSplit} onChange={(value) => value && setAnalysisSplit(value as "valid" | "train")}>
            <Tabs.List>
              <Tabs.Tab value="valid">Valid</Tabs.Tab>
              <Tabs.Tab value="train">Train</Tabs.Tab>
            </Tabs.List>
          </Tabs>
        </Group>

        {!currentCoverageRequest || currentCoverageRequest.status === "loading" ? (
          <Center mih={100}>
            <Loader size="sm" />
            <Text c="dimmed">Loading analysis...</Text>
          </Center>
        ) : currentCoverageRequest.status === "loaded" ? (
          <Stack gap="md">
            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
              <DistributionChart
                values={currentCoverageRequest.data.max_recall_values}
                title="Max Recall Distribution"
                numBuckets={10}
                valueFormat={(value) => `${(value * 100).toFixed(0)}%`}
                color="rgb(59, 130, 246)"
              />
              <DistributionChart
                values={currentCoverageRequest.data.tp_count_values}
                title="TP Occurrence Count Distribution"
                numBuckets={8}
                valueFormat={(value) => value.toFixed(0)}
                color="rgb(34, 197, 94)"
              />
            </SimpleGrid>
            {currentCoverageRequest.data.definitions.length > 0 && (
              <CoverageHeatmap
                definitions={currentCoverageRequest.data.definitions}
                examples={currentCoverageRequest.data.examples}
                cells={currentCoverageRequest.data.cells}
              />
            )}
          </Stack>
        ) : null}
      </Card>
    </Stack>
  );
}
