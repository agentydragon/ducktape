import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  Badge,
  Box,
  Button,
  Card,
  Center,
  Code,
  Group,
  Loader,
  Paper,
  SimpleGrid,
  Stack,
  Tabs,
  Text,
  Title,
} from "@mantine/core";

import BackButton from "$components/BackButton";
import Breadcrumb from "$components/Breadcrumb";
import CopyButton from "$components/CopyButton";
import FileTree from "$components/FileTree";
import FileViewer from "$components/FileViewer";
import OccurrenceStats from "$components/stats/OccurrenceStats";
import CreditBadge from "$components/stats/CreditBadge";
import {
  fetchOccurrenceStats,
  fetchSnapshotClusters,
  fetchSnapshotDetail,
  fetchSnapshotFile,
  fetchSnapshotTree,
  type ClusterResponse,
  type FileContentResponse,
  type FileTreeResponse,
  type OccurrenceStatsRow,
  type SnapshotDetailResponse,
} from "$lib/api/client";
import { formatLocationAnchor } from "$lib/formatters";
import { useSearchParams, resolve } from "$lib/router";
import { toast } from "$lib/toast";
import OccurrenceLink from "$lib/OccurrenceLink";

interface Props {
  slug: string;
  initialSnapshot?: SnapshotDetailResponse;
  initialTree?: FileTreeResponse;
}

type ActiveTab = "files" | "tps" | "fps" | "clusters" | "stats";
type SnapshotIssue =
  SnapshotDetailResponse["true_positives"][number] | SnapshotDetailResponse["false_positives"][number];

function splitColor(split: string): string {
  switch (split) {
    case "train":
      return "blue";
    case "valid":
      return "green";
    case "test":
      return "violet";
    default:
      return "gray";
  }
}

export default function SnapshotDetailPage({ slug, initialSnapshot, initialTree }: Props) {
  const searchParams = useSearchParams();
  const parts = slug.split("/");
  const snapshotSlug = parts.slice(0, 2).join("/");
  const issueId = parts.length >= 4 ? parts[2] : undefined;
  const routeOccurrenceId = parts.length >= 4 ? parts[3] : undefined;
  const targetFile = searchParams.get("file") || undefined;

  const [snapshot, setSnapshot] = useState<SnapshotDetailResponse | null>(initialSnapshot ?? null);
  const [tree, setTree] = useState<FileTreeResponse | null>(initialTree ?? null);
  const [loading, setLoading] = useState(!initialSnapshot);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<ActiveTab>("files");
  const [selectedFile, setSelectedFile] = useState<FileContentResponse | null>(null);
  const [loadingFile, setLoadingFile] = useState(false);
  const [occurrenceStats, setOccurrenceStats] = useState<OccurrenceStatsRow[]>([]);
  const [clusters, setClusters] = useState<ClusterResponse[]>([]);
  const [expandedIssues, setExpandedIssues] = useState<Set<string>>(() => new Set());
  const [pendingScrollTarget, setPendingScrollTarget] = useState<string | null>(null);
  const fileRequestId = useRef(0);

  const occurrenceStatsMap = useMemo(
    () => new Map(occurrenceStats.map((occurrence) => [`${occurrence.tp_id}:${occurrence.occurrence_id}`, occurrence])),
    [occurrenceStats]
  );

  useEffect(() => {
    if (!snapshotSlug || initialSnapshot) return;

    let current = true;
    setLoading(true);
    setError(null);
    setOccurrenceStats([]);
    setClusters([]);
    setSelectedFile(null);
    setPendingScrollTarget(null);
    setExpandedIssues(new Set());
    fileRequestId.current += 1;
    setLoadingFile(false);

    void Promise.all([fetchSnapshotDetail(snapshotSlug), fetchSnapshotTree(snapshotSlug)])
      .then(([snapshotData, treeData]) => {
        if (!current) return;
        setSnapshot(snapshotData);
        setTree(treeData);

        void fetchOccurrenceStats(snapshotSlug).then(
          (data) => {
            if (current) setOccurrenceStats(data.occurrences);
          },
          (reason: unknown) => {
            if (current) toast.error(reason instanceof Error ? reason.message : "Failed to load occurrence stats");
          }
        );
        void fetchSnapshotClusters(snapshotSlug).then(
          (data) => {
            if (current) setClusters(data.clusters);
          },
          (reason: unknown) => {
            if (current) toast.error(reason instanceof Error ? reason.message : "Failed to load clusters");
          }
        );
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : "Failed to load snapshot");
      })
      .finally(() => {
        if (current) setLoading(false);
      });

    return () => {
      current = false;
    };
  }, [snapshotSlug, initialSnapshot]);

  const handleFileClick = useCallback(
    async (path: string) => {
      const requestId = ++fileRequestId.current;
      const requestedSlug = snapshotSlug;
      setLoadingFile(true);
      try {
        const file = await fetchSnapshotFile(requestedSlug, path);
        if (requestId === fileRequestId.current) setSelectedFile(file);
      } catch (reason) {
        if (requestId === fileRequestId.current) toast.error(`Failed to load file: ${reason}`);
      } finally {
        if (requestId === fileRequestId.current) setLoadingFile(false);
      }
    },
    [snapshotSlug]
  );

  const toggleExpanded = useCallback((id: string) => {
    setExpandedIssues((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const expandIssue = useCallback((id: string) => {
    setExpandedIssues((current) => (current.has(id) ? current : new Set(current).add(id)));
  }, []);

  const findAndNavigateToOccurrence = useCallback(
    (targetIssueId: string, occurrenceId: string, filePath?: string) => {
      if (!snapshot) return;
      const issues: SnapshotIssue[] = [...snapshot.true_positives, ...snapshot.false_positives];
      for (const issue of issues) {
        const currentIssueId = "tp_id" in issue ? issue.tp_id : issue.fp_id;
        if (currentIssueId !== targetIssueId) continue;
        const occurrence = issue.occurrences.find((item) => item.occurrence_id === occurrenceId);
        if (!occurrence?.locations.length) return;

        void handleFileClick(filePath || occurrence.locations[0].file);
        expandIssue(targetIssueId);
        setActiveTab("files");
        setPendingScrollTarget(`${targetIssueId}-${occurrenceId}`);
        return;
      }
    },
    [snapshot, handleFileClick, expandIssue]
  );

  useEffect(() => {
    if (issueId && routeOccurrenceId && snapshot) {
      findAndNavigateToOccurrence(issueId, routeOccurrenceId, targetFile);
    }
  }, [issueId, routeOccurrenceId, snapshot, targetFile, findAndNavigateToOccurrence]);

  useEffect(() => {
    if (!pendingScrollTarget || !selectedFile || loadingFile) return;
    const target = pendingScrollTarget;
    setPendingScrollTarget(null);
    const frame = requestAnimationFrame(() => {
      document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    return () => cancelAnimationFrame(frame);
  }, [pendingScrollTarget, selectedFile, loadingFile]);

  const breadcrumbs = useMemo(() => {
    if (!selectedFile || !snapshot) return [{ label: snapshotSlug }];
    return [
      { label: snapshot.slug, href: `/snapshots/${snapshotSlug}` },
      ...selectedFile.path.split("/").map((part) => ({ label: part })),
    ];
  }, [selectedFile, snapshot, snapshotSlug]);

  function getOccurrenceUrl(targetIssueId: string, occurrenceId: string, filePath?: string): string {
    const routePath = `/snapshots/${snapshotSlug}/${targetIssueId}/${occurrenceId}`;
    const hashPath = resolve(routePath);
    if (filePath) return `${window.location.origin}${hashPath}?file=${encodeURIComponent(filePath)}`;
    return `${window.location.origin}${hashPath}`;
  }

  function renderIssueList(issues: SnapshotIssue[], isTruePositive: boolean) {
    if (issues.length === 0) {
      return <Text c="dimmed">No {isTruePositive ? "true" : "false"} positives</Text>;
    }

    return (
      <Stack gap="xs">
        {issues.map((issue) => {
          const currentIssueId = "tp_id" in issue ? issue.tp_id : issue.fp_id;
          const expanded = expandedIssues.has(currentIssueId);
          return (
            <Card key={currentIssueId} withBorder p={0}>
              <Button
                variant="subtle"
                color="gray"
                fullWidth
                onClick={() => toggleExpanded(currentIssueId)}
                aria-expanded={expanded}
              >
                <Group justify="space-between" w="100%">
                  <Group gap="xs">
                    <Text c="dimmed">{expanded ? "▼" : "▶"}</Text>
                    <Code>{currentIssueId}</Code>
                    <Text size="xs" c="dimmed">
                      ({issue.occurrences.length} occ)
                    </Text>
                  </Group>
                </Group>
              </Button>
              {expanded && (
                <Box px="md" pb="md">
                  <Box mt="xs">
                    <Text size="xs" fw={600} c="dimmed" tt="uppercase" mb={4}>
                      Rationale
                    </Text>
                    <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
                      {issue.rationale}
                    </Text>
                  </Box>
                  <Box mt="md">
                    <Text size="xs" fw={600} c="dimmed" tt="uppercase" mb={4}>
                      Occurrences
                    </Text>
                    <Stack gap="xs">
                      {issue.occurrences.map((occurrence) => {
                        const stats = isTruePositive
                          ? occurrenceStatsMap.get(`${currentIssueId}:${occurrence.occurrence_id}`)
                          : undefined;
                        const highlighted =
                          issueId === currentIssueId && routeOccurrenceId === occurrence.occurrence_id;
                        return (
                          <Paper
                            key={occurrence.occurrence_id}
                            id={`${currentIssueId}-${occurrence.occurrence_id}`}
                            withBorder
                            p="sm"
                            style={highlighted ? { outline: "2px solid var(--mantine-color-blue-5)" } : undefined}
                          >
                            <Group justify="space-between" align="flex-start">
                              <Group gap="xs">
                                <OccurrenceLink
                                  snapshotSlug={snapshot?.slug ?? snapshotSlug}
                                  issueId={currentIssueId}
                                  occurrenceId={occurrence.occurrence_id}
                                  filePath={occurrence.locations[0]?.file}
                                />
                                {stats && <CreditBadge meanCredit={stats.mean_credit} nRuns={stats.n_runs} />}
                              </Group>
                              <CopyButton
                                text={getOccurrenceUrl(
                                  currentIssueId,
                                  occurrence.occurrence_id,
                                  occurrence.locations[0]?.file
                                )}
                                label="Copy URL"
                              />
                            </Group>
                            <Stack gap={2} mt="xs">
                              {occurrence.locations.map((location, index) => (
                                <Text
                                  key={`${occurrence.occurrence_id}-${location.file}-${index}`}
                                  size="sm"
                                  ff="monospace"
                                >
                                  {formatLocationAnchor(location)}
                                  {location.note && (
                                    <Text span c="dimmed" fs="italic" ml="xs">
                                      ({location.note})
                                    </Text>
                                  )}
                                </Text>
                              ))}
                            </Stack>
                            {occurrence.note && (
                              <Text mt="xs" size="sm" c="dimmed" fs="italic">
                                {occurrence.note}
                              </Text>
                            )}
                            {isTruePositive &&
                              "critic_scopes_expected_to_recall" in occurrence &&
                              occurrence.critic_scopes_expected_to_recall &&
                              occurrence.critic_scopes_expected_to_recall.length > 0 && (
                                <Text mt="xs" size="xs" c="dimmed">
                                  Expected recall scopes:{" "}
                                  {occurrence.critic_scopes_expected_to_recall
                                    .map((scope: string[]) => scope.join(", "))
                                    .join(" | ")}
                                </Text>
                              )}
                            {!isTruePositive &&
                              "relevant_files" in occurrence &&
                              occurrence.relevant_files &&
                              occurrence.relevant_files.length > 0 && (
                                <Text mt="xs" size="xs" c="dimmed">
                                  Relevant: {occurrence.relevant_files.join(", ")}
                                </Text>
                              )}
                          </Paper>
                        );
                      })}
                    </Stack>
                  </Box>
                </Box>
              )}
            </Card>
          );
        })}
      </Stack>
    );
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
  if (!snapshot || !tree) return null;

  return (
    <Card shadow="sm" radius="md" p={0}>
      <Stack gap={0}>
        <Box p="md" style={{ borderBottom: "1px solid var(--mantine-color-default-border)" }}>
          <Group mb="xs">
            <BackButton href="/snapshots" />
            <Title order={2} ff="monospace">
              {snapshot.slug}
            </Title>
            <Badge color={splitColor(snapshot.split)} variant="light">
              {snapshot.split}
            </Badge>
          </Group>
          <Breadcrumb
            items={[{ label: "Home", href: "/" }, { label: "Snapshots", href: "/snapshots" }, { label: snapshot.slug }]}
          />
        </Box>

        <Tabs value={activeTab} onChange={(value) => value && setActiveTab(value as ActiveTab)} keepMounted={false}>
          <Tabs.List>
            <Tabs.Tab value="files">Files</Tabs.Tab>
            <Tabs.Tab value="tps">True Positives ({snapshot.true_positives.length})</Tabs.Tab>
            <Tabs.Tab value="fps">False Positives ({snapshot.false_positives.length})</Tabs.Tab>
            <Tabs.Tab value="clusters">Clusters ({clusters.length})</Tabs.Tab>
            <Tabs.Tab value="stats">Detection Stats</Tabs.Tab>
          </Tabs.List>

          <Box p="md">
            <Tabs.Panel value="files">
              <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
                <Box style={{ overflowY: "auto", maxHeight: "70vh" }}>
                  <Text size="sm" fw={600} mb="xs">
                    File Browser
                  </Text>
                  <FileTree nodes={tree.tree} onFileClick={handleFileClick} selectedPath={selectedFile?.path} />
                </Box>
                <Box style={{ overflowY: "auto", maxHeight: "70vh" }}>
                  {loadingFile ? (
                    <Center mih={160}>
                      <Loader size="sm" />
                      <Text c="dimmed">Loading...</Text>
                    </Center>
                  ) : selectedFile ? (
                    <>
                      <Box mb="sm">
                        <Breadcrumb items={breadcrumbs} />
                      </Box>
                      <FileViewer
                        file={selectedFile}
                        tps={snapshot.true_positives}
                        fps={snapshot.false_positives}
                        snapshotSlug={snapshot.slug}
                        targetOccurrenceId={routeOccurrenceId}
                      />
                    </>
                  ) : (
                    <Center mih={160}>
                      <Text c="dimmed">Select a file to view</Text>
                    </Center>
                  )}
                </Box>
              </SimpleGrid>
            </Tabs.Panel>

            <Tabs.Panel value="tps">
              <Box style={{ maxHeight: "70vh", overflowY: "auto" }}>
                {renderIssueList(snapshot.true_positives, true)}
              </Box>
            </Tabs.Panel>

            <Tabs.Panel value="fps">
              <Box style={{ maxHeight: "70vh", overflowY: "auto" }}>
                {renderIssueList(snapshot.false_positives, false)}
              </Box>
            </Tabs.Panel>

            <Tabs.Panel value="clusters">
              <Box style={{ maxHeight: "70vh", overflowY: "auto" }}>
                {clusters.length === 0 ? (
                  <Text c="dimmed">No clusters</Text>
                ) : (
                  <Stack gap="xs">
                    {clusters.map((cluster) => {
                      const expansionId = `cluster-${cluster.cluster_id}`;
                      const expanded = expandedIssues.has(expansionId);
                      return (
                        <Card key={cluster.cluster_id} withBorder p={0}>
                          <Button
                            variant="subtle"
                            color="gray"
                            fullWidth
                            onClick={() => toggleExpanded(expansionId)}
                            aria-expanded={expanded}
                          >
                            <Group justify="space-between" w="100%">
                              <Group gap="xs">
                                <Text c="dimmed">{expanded ? "▼" : "▶"}</Text>
                                <Code>{cluster.cluster_id}</Code>
                                <Text size="xs" c="dimmed">
                                  ({cluster.members.length} issues)
                                </Text>
                              </Group>
                            </Group>
                          </Button>
                          {expanded && (
                            <Box px="md" pb="md">
                              <Box mt="xs">
                                <Text size="xs" fw={600} c="dimmed" tt="uppercase" mb={4}>
                                  Description
                                </Text>
                                <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
                                  {cluster.rationale}
                                </Text>
                              </Box>
                              <Box mt="md">
                                <Text size="xs" fw={600} c="dimmed" tt="uppercase" mb={4}>
                                  Member Issues
                                </Text>
                                <Stack gap="xs">
                                  {cluster.members.map((member) => (
                                    <Paper
                                      key={`${member.critique_run_id}-${member.critique_issue_id}`}
                                      withBorder
                                      p="sm"
                                    >
                                      <Group gap="xs">
                                        <a
                                          href={resolve(`/runs/${member.critique_run_id}`)}
                                          style={{ fontFamily: "monospace" }}
                                        >
                                          {member.critique_run_id.slice(0, 8)}
                                        </a>
                                        <Text c="dimmed">/</Text>
                                        <Code>{member.critique_issue_id}</Code>
                                      </Group>
                                      {member.issue_rationale && (
                                        <Text mt="xs" size="sm">
                                          {member.issue_rationale}
                                        </Text>
                                      )}
                                      {member.rationale && (
                                        <Text mt="xs" size="xs" c="dimmed" fs="italic">
                                          {member.rationale}
                                        </Text>
                                      )}
                                    </Paper>
                                  ))}
                                </Stack>
                              </Box>
                            </Box>
                          )}
                        </Card>
                      );
                    })}
                  </Stack>
                )}
              </Box>
            </Tabs.Panel>

            <Tabs.Panel value="stats">
              <Box style={{ maxHeight: "70vh", overflowY: "auto" }}>
                <OccurrenceStats occurrences={occurrenceStats} />
              </Box>
            </Tabs.Panel>
          </Box>
        </Tabs>
      </Stack>
    </Card>
  );
}
