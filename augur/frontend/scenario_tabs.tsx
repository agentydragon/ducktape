import React, { useState } from "react";
import { scenarioColor, MAX_SCENARIOS } from "./input_helpers.ts";

// Selector + editor for the comparison scenario set. Clicking a chip makes that scenario active —
// the rollout histogram, selected-rollout overlay, events, and the detailed terminal table all
// scope to the active scenario, while the fan chart overlays every scenario at once. Double-click a
// label to rename it (names drive the chart legend and the per-scenario comparison columns, e.g.
// "Rent" vs "Mortgage"). Color is assigned by position (`scenarioColor`) so it stays put as the
// active selection moves; the active chip is marked by its ring, not by hue.
export function ScenarioTabs({ scenarios, activeId, onSelect, onAdd, onDelete, onRename }) {
  const [editingId, setEditingId] = useState(null);
  const canDelete = scenarios.length > 1;
  const canAdd = scenarios.length < MAX_SCENARIOS;
  return (
    <div className="augur-card flex flex-wrap items-center gap-2 p-3" data-product-scenario-tabs="">
      <span className="augur-eyebrow mr-1">Scenarios</span>
      {scenarios.map((scenario, index) => {
        const isActive = scenario.id === activeId;
        const isEditing = editingId === scenario.id;
        const commitEdit = () => setEditingId(null);
        return (
          <div
            key={scenario.id}
            data-product-scenario-tab={scenario.id}
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
                data-product-scenario-rename={scenario.id}
                aria-label={`Rename ${scenario.label}`}
                autoFocus
                className="w-28 min-w-0 bg-transparent font-medium text-slate-800 focus:outline-none dark:text-slate-100"
                value={scenario.label}
                onChange={(event) => onRename(scenario.id, event.target.value)}
                onBlur={commitEdit}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === "Escape") commitEdit();
                }}
              />
            ) : (
              <button
                type="button"
                data-product-scenario-select={scenario.id}
                className={`font-medium ${
                  isActive
                    ? "text-slate-800 dark:text-slate-100"
                    : "text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
                }`}
                onClick={() => onSelect(scenario.id)}
                onDoubleClick={() => setEditingId(scenario.id)}
                title="Click to select, double-click to rename"
              >
                {scenario.label}
              </button>
            )}
            {canDelete && (
              <button
                type="button"
                data-product-scenario-delete={scenario.id}
                aria-label={`Delete ${scenario.label}`}
                className="text-base leading-none text-slate-400 hover:text-rose-600 dark:hover:text-rose-400"
                onClick={() => onDelete(scenario.id)}
              >
                ×
              </button>
            )}
          </div>
        );
      })}
      {canAdd && (
        <button
          type="button"
          data-product-scenario-add=""
          className="rounded-full border border-dashed border-slate-300 px-3 py-1 text-sm font-medium text-slate-500 hover:border-slate-400 hover:text-slate-700 dark:border-slate-600 dark:text-slate-400 dark:hover:text-slate-200"
          onClick={onAdd}
        >
          + Add scenario
        </button>
      )}
    </div>
  );
}
