import React, { useState } from "react";
import { scenarioColor } from "./input_helpers.ts";

// The active scenario's visual identity (position color + name), reused by every panel that scopes
// to the active scenario (histogram, selected-rollout overlay, events, terminal table) so they all
// announce *which* variant they're showing — not just the chip selector. Color matches the chip and
// chart legend (`scenarioColor`, by position), so the badge reads as the same entity everywhere.
export function ScenarioBadge({ label, color, className }) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${className ?? ""}`} data-product-active-scenario-badge="">
      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: color }} aria-hidden="true" />
      <span className="font-medium">{label}</span>
    </span>
  );
}

// Chip selector for the Base + variant set. Clicking a chip makes that entry active — the rollout
// histogram, selected-rollout overlay, events, and the detailed terminal table all scope to it,
// while the fan chart overlays Base + every variant at once. Double-click a label to rename it
// (names drive the chart legend and the per-variant comparison columns, e.g. "Rent" vs "Buy").
// Color is assigned by position (`scenarioColor`, Base = index 0) so it stays put as the active
// selection moves; the active chip is marked by its ring, not by hue. Base is the baseline every
// variant inherits from, so it can't be deleted.
export function ScenarioTabs({ entries, activeId, onSelect, onDelete, onRename }) {
  const [editingId, setEditingId] = useState(null);
  return (
    <div className="flex flex-wrap items-center gap-2" data-product-scenario-tabs="">
      <span className="augur-eyebrow mr-1">Scenarios</span>
      {entries.map((entry, index) => {
        const isActive = entry.id === activeId;
        const isEditing = editingId === entry.id;
        const isBase = entry.id === "base";
        // A blank label renders empty everywhere and the URL codec substitutes a default on
        // reload/share, so normalize an all-whitespace name to a non-empty fallback on commit.
        const commitEdit = () => {
          if (entry.label.trim() === "") onRename(entry.id, isBase ? "Base" : `Variant ${index}`);
          setEditingId(null);
        };
        return (
          <div
            key={entry.id}
            data-product-scenario-tab={entry.id}
            data-active={isActive ? "" : undefined}
            className={`flex items-center gap-2 rounded-full border px-3 py-1 text-sm ${
              isActive
                ? "border-slate-400 bg-white shadow-sm dark:border-slate-500 dark:bg-slate-800"
                : "border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900"
            }`}
          >
            <span
              className="h-3 w-3 shrink-0 rounded-full"
              style={{ backgroundColor: scenarioColor(index) }}
              aria-hidden="true"
            />
            {isEditing ? (
              <input
                data-product-scenario-rename={entry.id}
                aria-label={`Rename ${entry.label}`}
                autoFocus
                className="w-28 min-w-0 bg-transparent font-medium text-slate-800 focus:outline-none dark:text-slate-100"
                value={entry.label}
                onChange={(event) => onRename(entry.id, event.target.value)}
                onBlur={commitEdit}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === "Escape") commitEdit();
                }}
              />
            ) : (
              <button
                type="button"
                data-product-scenario-select={entry.id}
                className={`font-medium ${
                  isActive
                    ? "text-slate-800 dark:text-slate-100"
                    : "text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
                }`}
                onClick={() => onSelect(entry.id)}
                onDoubleClick={() => setEditingId(entry.id)}
                title="Click to select, double-click to rename"
              >
                {entry.label}
              </button>
            )}
            {!isBase && (
              <button
                type="button"
                data-product-scenario-delete={entry.id}
                aria-label={`Delete ${entry.label}`}
                className="text-base leading-none text-slate-400 hover:text-rose-600 dark:hover:text-rose-400"
                onClick={() => onDelete(entry.id)}
              >
                ×
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
