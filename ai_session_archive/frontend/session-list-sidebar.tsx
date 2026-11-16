import { useEffect, useRef, type JSX } from "react";
import {
  Badge,
  Box,
  Button,
  Center,
  Checkbox,
  Group,
  Loader,
  NativeSelect,
  Paper,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Text,
} from "@mantine/core";
import { Link } from "react-router";

import { harnessLabel } from "./api";
import type { Machine, SessionKind, SessionSummary } from "./schema";
import { SESSION_KINDS } from "./session-viewer-url";
import { formatDateTime } from "./session-viewer-formatting";
import { ResizeHandle, type PaneSize } from "../../util/frontend/resize_handle";

export type SessionListSidebarModel = {
  filters: {
    machines: Machine[];
    machineId: string;
    harness: string;
    kinds: SessionKind[];
    loadingMachines: boolean;
    onChange: (name: "machine" | "harness", value: string) => void;
    onKindsChange: (values: string[]) => void;
  };
  sessions: {
    items: SessionSummary[];
    machineNames: ReadonlyMap<string, string>;
    selectedId: string | undefined;
    pathFor: (targetId: string) => string;
    nextCursor: string | null;
    loading: boolean;
    loadingNext: boolean;
    onLoadMore: () => void;
    onRefresh: () => void;
  };
  layout: {
    mobileOpen: boolean;
    setMobileOpen: (open: boolean) => void;
    sidebarWidth: number;
    sidebarPane: PaneSize;
  };
};

export function SessionListSidebar({ model }: { model: SessionListSidebarModel }): JSX.Element {
  const sidebarRef = useRef<HTMLElement>(null);
  const { filters, sessions, layout } = model;

  useEffect(() => {
    if (!layout.mobileOpen) return;
    const previousFocus = document.activeElement;
    const sidebar = sidebarRef.current;
    const focusableElements = (): HTMLElement[] => [
      ...(sidebar?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      ) ?? []),
    ];
    focusableElements()[0]?.focus();
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        layout.setMobileOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableElements();
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, [layout.mobileOpen, layout.setMobileOpen]);

  return (
    <>
      {layout.mobileOpen && (
        <button
          type="button"
          className="session-browser-mobile-backdrop"
          aria-label="Close session list"
          tabIndex={-1}
          onClick={() => layout.setMobileOpen(false)}
        />
      )}
      <Paper
        component="aside"
        ref={sidebarRef}
        aria-label="Session list"
        aria-modal={layout.mobileOpen || undefined}
        role={layout.mobileOpen ? "dialog" : "complementary"}
        id="session-browser-sidebar"
        withBorder
        radius="sm"
        p="xs"
        className={`session-browser-sidebar${layout.mobileOpen ? " session-browser-sidebar-open" : ""}`}
      >
        <Stack gap="xs" style={{ height: "100%", minHeight: 0 }}>
          <Group className="session-browser-mobile-sidebar-heading" justify="space-between" align="center">
            <Text size="sm" fw={600}>
              Session list
            </Text>
            <Button size="xs" variant="default" onClick={() => layout.setMobileOpen(false)}>
              Close
            </Button>
          </Group>
          <Select
            label="Machine"
            aria-label="Filter by machine"
            data={[
              { value: "", label: "All machines" },
              ...filters.machines.map((machine) => ({ value: machine.id, label: machine.display_name })),
            ]}
            value={filters.machineId}
            onChange={(value) => filters.onChange("machine", value ?? "")}
            disabled={filters.loadingMachines}
            placeholder={filters.loadingMachines ? "Loading machines…" : "All machines"}
          />
          <NativeSelect
            label="Harness"
            aria-label="Filter by harness"
            value={filters.harness}
            onChange={(event) => filters.onChange("harness", event.currentTarget.value)}
            data={[
              { value: "", label: "All harnesses" },
              { value: "claude", label: "Claude Code" },
              { value: "codex", label: "Codex CLI" },
            ]}
          />
          <Checkbox.Group label="Session kinds" value={filters.kinds} onChange={filters.onKindsChange}>
            <SimpleGrid cols={2} spacing={4} mt={4}>
              {SESSION_KINDS.map(({ value, label }) => (
                <Checkbox key={value} value={value} label={label} size="xs" />
              ))}
            </SimpleGrid>
          </Checkbox.Group>
          <Button size="xs" variant="default" loading={sessions.loading} onClick={sessions.onRefresh}>
            Refresh session list
          </Button>
          <ScrollArea style={{ flex: 1, minHeight: 0 }} type="auto">
            {sessions.loading ? (
              <Center h={200}>
                <Loader size="sm" aria-label="Loading sessions" />
              </Center>
            ) : sessions.items.length === 0 ? (
              <Center h={160} px="md">
                <Text size="sm" c="dimmed" ta="center">
                  No captured sessions match these filters.
                </Text>
              </Center>
            ) : (
              <Stack gap={4}>
                {sessions.items.map((session) => (
                  <SessionRow
                    key={session.id}
                    session={session}
                    machineName={sessions.machineNames.get(session.machine_id) ?? session.machine_id}
                    selected={session.id === sessions.selectedId}
                    to={sessions.pathFor(session.id)}
                    onClick={() => layout.setMobileOpen(false)}
                  />
                ))}
              </Stack>
            )}
            {sessions.nextCursor !== null && (
              <Button fullWidth variant="default" mt="xs" loading={sessions.loadingNext} onClick={sessions.onLoadMore}>
                Load more sessions
              </Button>
            )}
          </ScrollArea>
        </Stack>
      </Paper>
      <ResizeHandle
        pane={layout.sidebarPane}
        axis="x"
        className="session-browser-resize-handle"
        aria-label="Resize session list"
        aria-controls="session-browser-sidebar"
        aria-valuetext={`${layout.sidebarWidth} pixels`}
      />
    </>
  );
}

export function TranscriptStatusBadge({
  session,
  size,
  className,
}: {
  session: SessionSummary;
  size: "xs" | "sm";
  className?: string;
}): JSX.Element | null {
  if (session.transcript_status === "captured" && session.latest_version_record_count !== 0) return null;
  const status = transcriptStatus(session);
  return (
    <Badge className={className} size={size} color={status.color} variant="light">
      {status.label}
    </Badge>
  );
}

function SessionRow({
  session,
  machineName,
  selected,
  to,
  onClick,
}: {
  session: SessionSummary;
  machineName: string;
  selected: boolean;
  to: string;
  onClick: () => void;
}): JSX.Element {
  return (
    <Button
      component={Link}
      to={to}
      onClick={onClick}
      variant={selected ? "light" : "subtle"}
      color={selected ? "blue" : "gray"}
      fullWidth
      justify="flex-start"
      className="session-browser-session-row"
      aria-current={selected ? "page" : undefined}
      styles={{
        root: { height: "auto", padding: "8px", textAlign: "left" },
        label: { display: "block", width: "100%" },
      }}
    >
      <Box className="session-browser-session-row-content">
        <Text className="session-row-machine" size="xs" fw={600} truncate>
          {machineName}
        </Text>
        <Text className="session-row-harness" size="xs" c="dimmed">
          {harnessLabel(session.harness)}
        </Text>
        <Text className="session-row-time" size="xs" c="dimmed">
          {formatDateTime(session.last_activity_at)}
        </Text>
        <Text className="session-row-title" size="sm" fw={500} lineClamp={2} style={{ whiteSpace: "normal" }}>
          {session.title || "Untitled session"}
        </Text>
        <TranscriptStatusBadge className="session-row-status" session={session} size="xs" />
      </Box>
    </Button>
  );
}

export function transcriptStatus(session: SessionSummary): { label: string; color: string } {
  switch (session.transcript_status) {
    case "captured":
      return { label: session.latest_version_record_count === 0 ? "Latest capture empty" : "Captured", color: "green" };
    case "pending":
      return { label: "Inventory only", color: "yellow" };
    case "failed":
      return { label: "Import failed", color: "red" };
    case "unavailable":
      return { label: "No captured version", color: "gray" };
  }
}
