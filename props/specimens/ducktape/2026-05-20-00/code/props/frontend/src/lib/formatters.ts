/**
 * Shared formatting helpers for display.
 * All display-related transformations go here.
 */

import { formatDistanceToNow } from "date-fns";
import type { RunInfo, CriticTypeConfig } from "./api/client";
import type { StatsWithCI } from "./types";

// --- Percentage formatting ---

const pctFormatter = new Intl.NumberFormat(undefined, {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const pctFormatterWhole = new Intl.NumberFormat(undefined, {
  style: "percent",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/** Format a 0.0-1.0 value as percentage string. */
export function formatPct(value: number, decimals = 1): string {
  return decimals === 0 ? pctFormatterWhole.format(value) : pctFormatter.format(value);
}

/** Format StatsWithCI as "mean ± margin". */
export function formatStatsWithCI(stats: StatsWithCI, options?: { showN?: boolean }): string {
  const mean = formatPct(stats.mean);

  if (stats.lcb95 != null && stats.ucb95 != null) {
    const margin = (stats.ucb95 - stats.lcb95) / 2;
    return `${mean} ± ${formatPct(margin)}`;
  }

  if (options?.showN) {
    return `${mean} (n=${stats.n})`;
  }

  return mean;
}

/** Format StatsWithCI compactly for table cells. */
export function formatStatsCompact(stats: StatsWithCI): string {
  const mean = formatPct(stats.mean);

  if (stats.lcb95 != null && stats.ucb95 != null) {
    const margin = (stats.ucb95 - stats.lcb95) / 2;
    return `${mean} ± ${formatPct(margin, 0)}`;
  }

  return mean;
}

// --- Date formatting ---

/** Format an ISO date as relative time (e.g., "2 hours ago"). */
export function formatAge(isoDate: string, addSuffix = true): string {
  return formatDistanceToNow(new Date(isoDate), { addSuffix });
}

// --- Snapshot/example formatting ---

/** Format a snapshot slug for display (first path component only). */
export function formatSnapshotSlug(slug: string): string {
  return slug.split("/")[0];
}

/** Format a files hash for display (8 char truncation). */
export function formatFilesHash(hash: string): string {
  return hash.slice(0, 8);
}

/** Format a UUID for display (first 8 chars). */
export function formatUuid(uuid: string): string {
  return uuid.slice(0, 8);
}

/** Format an OCI image digest for display (sha256:abcdef12... → sha256:abcdef12). */
export function formatDigest(digest: string): string {
  const prefix = "sha256:";
  if (digest.startsWith(prefix)) {
    return prefix + digest.slice(prefix.length, prefix.length + 8);
  }
  return digest.length > 16 ? digest.slice(0, 16) : digest;
}

/** Truncate text with ellipsis. */
export function truncateText(text: string, maxLength: number = 100): string {
  return text.length > maxLength ? text.slice(0, maxLength) + "..." : text;
}

/** Format an example for display in compact form. */
export function formatExample(run: RunInfo): string {
  if (run.type_config.agent_type !== "critic") return "—";
  const config = run.type_config as CriticTypeConfig;
  const example = config.example;
  const slug = formatSnapshotSlug(example.snapshot_slug);
  if (example.kind === "whole_snapshot") {
    return `whole@${slug}`;
  }
  return `files@${slug}/${formatFilesHash(example.files_hash)}`;
}

// --- Location anchor formatting ---

/** Format a location anchor as "file:line" or "file:start-end". */
export function formatLocationAnchor(loc: {
  file: string;
  start_line?: number | null;
  end_line?: number | null;
}): string {
  if (loc.start_line == null) return loc.file;
  const endLine = loc.end_line ?? loc.start_line;
  if (loc.start_line === endLine) return `${loc.file}:${loc.start_line}`;
  return `${loc.file}:${loc.start_line}-${endLine}`;
}

/** Group flat location anchors by file path. */
export function groupLocationsByFile<T extends { file: string }>(locations: T[]): Map<string, T[]> {
  const byFile = new Map<string, T[]>();
  for (const loc of locations) {
    const existing = byFile.get(loc.file);
    if (existing) {
      existing.push(loc);
    } else {
      byFile.set(loc.file, [loc]);
    }
  }
  return byFile;
}
