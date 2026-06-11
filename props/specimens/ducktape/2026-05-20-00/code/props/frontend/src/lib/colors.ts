// Shared color utilities for recall, split badges, and issue types

/** Returns Tailwind classes for recall value styling */
export function recallColorClass(value: number | null | undefined): string {
  if (value == null) return "text-gray-400 dark:text-gray-500";
  if (value >= 0.7) return "text-green-600 dark:text-green-400 font-medium";
  if (value >= 0.4) return "text-yellow-600 dark:text-yellow-400";
  return "text-red-600 dark:text-red-400";
}

/** Returns Tailwind classes for split badge styling */
export function splitBadgeClass(split: string): string {
  switch (split) {
    case "train":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200";
    case "valid":
      return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
    case "test":
      return "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200";
    default:
      return "bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200";
  }
}

export interface IssueColorScheme {
  bg: string;
  border: string;
  borderLeft: string;
  headerBg: string;
  text: string;
  textDark: string;
}

/** Color schemes for issue types — static class names for Tailwind scanner */
export const issueColors: Record<string, IssueColorScheme> = {
  tp: {
    bg: "bg-green-50 dark:bg-green-950",
    border: "border-green-200 dark:border-green-800",
    borderLeft: "border-l-4 border-green-500",
    headerBg: "bg-green-100 dark:bg-green-900",
    text: "text-green-600 dark:text-green-400",
    textDark: "text-green-700 dark:text-green-300",
  },
  fp: {
    bg: "bg-red-50 dark:bg-red-950",
    border: "border-red-200 dark:border-red-800",
    borderLeft: "border-l-4 border-red-500",
    headerBg: "bg-red-100 dark:bg-red-900",
    text: "text-red-600 dark:text-red-400",
    textDark: "text-red-700 dark:text-red-300",
  },
  critique: {
    bg: "bg-blue-50 dark:bg-blue-950",
    border: "border-blue-200 dark:border-blue-800",
    borderLeft: "border-l-4 border-blue-500",
    headerBg: "bg-blue-100 dark:bg-blue-900",
    text: "text-blue-600 dark:text-blue-400",
    textDark: "text-blue-700 dark:text-blue-300",
  },
  critiqueFp: {
    bg: "bg-orange-50 dark:bg-orange-950",
    border: "border-orange-200 dark:border-orange-800",
    borderLeft: "border-l-4 border-orange-500",
    headerBg: "bg-orange-100 dark:bg-orange-900",
    text: "text-orange-600 dark:text-orange-400",
    textDark: "text-orange-700 dark:text-orange-300",
  },
};
