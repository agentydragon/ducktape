/**
 * The console's AI-quota side panel: console chrome around aiquota's own board.
 *
 * The board (`//aiquota/frontend:board`) is the renderer aiquota's standalone dashboard uses,
 * fed here by the payload `aiquota_proxy.py` already fetches — so the console cannot drift from
 * the dashboard, the CLI and the GNOME popup the way a second hand-written copy did. What stays
 * console-side is what the console owns: the panel frame, the loading and error states, and the
 * rail button.
 */

// Reached by workspace-relative path: neither tsconfig declares `paths`, and the board is a
// Bazel dep (//aiquota/frontend:board) rather than an npm package.
import { QuotaBoard } from "../../../aiquota/frontend/board";
import { ActionIcon, Group, Loader, Stack, Text, Tooltip } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";

import type { AiquotaView } from "./client";
import { AiquotaIcon, CloseIcon } from "./icons";
import { formatClockTime, parseTimestamp } from "./time";

/** Reset countdowns are the only thing that moves between fetches; tick them like a clock. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = globalThis.setInterval(() => setNow(Date.now()), 1000);
    return () => globalThis.clearInterval(timer);
  }, []);
  return now;
}

export function AiquotaPanel({
  quotas,
  loading,
  error,
  onClose,
}: {
  quotas: AiquotaView | null;
  loading: boolean;
  error: string | null;
  onClose?: () => void;
}): JSX.Element {
  const now = useNow();
  const fetchedAt = useMemo(() => (quotas ? parseTimestamp(quotas.fetched_at) : null), [quotas]);
  return (
    <section className="haku-shell-card haku-shell-side-panel haku-aiquota-panel" aria-label="AI quotas">
      <Stack gap="sm">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <Text fw={700}>AI quotas</Text>
            {onClose && (
              <ActionIcon size="sm" variant="subtle" aria-label="Close AI quotas" onClick={onClose}>
                <CloseIcon size={16} />
              </ActionIcon>
            )}
          </Group>
          {loading && <Loader size="xs" />}
        </Group>
        {error && (
          <Text size="sm" c="red">
            {error}
          </Text>
        )}
        {!quotas && !loading && !error && (
          <Text size="sm" c="dimmed">
            No quota data available.
          </Text>
        )}
        {quotas && <QuotaBoard quotas={quotas} now={now} />}
        {fetchedAt && (
          <Text size="xs" c="dimmed">
            Snapshot {formatClockTime(fetchedAt)}
          </Text>
        )}
      </Stack>
    </section>
  );
}

/**
 * The rail's own summary: the worst usage across every provider, which is all a 32px glyph can
 * say. Deliberately not the board's pace-aware tint — the rail answers "is anything close to
 * full", and opening the panel answers why.
 */
function railUsedPercent(quotas: AiquotaView | null): number {
  const used = (quotas?.providers ?? []).flatMap((provider) => {
    const result = provider.last_output.result;
    const windows =
      result.kind === "success" && result.windows?.length
        ? result.windows
        : (provider.last_success?.result.windows ?? []);
    return windows.filter((window) => window.display).map((window) => window.used_percent);
  });
  return used.length === 0 ? 0 : Math.max(...used);
}

export function AiquotaRailButton({
  quotas,
  loading,
  open,
  onClick,
}: {
  quotas: AiquotaView | null;
  loading: boolean;
  open: boolean;
  onClick: () => void;
}): JSX.Element {
  const used = railUsedPercent(quotas);
  const color = used >= 95 ? "red" : used >= 80 ? "yellow" : "teal";
  return (
    <Tooltip label={open ? "Close AI quotas" : "Open AI quotas"} position="right" withArrow openDelay={350}>
      <ActionIcon
        size="lg"
        radius="md"
        variant={open ? "filled" : "subtle"}
        color={color}
        onClick={onClick}
        aria-label={open ? "Close AI quotas" : "Open AI quotas"}
        aria-pressed={open}
        className="haku-aiquota-rail-button"
      >
        <span className="haku-aiquota-rail-glyph" aria-hidden="true">
          {loading ? <Loader size={18} /> : <AiquotaIcon />}
        </span>
        <span className={`haku-aiquota-rail-meter haku-aiquota-rail-meter-${color}`} aria-hidden="true">
          <span style={{ width: `${Math.min(100, used)}%` }} />
        </span>
      </ActionIcon>
    </Tooltip>
  );
}
