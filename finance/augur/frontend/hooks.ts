import { createContext, useEffect, useState } from "react";
import { DEFAULT_HIDDEN_EVENT_KINDS, ROLLOUT_EVENT_KIND_ORDER } from "./data_helpers";
import type { CurrencyDisplay } from "./header";
import type { RolloutEventKind } from "./rollout_event_vocabulary.generated";

const CurrencyDisplayContext = createContext<{
  display: CurrencyDisplay;
  setDisplay: (display: CurrencyDisplay) => void;
}>({
  display: "compact",
  setDisplay: () => {},
});

export const CurrencyDisplayProvider = CurrencyDisplayContext.Provider;

export function useVisibleEventKinds() {
  const allKinds = ROLLOUT_EVENT_KIND_ORDER;
  const [visible, setVisible] = useState<Set<RolloutEventKind>>(
    () => new Set(allKinds.filter((kind) => !DEFAULT_HIDDEN_EVENT_KINDS.has(kind)))
  );
  const toggle = (kind: RolloutEventKind) => {
    setVisible((previous) => {
      const next = new Set(previous);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };
  const only = (kind: RolloutEventKind) => setVisible(new Set([kind]));
  const showAll = () => setVisible(new Set(allKinds));
  const hideAll = () => setVisible(new Set());
  return { visible, toggle, only, showAll, hideAll };
}

export function useEventSelection() {
  const [selectedEventMonthIndex, setSelectedEventMonthIndex] = useState<number | null>(null);
  const [hoveredEventMonthIndex, setHoveredEventMonthIndex] = useState<number | null>(null);
  const toggle = (monthIndex: number) =>
    setSelectedEventMonthIndex((previous) => (previous === monthIndex ? null : monthIndex));
  const clear = () => {
    setSelectedEventMonthIndex(null);
    setHoveredEventMonthIndex(null);
  };
  useEffect(() => {
    if (selectedEventMonthIndex == null) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      clear();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selectedEventMonthIndex]);
  return {
    selectedEventMonthIndex,
    hoveredEventMonthIndex,
    onSelectEventMonth: toggle,
    onHoverEventMonth: setHoveredEventMonthIndex,
    clear,
  };
}
