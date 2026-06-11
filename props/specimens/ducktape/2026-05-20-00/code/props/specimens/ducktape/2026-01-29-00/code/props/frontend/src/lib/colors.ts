// Shared color utilities for recall, split badges, and issue types

/** Returns Tailwind classes for recall value styling */
export function recallColorClass(value: number | null | undefined): string {
  if (value == null) return "text-gray-400";
  if (value >= 0.7) return "text-green-600 font-medium";
  if (value >= 0.4) return "text-yellow-600";
  return "text-red-600";
}

/** Returns Tailwind classes for split badge styling */
export function splitBadgeClass(split: string): string {
  switch (split) {
    case "train":
      return "bg-blue-100 text-blue-800";
    case "valid":
      return "bg-green-100 text-green-800";
    case "test":
      return "bg-purple-100 text-purple-800";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

/** Generate color scheme for an issue type based on base color */
function colorScheme(color: string) {
  return {
    bg: `bg-${color}-50`,
    border: `border-${color}-200`,
    borderLeft: `border-l-4 border-${color}-500`,
    headerBg: `bg-${color}-100`,
    text: `text-${color}-600`,
    textDark: `text-${color}-700`,
  } as const;
}

/** Color scheme for issue types (TP, FP, critique) */
export const issueColors = {
  tp: colorScheme("green"),
  fp: colorScheme("red"),
  critique: colorScheme("blue"),
  critiqueFp: colorScheme("orange"),
} as const;
