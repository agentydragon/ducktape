import { Badge, Button, SegmentedControl, Stack, Table, Text, Title, Tooltip } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import { TimeAgo } from "../time_ago";
import type { Decision } from "./client";
import { type DecisionGroup, groupDecisions } from "./decision_groups";
import { type DecisionsSnapshot, liveEgressDecisionsUrl } from "../live";
import { followStream, type StreamConnection } from "../live_stream";
import { useStreamStatus } from "../stream_status";

/** The groups whose requests are listed, comma-separated; a group key has no comma in it. */
const GROUPS_PARAM = "decision_groups";
/** Present while the log is shown as one row per request. */
const EVERY_REQUEST_PARAM = "every_request";
/** Requests listed under an open group before "Show all". */
const MEMBERS_SHOWN = 50;

function reasonOf(decision: Decision): string {
  return decision.outcome === "allow"
    ? [decision.policy, decision.substituted ? "credential substituted" : null].filter((part) => part).join(" · ")
    : (decision.reason ?? "");
}

function destination(decision: { host: string; port: number }): string {
  return `${decision.host}${decision.port !== 443 ? `:${decision.port}` : ""}`;
}

function DecisionRow({ decision, nested }: { decision: Decision; nested: boolean }): JSX.Element {
  const reason = reasonOf(decision);
  return (
    <Table.Tr>
      <Table.Td pl={nested ? "xl" : undefined}>
        <Text size="sm" style={{ whiteSpace: "nowrap" }}>
          <TimeAgo value={decision.at} />
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm">{decision.method}</Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {destination(decision)}
        </Text>
        {decision.address && (
          <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
            {decision.address}
          </Text>
        )}
        <Text size="xs" c="dimmed" hiddenFrom="sm" style={{ overflowWrap: "anywhere" }}>
          {reason}
        </Text>
      </Table.Td>
      <Table.Td>
        <Badge color={decision.outcome === "allow" ? "green" : "red"}>{decision.outcome}</Badge>
      </Table.Td>
      <Table.Td visibleFrom="sm">
        <Text size="sm">{reason || "—"}</Text>
      </Table.Td>
    </Table.Tr>
  );
}

function DecisionGroupRows({
  group,
  open,
  onToggle,
}: {
  group: DecisionGroup;
  open: boolean;
  onToggle: () => void;
}): JSX.Element {
  const [showAll, setShowAll] = useState(false);
  if (group.members.length === 1) return <DecisionRow decision={group.members[0]} nested={false} />;
  const reason = reasonOf({ ...group.members[0], substituted: group.substituted });
  const listed = showAll ? group.members : group.members.slice(0, MEMBERS_SHOWN);
  return (
    <>
      <Table.Tr>
        <Table.Td>
          <Tooltip
            label={
              <>
                First <TimeAgo value={group.firstAt} />
              </>
            }
            withArrow
          >
            <Text size="sm" style={{ whiteSpace: "nowrap" }}>
              <TimeAgo value={group.lastAt} />
            </Text>
          </Tooltip>
        </Table.Td>
        <Table.Td>
          <Text size="sm">{group.method}</Text>
        </Table.Td>
        <Table.Td>
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            {destination(group)}
          </Text>
          <Button size="compact-xs" variant="subtle" onClick={onToggle} aria-expanded={open}>
            {open ? "Hide" : "Show"} {group.members.length} requests
          </Button>
          <Text size="xs" c="dimmed" hiddenFrom="sm" style={{ overflowWrap: "anywhere" }}>
            {reason}
          </Text>
        </Table.Td>
        <Table.Td>
          <Badge color={group.outcome === "allow" ? "green" : "red"}>{group.outcome}</Badge>
        </Table.Td>
        <Table.Td visibleFrom="sm">
          <Text size="sm">{reason || "—"}</Text>
        </Table.Td>
      </Table.Tr>
      {open && listed.map((member, index) => <DecisionRow key={index} decision={member} nested />)}
      {open && listed.length < group.members.length && (
        <Table.Tr>
          <Table.Td colSpan={5} pl="xl">
            <Button size="compact-xs" variant="subtle" onClick={() => setShowAll(true)}>
              Show all {group.members.length}
            </Button>
          </Table.Td>
        </Table.Tr>
      )}
    </>
  );
}

function DecisionsTable({ decisions }: { decisions: Decision[] }): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const everyRequest = searchParams.has(EVERY_REQUEST_PARAM);
  const open = new Set((searchParams.get(GROUPS_PARAM) ?? "").split(",").filter((key) => key));
  function update(change: (params: URLSearchParams) => void): void {
    const params = new URLSearchParams(searchParams);
    change(params);
    setSearchParams(params, { replace: true });
  }
  function toggle(key: string): void {
    const next = new Set(open);
    if (!next.delete(key)) next.add(key);
    update((params) => (next.size === 0 ? params.delete(GROUPS_PARAM) : params.set(GROUPS_PARAM, [...next].join(","))));
  }
  const groups = groupDecisions(decisions);
  return (
    <Stack gap="xs">
      <SegmentedControl
        size="xs"
        value={everyRequest ? "every" : "grouped"}
        onChange={(value) =>
          update((params) =>
            value === "every" ? params.set(EVERY_REQUEST_PARAM, "") : params.delete(EVERY_REQUEST_PARAM)
          )
        }
        data={[
          { value: "grouped", label: "Grouped" },
          { value: "every", label: "Every request" },
        ]}
      />
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{everyRequest ? "Time" : "Last"}</Table.Th>
            <Table.Th>Method</Table.Th>
            <Table.Th>Host</Table.Th>
            <Table.Th>Decision</Table.Th>
            <Table.Th visibleFrom="sm">Reason</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {decisions.length === 0 && (
            <Table.Tr>
              <Table.Td colSpan={5}>
                <Text size="sm" c="dimmed">
                  No decisions yet.
                </Text>
              </Table.Td>
            </Table.Tr>
          )}
          {everyRequest
            ? // Newest first: the log is served oldest first.
              [...decisions]
                .reverse()
                .map((decision, index) => <DecisionRow key={index} decision={decision} nested={false} />)
            : groups.map((group) => (
                <DecisionGroupRows
                  key={group.key}
                  group={group}
                  open={open.has(group.key)}
                  onToggle={() => toggle(group.key)}
                />
              ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

/** The sandbox's decisions as the proxy reports them: a snapshot per change, none while it is quiet. */
function useEgressDecisions(name: string): { snapshot: DecisionsSnapshot | null } {
  const [snapshot, setSnapshot] = useState<DecisionsSnapshot | null>(null);
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  useEffect(() => {
    setSnapshot(null);
    return followStream(liveEgressDecisionsUrl(name), {
      events: {
        snapshot: (message) => setSnapshot(JSON.parse(message.data) as DecisionsSnapshot),
        health: (message) =>
          setSnapshot((current) => current && { ...current, ...(JSON.parse(message.data) as { watching: boolean }) }),
      },
      onConnection: setConnection,
    });
  }, [name]);
  useStreamStatus(`Egress decisions of ${name}`, connection);
  return { snapshot };
}

/** What recently left the sandbox, or was refused, as the egress proxy reports it. */
export function EgressLog({ name }: { name: string }): JSX.Element {
  const decisions = useEgressDecisions(name);
  return (
    <Stack gap="xs">
      <Title order={5}>Recent decisions</Title>
      {decisions.snapshot !== null && !decisions.snapshot.watching && (
        <Text size="sm" c="dimmed">
          Not hearing from the egress proxy about new decisions; this log may be behind.
        </Text>
      )}
      {decisions.snapshot !== null &&
        (Array.isArray(decisions.snapshot.decisions) ? (
          <DecisionsTable decisions={decisions.snapshot.decisions} />
        ) : (
          <Text size="sm" c="dimmed">
            The egress proxy could not be asked; the bindings still apply. ({decisions.snapshot.decisions.detail})
          </Text>
        ))}
    </Stack>
  );
}
