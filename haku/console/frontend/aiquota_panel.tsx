import { ActionIcon, Badge, Group, Loader, Progress, Stack, Text, Tooltip } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";

import type { AiquotaView } from "./client";
import { AiquotaIcon, CloseIcon } from "./icons";
import {
  formatClockTime,
  formatDurationShort,
  formatWindowDuration,
  parseTimestamp,
  secondsUntil,
  shortDate,
} from "./time";

type Window = NonNullable<NonNullable<AiquotaView["providers"][number]["last_success"]>["result"]["windows"]>[number];

function windowLabel(window: Window): string {
  const seconds = Math.round(window.window_seconds);
  const length = formatWindowDuration(seconds);
  return window.name ? window.name + " (" + length + ")" : length;
}

function statusColor(window: Window, short: boolean): string {
  if (short && window.used_percent >= 85) return "red";
  if (window.used_percent >= 95) return "red";
  if (window.used_percent >= 80) return "yellow";
  return "teal";
}

function effectiveWindows(provider: AiquotaView["providers"][number]): Window[] {
  const result = provider.last_output.result;
  if (result.kind === "success" && result.windows?.length) {
    return result.windows.filter((window) => window.display);
  }
  return provider.last_success?.result.windows?.filter((window) => window.display) ?? [];
}

function effectiveResetCredits(provider: AiquotaView["providers"][number]): number | null {
  const result = provider.last_output.result;
  if (result.kind === "success" && (result.windows?.length || result.available_reset_credits != null)) {
    return result.available_reset_credits ?? null;
  }
  return provider.last_success?.result.available_reset_credits ?? null;
}

function effectiveResetCreditExpiries(provider: AiquotaView["providers"][number]): string[] {
  const result = provider.last_output.result;
  if (result.kind === "success" && (result.windows?.length || result.available_reset_credits != null)) {
    return result.available_reset_credit_expiries ?? [];
  }
  return provider.last_success?.result.available_reset_credit_expiries ?? [];
}

function QuotaWindowRow({ quotaWindow, short }: { quotaWindow: Window; short: boolean }): JSX.Element {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = globalThis.setInterval(() => setNow(Date.now()), 1000);
    return () => globalThis.clearInterval(timer);
  }, []);
  const resetSeconds = quotaWindow.reset_at ? secondsUntil(quotaWindow.reset_at, now) : quotaWindow.reset_seconds;
  const used = Math.max(0, Math.min(100, quotaWindow.used_percent));
  return (
    <div className="haku-aiquota-window">
      <Group justify="space-between" gap="xs" wrap="nowrap">
        <Text size="sm" fw={600} truncate>
          {windowLabel(quotaWindow)}
        </Text>
        <Text size="sm" ff="monospace">
          {Math.round(quotaWindow.used_percent)}%
        </Text>
      </Group>
      <Progress value={used} color={statusColor(quotaWindow, short)} size="sm" radius="xl" />
      <Group justify="space-between" gap="xs" wrap="nowrap">
        <Text size="xs" c="dimmed">
          {Math.round(100 - quotaWindow.used_percent)}% remaining
        </Text>
        <Text size="xs" c="dimmed" ff="monospace">
          ↻ {formatDurationShort(resetSeconds)}
        </Text>
      </Group>
    </div>
  );
}

function ProviderSection({ provider }: { provider: AiquotaView["providers"][number] }): JSX.Element {
  const windows = effectiveWindows(provider);
  const resetCredits = effectiveResetCredits(provider);
  const resetExpiryText = effectiveResetCreditExpiries(provider)
    .map(shortDate)
    .filter((expiry): expiry is string => expiry !== null)
    .join(", ");
  const result = provider.last_output.result;
  const stale = provider.last_success !== null && (result.kind !== "success" || windows.length === 0);
  return (
    <section className="haku-aiquota-provider" aria-label={`${provider.provider} quota`}>
      <Group justify="space-between" align="center" wrap="nowrap">
        <Text fw={700}>{provider.provider}</Text>
        <Group gap="xs" wrap="nowrap">
          {resetCredits !== null && (
            <Badge variant="light" color="blue">
              {resetCredits} banked reset{resetCredits === 1 ? "" : "s"}
            </Badge>
          )}
          {provider.currently_over_plan && (
            <Badge color="red" variant="light">
              Over plan
            </Badge>
          )}
          {stale && (
            <Badge color="yellow" variant="light">
              Stale
            </Badge>
          )}
          {result.kind === "error" && (
            <Badge color="red" variant="light">
              Error
            </Badge>
          )}
        </Group>
      </Group>
      {result.kind === "error" && (
        <Text size="xs" c="red">
          {result.error}
        </Text>
      )}
      {resetExpiryText && (
        <Text size="xs" c="dimmed">
          Known expiries: {resetExpiryText}
        </Text>
      )}
      {windows.length === 0 ? (
        <Text size="sm" c="dimmed">
          No quota data available.
        </Text>
      ) : (
        <Stack gap="sm">
          {windows.map((quotaWindow, index) => (
            <QuotaWindowRow
              key={`${quotaWindow.name ?? "window"}-${quotaWindow.window_seconds}`}
              quotaWindow={quotaWindow}
              short={index === 0}
            />
          ))}
        </Stack>
      )}
      {provider.extra_status !== "none" && (
        <Text size="xs" c="dimmed">
          Extra spend {provider.extra_status}
        </Text>
      )}
    </section>
  );
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
        {quotas?.providers.map((provider) => (
          <ProviderSection key={provider.provider} provider={provider} />
        ))}
        {fetchedAt && (
          <Text size="xs" c="dimmed">
            Snapshot {formatClockTime(fetchedAt)}
          </Text>
        )}
      </Stack>
    </section>
  );
}

function railWindows(quotas: AiquotaView | null): Window[] {
  return quotas?.providers.flatMap(effectiveWindows) ?? [];
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
  const windows = railWindows(quotas);
  const used = windows.length === 0 ? 0 : Math.max(...windows.map((quotaWindow) => quotaWindow.used_percent));
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
