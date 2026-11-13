import { Badge, Button, SegmentedControl, Stack, Table, Text, Title, Tooltip } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";
import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useSearchParams } from "react-router";

import { WatchDecisionsResponseSchema, type Decision } from "../../../egress/api/admin/egress_admin_pb";
import { TimeAgo } from "../time_ago";
import { decisionAt, decisionOutcome, decisionReason, type DecisionGroup, groupDecisions } from "./decision_groups";
import { followWatch } from "../watch_stream";
import { type StreamConnection } from "../stream_reader";
import { useStreamStatus } from "../stream_status";
import type { ServiceAccount } from "../../../proto/identity_pb";

/** The groups whose requests are listed, comma-separated; a group key has no comma in it. */
const GROUPS_PARAM = "decision_groups";
/** Present while the log is shown as one row per request. */
const EVERY_REQUEST_PARAM = "every_request";
/** Requests listed under an open group before "Show all". */
const MEMBERS_SHOWN = 50;

function reasonOf(decision: Decision): string {
  return decisionOutcome(decision) === "allow"
    ? [decision.policy, decision.substituted ? "credential substituted" : null].filter((part) => part).join(" · ")
    : (decisionReason(decision) ?? "");
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
          <TimeAgo value={decisionAt(decision)} />
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
        <Badge
          color={
            decisionOutcome(decision) === "allow" ? "green" : decisionOutcome(decision) === "deny" ? "red" : "gray"
          }
        >
          {decisionOutcome(decision)}
        </Badge>
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
          <Badge
            color={
              decisionOutcome(group.members[0]) === "allow"
                ? "green"
                : decisionOutcome(group.members[0]) === "deny"
                  ? "red"
                  : "gray"
            }
          >
            {decisionOutcome(group.members[0])}
          </Badge>
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

/** Complete recent windows for the observed execution account. */
function useEgressDecisions(account: ServiceAccount): { decisions: Decision[] | null; connection: StreamConnection } {
  const { namespace, name } = account;
  const [decisions, setDecisions] = useState<Decision[] | null>(null);
  const [connection, setConnection] = useState<StreamConnection>({ phase: "connecting", since: Date.now() });
  useEffect(() => {
    let active = true;
    let receivedSnapshot = false;
    let current = new Map<string, Decision>();
    const publish = (): void => {
      setDecisions(
        [...current.values()].sort((a, b) => {
          const as = a.at?.seconds ?? 0n;
          const bs = b.at?.seconds ?? 0n;
          if (as !== bs) return as < bs ? -1 : 1;
          const nanos = (a.at?.nanos ?? 0) - (b.at?.nanos ?? 0);
          return nanos || a.eventId.localeCompare(b.eventId);
        })
      );
    };
    const query = new URLSearchParams({ "filter.subject.namespace": namespace, "filter.subject.name": name });
    const stop = followWatch(`/upstream/egress/v1/decisions:watch?${query}`, {
      initialFrameTimeoutMs: 5000,
      idleTimeoutMs: 45_000,
      onOpen: () => {
        receivedSnapshot = false;
        current = new Map();
        setDecisions(null);
      },
      onFrame: (value) => {
        if (!active) return;
        const frame = fromJson(WatchDecisionsResponseSchema, value as JsonValue).frame;
        if (frame.case === "snapshot") {
          if (receivedSnapshot) throw new Error("Egress repeated its initial snapshot");
          for (const decision of frame.value.decisions) {
            if (!decision.eventId || current.has(decision.eventId))
              throw new Error("Egress returned an invalid decision identity");
            current.set(decision.eventId, decision);
          }
          receivedSnapshot = true;
          publish();
        } else if (frame.case === "delta" && receivedSnapshot) {
          for (const id of frame.value.removedEventIds) {
            if (!current.delete(id)) throw new Error("Egress removed an unknown decision");
          }
          for (const decision of frame.value.added) {
            if (!decision.eventId || current.has(decision.eventId))
              throw new Error("Egress returned an invalid decision identity");
            current.set(decision.eventId, decision);
          }
          publish();
        } else if (frame.case !== "heartbeat" || !receivedSnapshot) {
          throw new Error("Egress returned an invalid decision watch frame");
        }
      },
      onConnection: (next) => {
        if (!active) return;
        if (next.phase !== "live") setDecisions(null);
        setConnection(next);
      },
    });
    return () => {
      active = false;
      stop();
    };
  }, [namespace, name]);
  useStreamStatus(`Egress decisions of ${namespace}/${name}`, connection);
  return { decisions, connection };
}

/** The current execution account's recent Egress decisions. */
export function EgressLog({ account, sandbox }: { account: ServiceAccount; sandbox: string }): JSX.Element {
  const { decisions, connection } = useEgressDecisions(account);
  return (
    <Stack gap="xs">
      <Title order={5}>Recent decisions</Title>
      <Text size="xs" c="dimmed">
        ServiceAccount {account.namespace}/{account.name}; may include earlier Sandbox incarnations.
      </Text>
      {connection.phase === "reconnecting" && (
        <Text size="sm" c="dimmed">
          Egress decisions for {sandbox} are unavailable. Reconnecting…
        </Text>
      )}
      {decisions !== null && <DecisionsTable decisions={decisions} />}
    </Stack>
  );
}
