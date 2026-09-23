import { useEffect, useState } from "react";

import { Badge, Card, Center, Loader, Table, Text, Title } from "@mantine/core";

import { goto } from "$lib/router";
import { fetchSnapshots, type SnapshotsResponse } from "$lib/api/client";

interface Props {
  initialData?: SnapshotsResponse["snapshots"];
}

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

export default function SnapshotsPage({ initialData }: Props) {
  const [snapshots, setSnapshots] = useState<SnapshotsResponse["snapshots"]>(initialData ?? []);
  const [loading, setLoading] = useState(!initialData);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initialData) return;

    let current = true;
    setLoading(true);
    setError(null);
    void fetchSnapshots()
      .then((data) => {
        if (current) setSnapshots(data.snapshots);
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : "Failed to load snapshots");
      })
      .finally(() => {
        if (current) setLoading(false);
      });

    return () => {
      current = false;
    };
  }, [initialData]);

  if (loading) {
    return (
      <Center mih={160}>
        <Loader size="sm" />
        <Text c="dimmed">Loading...</Text>
      </Center>
    );
  }
  if (error) return <Text c="red">{error}</Text>;

  return (
    <Card shadow="sm" radius="md" p="md">
      <Title order={2} mb="md">
        Snapshots
      </Title>
      {snapshots.length === 0 ? (
        <Text c="dimmed">No snapshots found</Text>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <Table striped highlightOnHover verticalSpacing="sm" miw={640}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Slug</Table.Th>
                <Table.Th>Split</Table.Th>
                <Table.Th ta="right">TPs</Table.Th>
                <Table.Th ta="right">FPs</Table.Th>
                <Table.Th ta="right">Created</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {snapshots.map((snapshot) => (
                <Table.Tr
                  key={snapshot.slug}
                  onClick={() => goto(`/snapshots/${snapshot.slug}`)}
                  style={{ cursor: "pointer" }}
                >
                  <Table.Td>
                    <Text ff="monospace" size="sm">
                      {snapshot.slug}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge color={splitColor(snapshot.split)} variant="light">
                      {snapshot.split}
                    </Badge>
                  </Table.Td>
                  <Table.Td ta="right">{snapshot.tp_count}</Table.Td>
                  <Table.Td ta="right">{snapshot.fp_count}</Table.Td>
                  <Table.Td ta="right">
                    <Text c="dimmed" size="sm">
                      {new Date(snapshot.created_at).toLocaleDateString()}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}
