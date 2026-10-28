import type { JSX } from "react";
import {
  Badge,
  Button,
  Center,
  Group,
  Loader,
  NavLink,
  Paper,
  ScrollArea,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import type { SessionSummary } from "./schema";
import { isRoutineSession, sessionRoutineId, sessionSubtitle, statusColor, type StatusFilter } from "./session-info";

function SessionRow({
  session,
  selected,
  onSelect,
}: {
  session: SessionSummary;
  selected: boolean;
  onSelect: () => void;
}): JSX.Element {
  const routineId = sessionRoutineId(session);
  const routineSession = routineId !== null || isRoutineSession(session);
  return (
    <NavLink
      component="button"
      type="button"
      active={selected}
      aria-pressed={selected}
      onClick={onSelect}
      label={
        <Text size="sm" fw={600} truncate>
          {session.title || "Untitled session"}
        </Text>
      }
      description={
        <Stack gap={4} mt={6}>
          <Group gap="xs">
            <Badge size="xs" variant="light" color={statusColor(session.status)}>
              {session.status}
            </Badge>
            {routineSession && (
              <Badge size="xs" variant="light" color="violet">
                Routine run
              </Badge>
            )}
            <Text component="time" size="xs" c="dimmed" dateTime={session.updated_at}>
              {new Date(session.updated_at).toLocaleDateString()}
            </Text>
          </Group>
          <Text size="xs" c="dimmed" ff="monospace" truncate>
            {sessionSubtitle(session)}
          </Text>
          {routineId !== null && (
            <Text size="xs" c="dimmed" ff="monospace" truncate>
              Routine {routineId}
            </Text>
          )}
        </Stack>
      }
    />
  );
}

export function SessionList({
  id,
  sessions,
  visibleSessions,
  selectedId,
  loadingSessions,
  nextSessionCursor,
  loadingMoreSessions,
  search,
  filter,
  onSearchChange,
  onFilterChange,
  onSelect,
  onLoadMore,
}: {
  id: string;
  sessions: SessionSummary[];
  visibleSessions: SessionSummary[];
  selectedId: string | null;
  loadingSessions: boolean;
  nextSessionCursor: string | null;
  loadingMoreSessions: boolean;
  search: string;
  filter: StatusFilter;
  onSearchChange: (search: string) => void;
  onFilterChange: (filter: StatusFilter) => void;
  onSelect: (sessionId: string) => void;
  onLoadMore: () => void;
}): JSX.Element {
  return (
    <Paper
      id={id}
      component="aside"
      aria-label="Session list"
      withBorder
      radius="sm"
      p="xs"
      style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "hidden" }}
    >
      <Stack gap="xs" style={{ flex: 1, minHeight: 0 }}>
        <TextInput
          label="Search loaded sessions"
          type="search"
          value={search}
          onChange={(event) => onSearchChange(event.currentTarget.value)}
        />
        <Select
          label="Status"
          data={[
            { value: "all", label: "All sessions" },
            { value: "active", label: "Active" },
            { value: "paused", label: "Paused" },
            { value: "archived", label: "Archived" },
          ]}
          value={filter}
          onChange={(value) => {
            if (value !== null) onFilterChange(value as StatusFilter);
          }}
        />
        <ScrollArea style={{ flex: 1, minHeight: 0 }} type="auto">
          {loadingSessions ? (
            <Center h={240}>
              <Loader size="sm" aria-label="Loading sessions" />
            </Center>
          ) : visibleSessions.length === 0 ? (
            <Center h={180} px="md">
              <Text c="dimmed" ta="center">
                {sessions.length === 0 ? "No sessions in the sync yet." : "No matching sessions."}
              </Text>
            </Center>
          ) : (
            <Stack gap={4}>
              {visibleSessions.map((session) => (
                <SessionRow
                  key={session.id}
                  session={session}
                  selected={session.id === selectedId}
                  onSelect={() => onSelect(session.id)}
                />
              ))}
            </Stack>
          )}
          {nextSessionCursor !== null && (
            <Button fullWidth variant="default" mt="xs" loading={loadingMoreSessions} onClick={onLoadMore}>
              Load more sessions
            </Button>
          )}
        </ScrollArea>
      </Stack>
    </Paper>
  );
}
