import React, { useState } from "react";
import { Button, Menu } from "@mantine/core";
import { NumberField, NativeSelectField } from "./lib/controls.tsx";
import { scenarioColor } from "./input_helpers.ts";
import { ScenarioTabs } from "./scenario_tabs.tsx";
import { PropertyPurchasePanel, SellOrderControl, ProductPortfolioPanel } from "./forms.tsx";

const INDEX_DATA = [
  { value: "inflation", label: "Inflation" },
  { value: "none", label: "None" },
];

// Scalar knobs that are shared-by-default but overridable per scenario (see plans/scenario_editor.md).
// A knob is rendered shared when every scenario agrees (and it isn't pinned); otherwise it gets a
// per-scenario override column. The housing cluster + lifecycle events are NOT here — they're edited
// per scenario in the Property & timeline panel below. Liquidity/sell-order is shared-only.
// Display groups so related knobs stay together; a flat flow split e.g. the cash-buffer trio.
const SCALAR_GROUPS = ["Spending", "Outside rent", "Cash buffer", "Private equity"];

const SCALAR_KNOBS = [
  { key: "monthlySpendUsd", label: "Monthly spend", kind: "usd", min: 1, step: 100, group: "Spending" },
  { key: "spendIndex", label: "Spend index", kind: "index", group: "Spending" },
  { key: "monthlyRentUsd", label: "Monthly rent", kind: "usd", min: 0, step: 100, group: "Outside rent" },
  { key: "rentalLocationId", label: "Rent location", kind: "location", group: "Outside rent" },
  { key: "cashBufferTriggerBelowUsd", label: "Trigger below", kind: "usd", min: 0, step: 1000, group: "Cash buffer" },
  { key: "cashBufferSaleUsd", label: "Sell amount", kind: "usd", min: 0, step: 1000, group: "Cash buffer" },
  { key: "cashBufferIndexToInflation", label: "Buffer index", kind: "boolIndex", group: "Cash buffer" },
  { key: "peLnwFloorUsd", label: "PE LNW floor", kind: "usd", min: 0, step: 10000, group: "Private equity" },
  { key: "peIndexFloorToInflation", label: "PE floor index", kind: "boolIndex", group: "Private equity" },
];

function KnobField({ knob, value, label, bootstrap, onChange }) {
  if (knob.kind === "usd") {
    return <NumberField label={label} value={value} min={knob.min} step={knob.step} prefix="$" onChange={onChange} />;
  }
  if (knob.kind === "index") {
    return (
      <NativeSelectField
        label={label}
        aria-label={label}
        value={value}
        data={INDEX_DATA}
        onChange={(event) => onChange(event.target.value === "none" ? "none" : "inflation")}
      />
    );
  }
  if (knob.kind === "boolIndex") {
    return (
      <NativeSelectField
        label={label}
        aria-label={label}
        value={value ? "inflation" : "none"}
        data={INDEX_DATA}
        onChange={(event) => onChange(event.target.value === "inflation")}
      />
    );
  }
  // location
  return (
    <NativeSelectField
      label={label}
      aria-label={label}
      value={value ?? ""}
      data={[
        { value: "", label: "(default)" },
        ...bootstrap.locations.map((location) => ({ value: location.id, label: location.label })),
      ]}
      onChange={(event) => onChange(event.target.value || null)}
    />
  );
}

// Full-width scenario editor. Most knobs are edited once (shared → applies to every scenario); any
// scalar can be split into a per-scenario override. Housing + lifecycle events are edited per
// scenario for the active scenario (switch with the chips). Data model is unchanged: each scenario
// keeps a full input; "shared vs overridden" is computed here.
export function ScenarioEditor({
  scenarios,
  activeId,
  bootstrap,
  portfolio,
  portfolioError,
  horizonMonths,
  onSelect,
  onAdd,
  onDelete,
  onRename,
  onResetActive,
  onSetAll,
  onUpdateScenario,
}) {
  // Knobs the user chose to override even though the scenarios currently agree. A knob that already
  // differs is always an override column regardless of this set.
  const [pinned, setPinned] = useState(() => new Set());
  // The editor is tall (it carries the full property + portfolio panels); let it collapse to a
  // compact bar (chips stay visible) so the chart/results below are reachable without scrolling.
  const [open, setOpen] = useState(true);
  const activeIndex = Math.max(
    0,
    scenarios.findIndex((entry) => entry.id === activeId)
  );
  const active = scenarios[activeIndex] ?? scenarios[0];
  const multi = scenarios.length > 1;
  const differs = (key) => !scenarios.every((entry) => entry.input[key] === scenarios[0].input[key]);
  const isOverride = (key) => differs(key) || pinned.has(key);
  const sharedKnobs = SCALAR_KNOBS.filter((knob) => !isOverride(knob.key));
  const overrideKnobs = SCALAR_KNOBS.filter((knob) => isOverride(knob.key));
  const pin = (key) => setPinned((previous) => new Set(previous).add(key));
  const makeShared = (key) => {
    onSetAll(key, active.input[key]); // collapse divergence onto the active scenario's value
    setPinned((previous) => {
      const next = new Set(previous);
      next.delete(key);
      return next;
    });
  };

  return (
    <div className="augur-card divide-y divide-slate-200 dark:divide-slate-700" data-product-scenario-editor="">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <ScenarioTabs
          scenarios={scenarios}
          activeId={activeId}
          onSelect={onSelect}
          onAdd={onAdd}
          onDelete={onDelete}
          onRename={onRename}
        />
        <div className="flex items-center gap-1">
          {open && (
            <Button size="xs" variant="subtle" onClick={onResetActive}>
              Reset {multi ? active.label : "scenario"}
            </Button>
          )}
          <Button
            size="xs"
            variant="default"
            data-product-editor-toggle=""
            aria-expanded={open}
            onClick={() => setOpen((previous) => !previous)}
          >
            {open ? "Hide ▴" : "Edit ▾"}
          </Button>
        </div>
      </div>

      {open && (
        <>
          <div className="px-4 py-3">
            <div className="flex items-center justify-between gap-2">
              <div className="augur-eyebrow">Spending &amp; funding{multi ? " — shared across all scenarios" : ""}</div>
              {multi && sharedKnobs.length > 0 && (
                <Menu shadow="md" position="bottom-end">
                  <Menu.Target>
                    <Button size="xs" variant="default" data-product-add-override="">
                      + Override a setting
                    </Button>
                  </Menu.Target>
                  <Menu.Dropdown>
                    {sharedKnobs.map((knob) => (
                      <Menu.Item key={knob.key} onClick={() => pin(knob.key)}>
                        {knob.label}
                      </Menu.Item>
                    ))}
                  </Menu.Dropdown>
                </Menu>
              )}
            </div>
            <div className="mt-3 space-y-3">
              {SCALAR_GROUPS.map((group) => {
                const groupKnobs = sharedKnobs.filter((knob) => knob.group === group);
                if (groupKnobs.length === 0) return null;
                return (
                  <div key={group}>
                    <div className="augur-field-label mb-1">{group}</div>
                    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                      {groupKnobs.map((knob) => (
                        <KnobField
                          key={knob.key}
                          knob={knob}
                          value={active.input[knob.key]}
                          label={knob.label}
                          bootstrap={bootstrap}
                          onChange={(value) => onSetAll(knob.key, value)}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
            <SellOrderControl
              sellOrder={active.input.sellOrder}
              portfolio={portfolio}
              onChange={(sellOrder) => onSetAll("sellOrder", sellOrder)}
            />
          </div>

          {overrideKnobs.length > 0 && (
            <div className="px-4 py-3">
              <div className="augur-eyebrow">Per-scenario overrides</div>
              <div className="mt-3 grid gap-4">
                {overrideKnobs.map((knob) => (
                  <div key={knob.key} data-product-override-knob={knob.key}>
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <div className="augur-field-label">{knob.label}</div>
                      <button
                        type="button"
                        className="augur-link text-xs font-semibold"
                        onClick={() => makeShared(knob.key)}
                      >
                        Make shared
                      </button>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                      {scenarios.map((entry, index) => (
                        <div key={entry.id} className="flex items-end gap-2">
                          <span
                            className="mb-2 h-3 w-3 shrink-0 rounded-full"
                            style={{ backgroundColor: scenarioColor(index) }}
                            aria-hidden="true"
                          />
                          <div className="min-w-0 flex-1">
                            <KnobField
                              knob={knob}
                              value={entry.input[knob.key]}
                              label={entry.label}
                              bootstrap={bootstrap}
                              onChange={(value) => onUpdateScenario(entry.id, { [knob.key]: value })}
                            />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            {multi && (
              <div className="px-4 pt-3 text-xs augur-muted">
                Housing &amp; timeline apply to{" "}
                <span className="font-semibold" style={{ color: scenarioColor(activeIndex) }}>
                  {active.label}
                </span>{" "}
                — switch scenarios with the chips above.
              </div>
            )}
            <PropertyPurchasePanel
              bootstrap={bootstrap}
              input={active.input}
              onChange={(patch) => onUpdateScenario(activeId, patch)}
              horizonMonths={horizonMonths}
            />
          </div>

          <ProductPortfolioPanel portfolio={portfolio} error={portfolioError} />
        </>
      )}
    </div>
  );
}
