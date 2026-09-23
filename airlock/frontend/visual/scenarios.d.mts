export type VisualScenario = {
  element: string;
  readySelectors?: string[];
  colorScheme?: "light" | "dark";
  viewport?: { width: number; height: number };
};

export const SCENARIOS: Record<string, VisualScenario>;
